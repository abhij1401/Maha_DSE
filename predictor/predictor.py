"""
predictor/predictor.py
======================
Production-ready college predictor for Maharashtra Direct Second Year
Engineering (DSE) CAP Round admissions.

Responsibilities
----------------
- Load and cache data/processed/final_dataset.csv on first use
- Accept a structured UserInput from app.py
- Validate every input field with clear, user-facing error messages
- Resolve the correct category column(s) from the user's caste, gender,
  domicile, PWD, DEF, EWS, and ORPHAN flags
- Filter the dataset by branch preferences, institute type, and round
- Compute an admission chance score for every matching row
- Return a structured PredictionResult back to app.py

NOT responsible for
-------------------
- PDF parsing           → parser/extract_pdf.py
- Data cleaning         → parser/clean_data.py
- Merging rounds        → parser/merge_data.py
- HTTP routing / HTML   → app.py

Architecture
------------
    UserInput          (dataclass – validated input from app.py)
    ValidationError    (exception – carries user-facing message list)
    InputValidator     (validates & normalises UserInput)
    CategoryResolver   (maps caste/gender/domicile flags → category tokens)
    DatasetLoader      (singleton – loads final_dataset.csv once)
    FilterEngine       (applies user preferences to the DataFrame)
    ChanceCalculator   (scores each row with an admission probability)
    ResultBuilder      (assembles PredictionResult from scored rows)
    CollegePredictor   (public façade – called directly by app.py)

Admission chance bands
----------------------
    ≥ 90 %  → "High"
    60–89 % → "Medium"
    30–59 % → "Low"
    < 30 %  → "Very Low"

Usage (from app.py)
-------------------
    from predictor.predictor import CollegePredictor, UserInput

    predictor = CollegePredictor()          # singleton-safe
    result    = predictor.predict(UserInput(
        percentile      = 85.5,
        caste           = "OBC",
        gender          = "Female",
        domicile        = "Maharashtra",
        preferred_branches = ["Computer Engineering", "IT"],
        institute_type  = "Autonomous",
        cap_round       = 2,
        academic_year   = 2024,
        pwd             = False,
        defence         = False,
        ews             = False,
        orphan          = False,
    ))

    if result.success:
        for college in result.colleges:
            print(college)
    else:
        print(result.errors)

Author : MAHA-DSE Project
Python : 3.11+
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import pandas as pd
    import numpy as np
except ImportError as exc:
    sys.exit(
        f"[FATAL] Missing dependency: {exc}. "
        "Run:  pip install pandas numpy"
    )

# ===========================================================================
# PATHS & LOGGING
# ===========================================================================

_ROOT: Path = Path(__file__).resolve().parent.parent
FINAL_DATASET_PATH: Path = _ROOT / "data" / "processed" / "final_dataset.csv"
LOG_DIR: Path = _ROOT / "logs"
LOG_FILE: Path = LOG_DIR / "predictor.log"


def _get_logger() -> logging.Logger:
    """Return (and lazily configure) the module logger."""
    logger = logging.getLogger("predictor")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _get_logger()

# ===========================================================================
# DOMAIN CONSTANTS
# ===========================================================================

# ── Caste / reservation categories ──────────────────────────────────────────
VALID_CASTES: frozenset[str] = frozenset({
    "OPEN", "OBC", "SC", "ST",
    "VJ", "NT-A", "NT-B", "NT-C", "NT-D",
    "SBC", "EWS",
})

# ── Gender ───────────────────────────────────────────────────────────────────
VALID_GENDERS: frozenset[str] = frozenset({"Male", "Female", "Other"})

# ── Domicile ─────────────────────────────────────────────────────────────────
VALID_DOMICILES: frozenset[str] = frozenset({"Maharashtra", "Outside Maharashtra"})

# ── Institute types (as they appear in the dataset after Title Case) ─────────
VALID_INSTITUTE_TYPES: frozenset[str] = frozenset({
    "Government",
    "Autonomous",
    "Unaided",
    "Aided",
    "University",
    "All",           # sentinel – no filter
})

# ── CAP Round bounds ─────────────────────────────────────────────────────────
CAP_ROUND_MIN: int = 1
CAP_ROUND_MAX: int = 5          # extend if Maharashtra adds more rounds

# ── Percentile bounds ────────────────────────────────────────────────────────
PERCENTILE_MIN: float = 0.0
PERCENTILE_MAX: float = 100.0

# ── Academic year ────────────────────────────────────────────────────────────
ACADEMIC_YEAR_MIN: int = 2000

# ── Column name parts (mirrors merge_data.py _make_col_name convention) ─────
# final_dataset.csv column pattern:  round{N}_stage{S}_{metric}
# e.g. round1_stageI_rank, round2_stageII_percentile
_STAGE_KEYS: dict[str, str] = {
    "Stage-I":   "stageI",
    "Stage-II":  "stageII",
    "Stage-III": "stageIII",
}

# Preference:  Stage-II is the definitive cutoff; Stage-I is the fallback
_STAGE_PREFERENCE: list[str] = ["Stage-II", "Stage-I", "Stage-III"]

# ── Chance bands ─────────────────────────────────────────────────────────────
CHANCE_BANDS: list[tuple[float, str]] = [
    (90.0, "High"),
    (60.0, "Medium"),
    (30.0, "Low"),
    (0.0,  "Very Low"),
]

# ── Maximum results returned to app.py ───────────────────────────────────────
MAX_RESULTS: int = 200

# ── Percentile buffer used to widen the upper-rank search window ─────────────
# A student with 85 % sees colleges whose cutoff is ≤ 85 + BUFFER %
UPPER_BUFFER: float = 2.0

# ── Pivot index columns (must exist in final_dataset.csv) ────────────────────
PIVOT_INDEX_COLS: list[str] = [
    "college_code",
    "college_name",
    "institute_type",
    "choice_code",
    "branch",
    "category",
    "academic_year",
]

# ===========================================================================
# CATEGORY RESOLUTION TABLE
# ===========================================================================
#
# Maharashtra DSE CAP categories follow this pattern:
#
#   Prefix:  G  = State-level (General / Maharashtra domicile outside home-district)
#            L  = Home-district / Local area
#            (For Outside Maharashtra / NRI there is no L prefix)
#
#   Caste code: OPEN, SC, ST, VJA, NT-A … NT-D, OBC, SBC, EWS
#
#   Suffix:  H  = Home-university area (sometimes omitted)
#            (Gender: Ladies seats are a separate suffix in some PDFs but
#             typically encoded as a separate row in CAP data)
#
# The resolver maps (caste, gender, domicile, pwd, defence, ews, orphan)
# → a priority-ordered list of category tokens to try.
#
# We try the most specific category first; if no cutoff exists we fall back
# to the next.  This mirrors the way a student would be allotted a seat.
# ===========================================================================

# Internal caste → raw token used inside category codes
_CASTE_TOKEN: dict[str, str] = {
    "OPEN":  "OPEN",
    "OBC":   "OBC",
    "SC":    "SC",
    "ST":    "ST",
    "VJ":    "VJA",      # Vimukta Jati / Denotified Tribes
    "NT-A":  "NTA",
    "NT-B":  "NTB",
    "NT-C":  "NTC",
    "NT-D":  "NTD",
    "SBC":   "SBC",
    "EWS":   "EWS",
}


def _build_category_priority(
    caste: str,
    gender: str,
    domicile: str,
    pwd: bool,
    defence: bool,
    ews: bool,
    orphan: bool,
) -> list[str]:
    """
    Return an ordered list of category tokens to try, most specific first.

    The function mirrors the Maharashtra CAP seat-allotment logic:
    1. Special quota (PWD, DEF, ORPHAN) – if applicable
    2. Home-district (L prefix) – if Maharashtra domicile
    3. State-level (G prefix)
    4. EWS – if flagged (only for OPEN caste)
    5. TFWS (Tuition Fee Waiver Scheme) – always appended as last resort

    Parameters
    ----------
    caste     : validated caste string from UserInput
    gender    : "Male" / "Female" / "Other"
    domicile  : "Maharashtra" / "Outside Maharashtra"
    pwd, defence, ews, orphan : boolean flags

    Returns
    -------
    list[str]
        Ordered category tokens, e.g. ["LOPENS", "GOPENH", "GOPENH", "EWS"]
    """
    token = _CASTE_TOKEN.get(caste.upper(), caste.upper())
    is_maha = domicile == "Maharashtra"
    is_female = gender == "Female"

    categories: list[str] = []

    # ── Special quotas (tried first) ─────────────────────────────────────────
    if orphan:
        categories.append("ORPHAN")
    if defence:
        categories.append("DEF")
    if pwd:
        categories.append("PWD")

    # ── EWS (only for OPEN caste applicants who flagged EWS) ─────────────────
    if ews and caste.upper() in ("OPEN", "EWS"):
        categories.append("EWS")

    # ── Home-district seats (L prefix) ───────────────────────────────────────
    if is_maha:
        # Ladies home-district
        if is_female:
            categories.append(f"L{token}S")   # e.g. LOPENS, LOBCS
        # General home-district (both genders compete; H = home university)
        categories.append(f"L{token}H")       # e.g. LOPENH, LOBCH
        # Plain local open (some PDFs omit H suffix)
        categories.append(f"L{token}")

    # ── State-level seats (G prefix) ─────────────────────────────────────────
    if is_female:
        categories.append(f"G{token}S")       # e.g. GOPENS, GSCST
    categories.append(f"G{token}H")           # e.g. GOPENH, GSCH
    categories.append(f"G{token}")            # fallback without H

    # ── NRI / Outside Maharashtra ─────────────────────────────────────────────
    if not is_maha:
        categories.append("NRI")

    # ── Last-resort: TFWS (Tuition Fee Waiver – income-based) ────────────────
    categories.append("TFWS")

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for cat in categories:
        if cat not in seen:
            seen.add(cat)
            unique.append(cat)

    return unique


# ===========================================================================
# DATA CLASSES
# ===========================================================================


@dataclass
class UserInput:
    """
    All parameters supplied by the student via app.py.

    Fields
    ------
    percentile         : Student's MHT-CET / DSE qualifying percentile.
    caste              : One of VALID_CASTES.
    gender             : One of VALID_GENDERS.
    domicile           : "Maharashtra" or "Outside Maharashtra".
    preferred_branches : List of branch names.  Empty list = all branches.
    institute_type     : One of VALID_INSTITUTE_TYPES.  "All" = no filter.
    cap_round          : Which CAP round's cutoffs to use (1, 2, 3 …).
    academic_year      : Year of the cutoff data to use (e.g. 2024).
    pwd                : Person with Disability flag.
    defence            : Defence quota flag.
    ews                : Economically Weaker Section flag (OPEN caste only).
    orphan             : Orphan quota flag.
    top_n              : Maximum number of results to return (default 50).
    """

    percentile: float
    caste: str
    gender: str
    domicile: str
    preferred_branches: list[str] = field(default_factory=list)
    institute_type: str = "All"
    cap_round: int = 1
    academic_year: int = 2024
    pwd: bool = False
    defence: bool = False
    ews: bool = False
    orphan: bool = False
    top_n: int = 50


@dataclass
class CollegeResult:
    """
    One predicted college row returned to app.py.

    All numeric fields are Python native types (not numpy) so they are
    directly JSON-serialisable for use in Flask/Jinja2 templates.
    """

    college_code: str
    college_name: str
    institute_type: str
    choice_code: str
    branch: str
    category: str
    cutoff_rank: Optional[int]
    cutoff_percentile: Optional[float]
    stage_used: str          # "Stage-I" / "Stage-II" / "Stage-III"
    round_used: int
    academic_year: int
    admission_chance: float  # 0–100
    chance_label: str        # "High" / "Medium" / "Low" / "Very Low"

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict suitable for Jinja2 / JSON serialisation."""
        return {
            "college_code":      self.college_code,
            "college_name":      self.college_name,
            "institute_type":    self.institute_type,
            "choice_code":       self.choice_code,
            "branch":            self.branch,
            "category":          self.category,
            "cutoff_rank":       self.cutoff_rank,
            "cutoff_percentile": self.cutoff_percentile,
            "stage_used":        self.stage_used,
            "round_used":        self.round_used,
            "academic_year":     self.academic_year,
            "admission_chance":  round(self.admission_chance, 2),
            "chance_label":      self.chance_label,
        }


@dataclass
class PredictionResult:
    """
    Top-level object returned by CollegePredictor.predict() to app.py.

    Fields
    ------
    success       : False when validation failed or a fatal error occurred.
    colleges      : Ordered list of CollegeResult objects (best chance first).
    errors        : User-facing validation / data error messages.
    warnings      : Non-fatal notices (e.g. "No Stage-II data; used Stage-I").
    stats         : Diagnostic counts for the admin/debug view.
    elapsed_ms    : Wall-clock time for the prediction in milliseconds.
    """

    success: bool
    colleges: list[CollegeResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialise for JSON / template rendering."""
        return {
            "success":    self.success,
            "colleges":   [c.to_dict() for c in self.colleges],
            "errors":     self.errors,
            "warnings":   self.warnings,
            "stats":      self.stats,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


# ===========================================================================
# EXCEPTIONS
# ===========================================================================


class ValidationError(Exception):
    """Raised by InputValidator when one or more input fields are invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class DatasetError(Exception):
    """Raised when final_dataset.csv cannot be loaded or is malformed."""


# ===========================================================================
# INPUT VALIDATOR
# ===========================================================================


class InputValidator:
    """
    Validates and normalises a UserInput object.

    All user-facing error messages are collected before raising so the form
    can display every problem at once (not just the first one).
    """

    def validate(self, ui: UserInput) -> UserInput:
        """
        Validate *ui* in-place (normalise strings) and return it.

        Raises
        ------
        ValidationError
            When one or more fields are invalid.
        """
        errors: list[str] = []

        # ── Percentile ────────────────────────────────────────────────────────
        if not isinstance(ui.percentile, (int, float)):
            errors.append("Percentile must be a number.")
        elif not (PERCENTILE_MIN <= ui.percentile <= PERCENTILE_MAX):
            errors.append(
                f"Percentile must be between {PERCENTILE_MIN} and "
                f"{PERCENTILE_MAX}.  Got {ui.percentile}."
            )

        # ── Caste ─────────────────────────────────────────────────────────────
        ui.caste = ui.caste.strip().upper() if isinstance(ui.caste, str) else ""
        if ui.caste not in {c.upper() for c in VALID_CASTES}:
            errors.append(
                f"Invalid caste '{ui.caste}'. "
                f"Valid options: {sorted(VALID_CASTES)}."
            )

        # ── Gender ────────────────────────────────────────────────────────────
        ui.gender = ui.gender.strip().title() if isinstance(ui.gender, str) else ""
        if ui.gender not in VALID_GENDERS:
            errors.append(
                f"Invalid gender '{ui.gender}'. "
                f"Valid options: {sorted(VALID_GENDERS)}."
            )

        # ── Domicile ──────────────────────────────────────────────────────────
        ui.domicile = (
            ui.domicile.strip().title() if isinstance(ui.domicile, str) else ""
        )
        if ui.domicile not in VALID_DOMICILES:
            errors.append(
                f"Invalid domicile '{ui.domicile}'. "
                f"Valid options: {sorted(VALID_DOMICILES)}."
            )

        # ── Institute type ────────────────────────────────────────────────────
        ui.institute_type = (
            ui.institute_type.strip().title()
            if isinstance(ui.institute_type, str)
            else "All"
        )
        if ui.institute_type not in VALID_INSTITUTE_TYPES:
            errors.append(
                f"Invalid institute type '{ui.institute_type}'. "
                f"Valid options: {sorted(VALID_INSTITUTE_TYPES)}."
            )

        # ── CAP round ─────────────────────────────────────────────────────────
        if not isinstance(ui.cap_round, int) or not (
            CAP_ROUND_MIN <= ui.cap_round <= CAP_ROUND_MAX
        ):
            errors.append(
                f"CAP round must be an integer between {CAP_ROUND_MIN} and "
                f"{CAP_ROUND_MAX}.  Got {ui.cap_round!r}."
            )

        # ── Academic year ─────────────────────────────────────────────────────
        if not isinstance(ui.academic_year, int) or ui.academic_year < ACADEMIC_YEAR_MIN:
            errors.append(
                f"Academic year must be an integer ≥ {ACADEMIC_YEAR_MIN}.  "
                f"Got {ui.academic_year!r}."
            )

        # ── Boolean flags ─────────────────────────────────────────────────────
        for flag_name in ("pwd", "defence", "ews", "orphan"):
            val = getattr(ui, flag_name)
            if not isinstance(val, bool):
                errors.append(f"'{flag_name}' must be True or False.")

        # ── EWS sanity check ──────────────────────────────────────────────────
        if ui.ews and ui.caste.upper() not in ("OPEN", "EWS"):
            errors.append(
                "EWS flag is only applicable to OPEN caste applicants."
            )

        # ── Preferred branches ────────────────────────────────────────────────
        if not isinstance(ui.preferred_branches, list):
            errors.append("preferred_branches must be a list of strings.")
        else:
            ui.preferred_branches = [
                str(b).strip() for b in ui.preferred_branches if str(b).strip()
            ]

        # ── top_n ─────────────────────────────────────────────────────────────
        if not isinstance(ui.top_n, int) or ui.top_n < 1:
            ui.top_n = 50
        ui.top_n = min(ui.top_n, MAX_RESULTS)

        if errors:
            raise ValidationError(errors)

        return ui


# ===========================================================================
# DATASET LOADER  (module-level singleton cache)
# ===========================================================================

_DATASET_CACHE: Optional[pd.DataFrame] = None
_DATASET_MTIME: float = 0.0        # modification-time of the CSV at last load


class DatasetLoader:
    """
    Loads final_dataset.csv into a module-level cache.

    The cache is invalidated automatically when the file's mtime changes
    (e.g. after a fresh pipeline run), so the app does not need to restart.

    Thread safety: single-threaded Flask is safe; for multi-threaded use
    wrap _load() in a lock.
    """

    def get(self) -> pd.DataFrame:
        """
        Return the cached DataFrame, reloading if the file has changed.

        Raises
        ------
        DatasetError
            When the file is missing or cannot be parsed.
        """
        global _DATASET_CACHE, _DATASET_MTIME

        if not FINAL_DATASET_PATH.exists():
            raise DatasetError(
                f"final_dataset.csv not found at {FINAL_DATASET_PATH}. "
                "Run the full pipeline first:  extract_pdf → clean_data → merge_data."
            )

        current_mtime = FINAL_DATASET_PATH.stat().st_mtime
        if _DATASET_CACHE is not None and current_mtime == _DATASET_MTIME:
            log.debug("Dataset cache hit (%d rows).", len(_DATASET_CACHE))
            return _DATASET_CACHE

        log.info("Loading dataset from %s …", FINAL_DATASET_PATH)
        t0 = time.perf_counter()

        try:
            df = pd.read_csv(
                FINAL_DATASET_PATH,
                dtype=str,          # keep everything as str initially
                keep_default_na=True,
                low_memory=False,
            )
        except Exception as exc:
            raise DatasetError(
                f"Cannot read final_dataset.csv: {exc}"
            ) from exc

        # Validate pivot-index columns are present
        missing = [c for c in PIVOT_INDEX_COLS if c not in df.columns]
        if missing:
            raise DatasetError(
                f"final_dataset.csv is missing columns: {missing}. "
                "Re-run merge_data.py."
            )

        # Coerce academic_year to int for filtering
        df["academic_year"] = pd.to_numeric(
            df["academic_year"], errors="coerce"
        ).astype("Int64")

        _DATASET_CACHE = df
        _DATASET_MTIME = current_mtime
        elapsed = (time.perf_counter() - t0) * 1000
        log.info(
            "Dataset loaded: %d rows × %d cols in %.1f ms.",
            len(df), len(df.columns), elapsed,
        )
        return df


# ===========================================================================
# CATEGORY RESOLVER
# ===========================================================================


class CategoryResolver:
    """
    Translates UserInput flags into an ordered list of category tokens AND
    maps those tokens to the actual column names present in final_dataset.csv.
    """

    def resolve(
        self,
        ui: UserInput,
        df: pd.DataFrame,
        round_num: int,
    ) -> dict[str, tuple[str, str]]:
        """
        For each candidate category token, find matching rank and percentile
        columns in *df* for the given *round_num*.

        Parameters
        ----------
        ui        : Validated UserInput.
        df        : final_dataset.csv as a DataFrame.
        round_num : The CAP round to look up.

        Returns
        -------
        dict mapping  category_token → (rank_col, percentile_col)

        Only tokens that have at least one non-null rank column in *df* are
        included.  Ordered by priority (most specific first).
        """
        tokens = _build_category_priority(
            caste=ui.caste,
            gender=ui.gender,
            domicile=ui.domicile,
            pwd=ui.pwd,
            defence=ui.defence,
            ews=ui.ews,
            orphan=ui.orphan,
        )
        log.debug("Category priority list: %s", tokens)

        resolved: dict[str, tuple[str, str]] = {}

        for token in tokens:
            rank_col, pct_col = self._find_columns(token, round_num, df)
            if rank_col:
                resolved[token] = (rank_col, pct_col)

        log.debug(
            "Resolved %d category→column mapping(s): %s",
            len(resolved), list(resolved.keys()),
        )
        return resolved

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _find_columns(
        category_token: str,
        round_num: int,
        df: pd.DataFrame,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Return (rank_col, percentile_col) for *category_token* in *round_num*.

        Prefers Stage-II over Stage-I over Stage-III.  Returns (None, None)
        when no matching column exists in the DataFrame.
        """
        for stage_label in _STAGE_PREFERENCE:
            stage_key = _STAGE_KEYS.get(stage_label, "")
            rank_col = f"round{round_num}_{stage_key}_rank"
            pct_col  = f"round{round_num}_{stage_key}_percentile"
            if rank_col in df.columns:
                # Check that this column has at least some non-null data for
                # this specific category (so we don't return ghost columns)
                cat_mask = df["category"].str.upper() == category_token.upper()
                if cat_mask.any() and df.loc[cat_mask, rank_col].notna().any():
                    return rank_col, pct_col if pct_col in df.columns else None
        return None, None


# ===========================================================================
# FILTER ENGINE
# ===========================================================================


class FilterEngine:
    """
    Applies all user preferences to the dataset and returns a filtered
    DataFrame containing only the rows relevant to the student's query.
    """

    def filter(
        self,
        df: pd.DataFrame,
        ui: UserInput,
        category_col_map: dict[str, tuple[str, str]],
        warnings: list[str],
    ) -> pd.DataFrame:
        """
        Apply filters sequentially.  Each step narrows the DataFrame.

        Parameters
        ----------
        df               : Full final_dataset.csv DataFrame.
        ui               : Validated UserInput.
        category_col_map : Output of CategoryResolver.resolve().
        warnings         : Mutable list – append non-fatal notices here.

        Returns
        -------
        pd.DataFrame
            Filtered rows; may be empty (caller handles gracefully).
        """
        original_len = len(df)

        # ── Step 1: Academic year ─────────────────────────────────────────────
        df = df[df["academic_year"] == ui.academic_year]
        log.debug(
            "After year=%d filter: %d / %d rows.",
            ui.academic_year, len(df), original_len,
        )
        if df.empty:
            warnings.append(
                f"No data found for academic year {ui.academic_year}. "
                "Try a different year."
            )
            return df

        # ── Step 2: Institute type ────────────────────────────────────────────
        if ui.institute_type != "All":
            before = len(df)
            df = df[
                df["institute_type"].str.strip().str.title() == ui.institute_type
            ]
            log.debug(
                "After institute_type='%s' filter: %d / %d rows.",
                ui.institute_type, len(df), before,
            )
            if df.empty:
                warnings.append(
                    f"No colleges of type '{ui.institute_type}' found. "
                    "Try 'All'."
                )
                return df

        # ── Step 3: Branch preferences ────────────────────────────────────────
        if ui.preferred_branches:
            before = len(df)
            pattern = "|".join(
                branch.replace("(", r"\(").replace(")", r"\)")
                for branch in ui.preferred_branches
            )
            df = df[
                df["branch"].str.contains(pattern, case=False, na=False, regex=True)
            ]
            log.debug(
                "After branch filter (%s): %d / %d rows.",
                ui.preferred_branches, len(df), before,
            )
            if df.empty:
                warnings.append(
                    f"No data found for branches: {ui.preferred_branches}. "
                    "Try removing branch filters."
                )
                return df

        # ── Step 4: Category filter ───────────────────────────────────────────
        if not category_col_map:
            warnings.append(
                "Could not resolve any valid category columns for your profile. "
                "Please check caste / domicile / round combination."
            )
            return pd.DataFrame()

        valid_cats = set(category_col_map.keys())
        before = len(df)
        df = df[df["category"].str.upper().isin({c.upper() for c in valid_cats})]
        log.debug(
            "After category filter %s: %d / %d rows.",
            valid_cats, len(df), before,
        )
        if df.empty:
            warnings.append(
                "No cutoff data found for your category/reservation combination. "
                "Try a different caste or domicile."
            )
            return df

        return df.copy()


# ===========================================================================
# CHANCE CALCULATOR
# ===========================================================================


class ChanceCalculator:
    """
    Computes an admission probability score (0–100) for every row.

    Scoring formula
    ---------------
    The primary signal is the gap between the student's percentile and the
    cutoff percentile for that seat.

    If cutoff data is missing for percentile but rank is available, we
    estimate a synthetic percentile from the rank using a monotone decay
    curve fitted to the observed data range.

    Gap  = student_percentile - cutoff_percentile
    score = sigmoid-like mapping of gap to [0, 100]

    The bands:
        gap ≥ +5     → 95–100  (very safe)
        gap  0..+5   → 70–95   (safe)
        gap -3..0    → 45–70   (borderline)
        gap -8..-3   → 15–45   (risky)
        gap < -8     → 0–15    (very risky)

    We also apply a small bonus/penalty for:
        + Stage-I used instead of Stage-II  (cutoff may tighten in Stage-II)
        + Multiple category options resolved (student has more seats available)
    """

    def score(
        self,
        student_percentile: float,
        cutoff_percentile: Optional[float],
        cutoff_rank: Optional[int],
        stage_used: str,
        n_categories: int,
    ) -> float:
        """
        Return a chance score in [0.0, 100.0].

        Parameters
        ----------
        student_percentile : Student's qualifying percentile.
        cutoff_percentile  : Last admitted percentile (NaN = unknown).
        cutoff_rank        : Last admitted rank (used if percentile is NaN).
        stage_used         : "Stage-I" / "Stage-II" / "Stage-III".
        n_categories       : Number of eligible categories resolved.
        """
        pct = self._resolve_percentile(
            cutoff_percentile, cutoff_rank, student_percentile
        )
        if pct is None:
            # No cutoff data at all – cannot score
            return 50.0      # neutral / unknown

        gap = student_percentile - pct

        # ── Core gap-to-score mapping ─────────────────────────────────────────
        if gap >= 5.0:
            score = 90.0 + min(gap - 5.0, 5.0) * 2.0      # 90–100
        elif gap >= 0.0:
            score = 70.0 + gap * 4.0                        # 70–90
        elif gap >= -3.0:
            score = 45.0 + (gap + 3.0) * (25.0 / 3.0)     # 45–70
        elif gap >= -8.0:
            score = 15.0 + (gap + 8.0) * 6.0               # 15–45
        else:
            score = max(0.0, 15.0 + gap * 1.5)             # 0–15

        # ── Stage adjustment ──────────────────────────────────────────────────
        # Stage-I tends to have higher (easier) cutoffs than Stage-II.
        # Seeing Stage-I only means Stage-II may be harder → small penalty.
        if stage_used == "Stage-I":
            score = max(0.0, score - 5.0)
        elif stage_used == "Stage-III":
            # Stage-III is a final mopping-up round; cutoffs tend to ease
            score = min(100.0, score + 3.0)

        # ── Category bonus ────────────────────────────────────────────────────
        # More eligible categories → student has more options
        if n_categories >= 3:
            score = min(100.0, score + 2.0)

        return round(min(100.0, max(0.0, score)), 2)

    @staticmethod
    def _resolve_percentile(
        cutoff_pct: Optional[float],
        cutoff_rank: Optional[int],
        student_pct: float,
    ) -> Optional[float]:
        """
        Return the best available cutoff percentile estimate.

        When *cutoff_pct* is NaN but *cutoff_rank* is available, we apply
        a rough linear approximation:
            estimated_pct ≈ 100 × (1 − rank / 500_000)
        (500_000 is the approximate total DSE rank pool)
        """
        if cutoff_pct is not None and not np.isnan(float(cutoff_pct)):
            return float(cutoff_pct)
        if cutoff_rank is not None and not pd.isna(cutoff_rank):
            rank = int(cutoff_rank)
            if rank > 0:
                return max(0.0, 100.0 - (rank / 500_000) * 100.0)
        return None

    @staticmethod
    def label(score: float) -> str:
        """Map a numeric score to a human-readable chance label."""
        for threshold, band in CHANCE_BANDS:
            if score >= threshold:
                return band
        return "Very Low"


# ===========================================================================
# RESULT BUILDER
# ===========================================================================


class ResultBuilder:
    """
    Converts scored DataFrame rows into a sorted list of CollegeResult objects.
    """

    def __init__(self) -> None:
        self._calculator = ChanceCalculator()

    def build(
        self,
        df: pd.DataFrame,
        ui: UserInput,
        category_col_map: dict[str, tuple[str, str]],
        warnings: list[str],
    ) -> list[CollegeResult]:
        """
        Iterate over *df*, compute a chance score for each row, and return
        CollegeResult objects sorted by admission_chance descending.

        Parameters
        ----------
        df               : Filtered DataFrame (output of FilterEngine).
        ui               : Validated UserInput.
        category_col_map : category_token → (rank_col, pct_col).
        warnings         : Mutable list for non-fatal notices.

        Returns
        -------
        list[CollegeResult]
            Sorted best-first, capped at ui.top_n.
        """
        if df.empty:
            return []

        results: list[CollegeResult] = []
        n_cats = len(category_col_map)

        for _, row in df.iterrows():
            cat_token = str(row.get("category", "")).strip().upper()

            # Find the column mapping for this row's category
            matched_map: Optional[tuple[str, str]] = None
            for token, cols in category_col_map.items():
                if token.upper() == cat_token:
                    matched_map = cols
                    break

            if matched_map is None:
                continue

            rank_col, pct_col = matched_map

            # Determine which stage the column belongs to
            stage_used = self._stage_from_col(rank_col)

            # Extract numeric values safely
            cutoff_rank = self._safe_int(row.get(rank_col))
            cutoff_pct  = self._safe_float(row.get(pct_col) if pct_col else None)

            # Apply upper-buffer: only show colleges where student stands a
            # realistic chance (student percentile ≥ cutoff − 10 %)
            if cutoff_pct is not None and not np.isnan(cutoff_pct):
                if ui.percentile < cutoff_pct - 10.0:
                    continue    # well out of range – skip

            score = self._calculator.score(
                student_percentile=ui.percentile,
                cutoff_percentile=cutoff_pct,
                cutoff_rank=cutoff_rank,
                stage_used=stage_used,
                n_categories=n_cats,
            )

            results.append(
                CollegeResult(
                    college_code=str(row.get("college_code", "")).strip(),
                    college_name=str(row.get("college_name", "")).strip(),
                    institute_type=str(row.get("institute_type", "")).strip(),
                    choice_code=str(row.get("choice_code", "")).strip(),
                    branch=str(row.get("branch", "")).strip(),
                    category=cat_token,
                    cutoff_rank=cutoff_rank,
                    cutoff_percentile=(
                        round(cutoff_pct, 4) if cutoff_pct is not None else None
                    ),
                    stage_used=stage_used,
                    round_used=ui.cap_round,
                    academic_year=int(
                        row.get("academic_year", ui.academic_year) or ui.academic_year
                    ),
                    admission_chance=score,
                    chance_label=self._calculator.label(score),
                )
            )

        # Sort: highest chance first; then alphabetically by college name
        results.sort(key=lambda r: (-r.admission_chance, r.college_name))

        # Warn if results were trimmed
        if len(results) > ui.top_n:
            warnings.append(
                f"Showing top {ui.top_n} of {len(results)} matching colleges."
            )

        return results[: ui.top_n]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stage_from_col(col_name: str) -> str:
        """
        Infer the stage label from a column name like 'round1_stageII_rank'.

        Returns "Stage-I" as default.
        """
        col_lower = col_name.lower()
        if "stageiii" in col_lower:
            return "Stage-III"
        if "stageii" in col_lower:
            return "Stage-II"
        return "Stage-I"

    @staticmethod
    def _safe_int(val: Any) -> Optional[int]:
        """Convert *val* to int, returning None on failure."""
        try:
            if val is None or (isinstance(val, float) and np.isnan(val)):
                return None
            return int(float(val))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(val: Any) -> Optional[float]:
        """Convert *val* to float, returning None on failure."""
        try:
            if val is None:
                return None
            f = float(val)
            return None if np.isnan(f) else f
        except (TypeError, ValueError):
            return None


# ===========================================================================
# COLLEGE PREDICTOR  (public façade)
# ===========================================================================


class CollegePredictor:
    """
    Public API consumed directly by app.py.

    Example
    -------
    ::

        predictor = CollegePredictor()
        result = predictor.predict(UserInput(
            percentile=85.5,
            caste="OBC",
            gender="Female",
            domicile="Maharashtra",
            preferred_branches=["Computer Engineering"],
            institute_type="Autonomous",
            cap_round=2,
            academic_year=2024,
        ))
        if result.success:
            for college in result.colleges:
                print(college.to_dict())

    The predictor is safe to instantiate once at module load and reuse across
    all requests (the dataset is cached at module level).
    """

    def __init__(self) -> None:
        self._loader    = DatasetLoader()
        self._validator = InputValidator()
        self._resolver  = CategoryResolver()
        self._filter    = FilterEngine()
        self._builder   = ResultBuilder()

    # ------------------------------------------------------------------
    # Public method
    # ------------------------------------------------------------------

    def predict(self, ui: UserInput) -> PredictionResult:
        """
        Run the full prediction pipeline for one student query.

        Parameters
        ----------
        ui : UserInput
            Raw (not yet validated) input from app.py.

        Returns
        -------
        PredictionResult
            Always returns a result object; never raises.
            On failure, PredictionResult.success is False and
            PredictionResult.errors contains user-facing messages.
        """
        t_start = time.perf_counter()
        warnings: list[str] = []
        stats: dict[str, Any] = {}

        # ── 1. Validate input ─────────────────────────────────────────────────
        try:
            ui = self._validator.validate(ui)
        except ValidationError as exc:
            return PredictionResult(
                success=False,
                errors=exc.errors,
                elapsed_ms=(time.perf_counter() - t_start) * 1000,
            )

        log.info(
            "Prediction request: percentile=%.2f caste=%s gender=%s "
            "domicile=%s round=%d year=%d branches=%s",
            ui.percentile, ui.caste, ui.gender, ui.domicile,
            ui.cap_round, ui.academic_year, ui.preferred_branches,
        )

        # ── 2. Load dataset ───────────────────────────────────────────────────
        try:
            df = self._loader.get()
        except DatasetError as exc:
            log.error("Dataset load error: %s", exc)
            return PredictionResult(
                success=False,
                errors=[str(exc)],
                elapsed_ms=(time.perf_counter() - t_start) * 1000,
            )

        stats["dataset_rows"] = len(df)

        # ── 3. Resolve categories → column names ──────────────────────────────
        category_col_map = self._resolver.resolve(ui, df, ui.cap_round)

        if not category_col_map:
            msg = (
                f"No cutoff data columns found for round {ui.cap_round} "
                f"and your category profile.  "
                f"Available rounds in dataset: {self._available_rounds(df)}."
            )
            log.warning(msg)
            return PredictionResult(
                success=False,
                errors=[msg],
                elapsed_ms=(time.perf_counter() - t_start) * 1000,
            )

        stats["categories_resolved"] = list(category_col_map.keys())

        # ── 4. Filter dataset ─────────────────────────────────────────────────
        filtered = self._filter.filter(df, ui, category_col_map, warnings)
        stats["rows_after_filter"] = len(filtered)

        if filtered.empty:
            elapsed = (time.perf_counter() - t_start) * 1000
            return PredictionResult(
                success=True,    # not a crash – just no matches
                colleges=[],
                warnings=warnings or [
                    "No colleges matched your criteria. "
                    "Try broadening your filters."
                ],
                stats=stats,
                elapsed_ms=elapsed,
            )

        # ── 5. Score and build results ─────────────────────────────────────────
        colleges = self._builder.build(filtered, ui, category_col_map, warnings)
        stats["colleges_returned"] = len(colleges)

        elapsed_ms = (time.perf_counter() - t_start) * 1000
        log.info(
            "Prediction complete: %d colleges in %.1f ms.",
            len(colleges), elapsed_ms,
        )

        return PredictionResult(
            success=True,
            colleges=colleges,
            warnings=warnings,
            stats=stats,
            elapsed_ms=elapsed_ms,
        )

    # ------------------------------------------------------------------
    # Utility methods (usable by app.py for dropdowns / metadata)
    # ------------------------------------------------------------------

    def available_branches(self, academic_year: Optional[int] = None) -> list[str]:
        """
        Return a sorted list of all unique branch names in the dataset.

        Optionally filtered by *academic_year*.
        Used to populate the branch dropdown in app.py.
        """
        try:
            df = self._loader.get()
        except DatasetError:
            return []

        if academic_year is not None:
            df = df[df["academic_year"] == academic_year]

        branches = (
            df["branch"].dropna().str.strip().unique().tolist()
        )
        return sorted(branches)

    def available_years(self) -> list[int]:
        """
        Return a sorted list of all academic years present in the dataset.

        Used to populate the year dropdown in app.py.
        """
        try:
            df = self._loader.get()
        except DatasetError:
            return []

        years = (
            pd.to_numeric(df["academic_year"], errors="coerce")
            .dropna()
            .astype(int)
            .unique()
            .tolist()
        )
        return sorted(years)

    def available_rounds(self, academic_year: Optional[int] = None) -> list[int]:
        """
        Return a sorted list of CAP round numbers present in the dataset
        for *academic_year* (or all years if None).
        """
        try:
            df = self._loader.get()
        except DatasetError:
            return []
        return self._available_rounds(df, academic_year)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _available_rounds(
        df: pd.DataFrame,
        academic_year: Optional[int] = None,
    ) -> list[int]:
        """
        Infer available rounds from the pivot column names in *df*.

        Column names follow: round{N}_stage{S}_{metric}
        We scan for distinct N values.
        """
        import re
        rounds: set[int] = set()
        pattern = re.compile(r"^round(\d+)_stage")
        for col in df.columns:
            m = pattern.match(col)
            if m:
                rounds.add(int(m.group(1)))
        return sorted(rounds)


# ===========================================================================
# CONVENIENCE FUNCTION  (for app.py one-liner usage)
# ===========================================================================

_PREDICTOR_SINGLETON: Optional[CollegePredictor] = None


def get_predictor() -> CollegePredictor:
    """
    Return the module-level singleton CollegePredictor.

    app.py should call this once at startup and reuse the instance.

    Example
    -------
    ::

        from predictor.predictor import get_predictor, UserInput

        predictor = get_predictor()
        result = predictor.predict(UserInput(...))
    """
    global _PREDICTOR_SINGLETON
    if _PREDICTOR_SINGLETON is None:
        _PREDICTOR_SINGLETON = CollegePredictor()
    return _PREDICTOR_SINGLETON


# ===========================================================================
# CLI SMOKE-TEST
# ===========================================================================

def _smoke_test() -> None:
    """
    Quick sanity check runnable from the command line.

    Usage::

        python predictor/predictor.py
    """
    log.info("Running smoke test …")

    predictor = get_predictor()

    # Print available metadata
    print(f"\nAvailable years  : {predictor.available_years()}")
    print(f"Available rounds : {predictor.available_rounds()}")
    print(f"Sample branches  : {predictor.available_branches()[:5]}\n")

    sample_input = UserInput(
        percentile=82.5,
        caste="OBC",
        gender="Male",
        domicile="Maharashtra",
        preferred_branches=[],   # all branches
        institute_type="All",
        cap_round=1,
        academic_year=2024,
        pwd=False,
        defence=False,
        ews=False,
        orphan=False,
        top_n=10,
    )

    result = predictor.predict(sample_input)

    if not result.success:
        print("ERRORS:")
        for e in result.errors:
            print(f"  {e}")
        return

    if result.warnings:
        print("WARNINGS:")
        for w in result.warnings:
            print(f"  {w}")

    print(f"\nStats: {result.stats}")
    print(f"Elapsed: {result.elapsed_ms:.1f} ms")
    print(f"\nTop {len(result.colleges)} college(s):\n")

    header = (
        f"{'#':<4} {'College':<50} {'Branch':<35} "
        f"{'Cat':<10} {'Cutoff%':<10} {'Chance%':<10} {'Label'}"
    )
    print(header)
    print("-" * len(header))

    for i, c in enumerate(result.colleges, 1):
        cutoff = f"{c.cutoff_percentile:.2f}" if c.cutoff_percentile else "N/A"
        print(
            f"{i:<4} {c.college_name[:49]:<50} {c.branch[:34]:<35} "
            f"{c.category:<10} {cutoff:<10} {c.admission_chance:<10.2f} {c.chance_label}"
        )


if __name__ == "__main__":
    _smoke_test()