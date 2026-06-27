"""
parser/clean_data.py
====================
Production-ready data cleaner for Maharashtra DSE CAP Round cutoff CSVs.

Responsibilities
----------------
- Discover every raw cap_round_*.csv in data/processed/
- Apply a deterministic, auditable cleaning pipeline to each file
- Write cleaned output as clean_cap_round_*.csv in data/processed/

NOT responsible for
-------------------
- PDF parsing           → extract_pdf.py
- Merging cleaned rounds → merge_data.py

Cleaning pipeline (in order)
-----------------------------
1.  Schema validation      – assert expected columns are present
2.  Whitespace normalisation – strip & collapse all string fields
3.  College name casing     – Title Case with protected abbreviations
4.  Branch name casing      – Title Case with protected abbreviations
5.  Institute type casing   – Title Case
6.  Category normalisation  – UPPER, strip trailing/leading junk
7.  Stage normalisation     – canonical "Stage-I" / "Stage-II" form
8.  Numeric coercion        – rank → int, percentile → float
9.  Range validation        – rank ≥ 1, 0 ≤ percentile ≤ 100
10. College code padding    – zero-pad to 4 digits
11. Choice code padding     – zero-pad to 9 digits
12. Round / year validation – positive integers, year ≥ 2000
13. Duplicate removal       – drop exact duplicate rows
14. Missing-value audit     – log counts of NaN per column
15. Sort                    – college_code, choice_code, category, stage

Usage
-----
    python parser/clean_data.py

Author : MAHA-DSE Project
Python : 3.11+
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import pandas as pd
    from tqdm import tqdm
except ImportError as exc:
    sys.exit(
        f"[FATAL] Missing dependency: {exc}. "
        "Run:  pip install pandas tqdm"
    )

# ===========================================================================
# CONSTANTS
# ===========================================================================

_ROOT: Path = Path(__file__).resolve().parent.parent
PROCESSED_DIR: Path = _ROOT / "data" / "processed"
LOG_DIR: Path = _ROOT / "logs"
LOG_FILE: Path = LOG_DIR / "clean_data.log"

# Input glob – matches cap_round_1.csv, cap_round_2.csv, cap_round_10.csv …
RAW_GLOB: str = "cap_round_*.csv"

# Output prefix
CLEAN_PREFIX: str = "clean_cap_round_"

# Columns that must exist in every raw CSV (matches extract_pdf.py CSV_COLUMNS)
REQUIRED_COLUMNS: list[str] = [
    "college_code",
    "college_name",
    "institute_type",
    "choice_code",
    "branch",
    "stage",
    "category",
    "rank",
    "percentile",
    "round",
    "academic_year",
]

# String columns whose whitespace will be normalised
STRING_COLUMNS: list[str] = [
    "college_name",
    "institute_type",
    "branch",
    "stage",
    "category",
]

# Columns used for final sort
SORT_COLUMNS: list[str] = [
    "college_code",
    "choice_code",
    "category",
    "stage",
]

# ── Rank / percentile bounds ─────────────────────────────────────────────────
RANK_MIN: int = 1
RANK_MAX: int = 500_000        # Upper safety limit (adjust if needed)
PERCENTILE_MIN: float = 0.0
PERCENTILE_MAX: float = 100.0
YEAR_MIN: int = 2000

# ── Padding widths ───────────────────────────────────────────────────────────
COLLEGE_CODE_WIDTH: int = 4
CHOICE_CODE_WIDTH: int = 9

# ── Stage canonical forms ────────────────────────────────────────────────────
# Maps any observed variant → canonical string
STAGE_CANON: dict[str, str] = {
    "stage-i":   "Stage-I",
    "stage i":   "Stage-I",
    "stagei":    "Stage-I",
    "stage1":    "Stage-I",
    "stage-1":   "Stage-I",
    "stage-ii":  "Stage-II",
    "stage ii":  "Stage-II",
    "stageii":   "Stage-II",
    "stage2":    "Stage-II",
    "stage-2":   "Stage-II",
    "stage-iii": "Stage-III",
    "stage iii": "Stage-III",
    "stageiii":  "Stage-III",
    "stage3":    "Stage-III",
    "stage-3":   "Stage-III",
}

# ── Title-case abbreviation protection ───────────────────────────────────────
# Words that must NOT be lower-cased when applying Title Case.
# Regex: match as whole word, case-insensitive.
_PROTECTED_UPPER: list[str] = [
    "IT", "AI", "ML", "IOT", "VLSI", "EXTC", "ETRX",
    "CSE", "ECE", "EEE", "ETE", "ICT", "AIDS",
    "CAD", "CAM", "CNC", "PLC",
    "BE", "ME", "MTech", "BTech",
]

_PROTECTED_LOWER: list[str] = [
    "and", "of", "in", "for", "the", "a", "an",
    "with", "at", "by", "to",
]

# Pre-compiled pattern for protected upper words (whole-word, case-insensitive)
_RE_PROTECTED_UPPER: re.Pattern = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _PROTECTED_UPPER) + r")\b",
    re.IGNORECASE,
)

# ===========================================================================
# LOGGING
# ===========================================================================


def configure_logging() -> logging.Logger:
    """Configure file + console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("clean_data")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)

    return logger


# ===========================================================================
# UTILITY FUNCTIONS
# ===========================================================================


def _discover_raw_csvs(directory: Path) -> list[Path]:
    """
    Return all raw cap_round_*.csv files sorted by round number.

    Raises
    ------
    FileNotFoundError
        When *directory* does not exist.
    """
    if not directory.exists():
        raise FileNotFoundError(
            f"Processed directory not found: {directory}. "
            "Run extract_pdf.py first."
        )

    files = sorted(
        directory.glob(RAW_GLOB),
        key=lambda p: _round_from_filename(p.stem),
    )
    # Exclude already-cleaned files (they also match the glob via prefix)
    files = [f for f in files if not f.stem.startswith(CLEAN_PREFIX.rstrip("_"))]
    return files


def _round_from_filename(stem: str) -> int:
    """
    Extract the round number from a filename stem like 'cap_round_2'.

    Returns 0 on failure (keeps sort stable).
    """
    m = re.search(r"(\d+)$", stem)
    return int(m.group(1)) if m else 0


def _output_path(raw_path: Path) -> Path:
    """
    Compute the output path for a cleaned CSV.

    Example
    -------
    raw_path = .../data/processed/cap_round_1.csv
    → .../data/processed/clean_cap_round_1.csv
    """
    round_num = _round_from_filename(raw_path.stem)
    return raw_path.parent / f"{CLEAN_PREFIX}{round_num}.csv"


def _smart_title(text: str) -> str:
    """
    Apply Title Case to *text* while:

    - Preserving abbreviations (CSE, IT, VLSI …) in UPPER.
    - Keeping small connector words (of, and, in …) in lower.
    - Always capitalising the first word.

    Parameters
    ----------
    text : str
        Raw string from the PDF (may be ALL CAPS or mixed).

    Returns
    -------
    str
        Human-readable Title Case string.
    """
    if not text:
        return text

    words = text.split()
    result: list[str] = []

    for i, word in enumerate(words):
        upper = word.upper()
        lower = word.lower()

        if upper in {w.upper() for w in _PROTECTED_UPPER}:
            # Restore the canonical abbreviation form
            canon = next(
                (w for w in _PROTECTED_UPPER if w.upper() == upper), upper
            )
            result.append(canon)
        elif i > 0 and lower in _PROTECTED_LOWER:
            result.append(lower)
        else:
            result.append(word.capitalize())

    return " ".join(result)


def _collapse_whitespace(value: str) -> str:
    """Replace runs of whitespace (including newlines/tabs) with a single space."""
    return re.sub(r"\s+", " ", value).strip()


# ===========================================================================
# CLEANING STEPS
# ===========================================================================
# Each step is a pure function  (df, logger) → df
# so the pipeline is trivially reorderable and testable.
# ===========================================================================

CleanStep = Callable[[pd.DataFrame, logging.Logger], pd.DataFrame]


def step_validate_schema(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Assert that all required columns are present.

    Raises
    ------
    ValueError
        On missing columns (hard stop – cannot clean without schema).
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Raw CSV is missing required columns: {missing}. "
            "Re-run extract_pdf.py to regenerate."
        )
    logger.debug("Schema OK – all %d required columns present.", len(REQUIRED_COLUMNS))
    return df


def step_normalise_whitespace(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Strip and collapse internal whitespace in all string columns."""
    for col in STRING_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype(str).apply(_collapse_whitespace)
    logger.debug("Whitespace normalised for columns: %s", STRING_COLUMNS)
    return df


def step_college_name_casing(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Apply smart Title Case to college_name."""
    df["college_name"] = df["college_name"].apply(
        lambda v: _smart_title(v) if isinstance(v, str) else v
    )
    logger.debug("college_name casing applied.")
    return df


def step_branch_casing(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Apply smart Title Case to branch (course name)."""
    df["branch"] = df["branch"].apply(
        lambda v: _smart_title(v) if isinstance(v, str) else v
    )
    logger.debug("branch casing applied.")
    return df


def step_institute_type_casing(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Apply Title Case to institute_type."""
    df["institute_type"] = df["institute_type"].apply(
        lambda v: v.title() if isinstance(v, str) else v
    )
    logger.debug("institute_type casing applied.")
    return df


def step_category_normalise(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Normalise category tokens to consistent UPPER CASE.

    Also strips stray punctuation that the PDF parser may have picked up.
    """
    def _clean_cat(val: str) -> str:
        val = str(val).strip().upper()
        # Remove anything that is not alphanumeric
        val = re.sub(r"[^A-Z0-9]", "", val)
        return val

    df["category"] = df["category"].apply(_clean_cat)
    # Blank categories after cleaning → NaN so they surface in the audit
    df["category"] = df["category"].replace("", pd.NA)
    logger.debug("category normalised to UPPER with punctuation stripped.")
    return df


def step_stage_normalise(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Map every observed stage variant to a canonical string.

    Unknown variants are kept as-is (logged as warnings).
    """
    def _canon(val: str) -> str:
        key = str(val).strip().lower().replace(" ", "-")
        return STAGE_CANON.get(key, val)

    before_unique = df["stage"].unique().tolist()
    df["stage"] = df["stage"].apply(_canon)
    after_unique = df["stage"].unique().tolist()

    # Warn on anything still not in the canon value set
    canon_values = set(STAGE_CANON.values())
    unknown = [s for s in after_unique if s not in canon_values]
    if unknown:
        logger.warning(
            "Unrecognised stage value(s) after normalisation: %s", unknown
        )
    logger.debug(
        "stage: %d unique → %d unique after normalisation.",
        len(before_unique), len(after_unique),
    )
    return df


def step_numeric_coercion(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Coerce rank → nullable Int64, percentile → float64.

    Invalid values become NaN (not a hard crash).
    """
    before_rank_na = df["rank"].isna().sum()
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce")

    before_perc_na = df["percentile"].isna().sum()
    df["percentile"] = pd.to_numeric(df["percentile"], errors="coerce")

    new_rank_na = df["rank"].isna().sum() - before_rank_na
    new_perc_na = df["percentile"].isna().sum() - before_perc_na

    if new_rank_na:
        logger.warning(
            "%d rank value(s) could not be coerced to int → set to NaN.",
            new_rank_na,
        )
    if new_perc_na:
        logger.warning(
            "%d percentile value(s) could not be coerced to float → set to NaN.",
            new_perc_na,
        )

    # Use nullable integer so NaN is representable
    df["rank"] = df["rank"].astype("Int64")
    logger.debug("rank → Int64, percentile → float64.")
    return df


def step_range_validation(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Flag out-of-range rank / percentile values as NaN.

    A row is not dropped – it remains in the CSV but its invalid numeric
    field becomes NaN so downstream code can filter explicitly.
    """
    # Rank
    rank_oob = df["rank"].notna() & (
        (df["rank"] < RANK_MIN) | (df["rank"] > RANK_MAX)
    )
    if rank_oob.any():
        logger.warning(
            "%d row(s) have rank outside [%d, %d] → set to NaN.",
            rank_oob.sum(), RANK_MIN, RANK_MAX,
        )
        df.loc[rank_oob, "rank"] = pd.NA

    # Percentile
    perc_oob = df["percentile"].notna() & (
        (df["percentile"] < PERCENTILE_MIN) | (df["percentile"] > PERCENTILE_MAX)
    )
    if perc_oob.any():
        logger.warning(
            "%d row(s) have percentile outside [%.1f, %.1f] → set to NaN.",
            perc_oob.sum(), PERCENTILE_MIN, PERCENTILE_MAX,
        )
        df.loc[perc_oob, "percentile"] = pd.NA

    logger.debug("Range validation done.")
    return df


def step_pad_college_code(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Zero-pad college_code to COLLEGE_CODE_WIDTH digits (e.g. "42" → "0042").
    """
    df["college_code"] = (
        df["college_code"]
        .astype(str)
        .str.strip()
        .str.zfill(COLLEGE_CODE_WIDTH)
    )
    logger.debug("college_code zero-padded to %d digits.", COLLEGE_CODE_WIDTH)
    return df


def step_pad_choice_code(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Zero-pad choice_code to CHOICE_CODE_WIDTH digits (e.g. "12345" → "000012345").
    """
    df["choice_code"] = (
        df["choice_code"]
        .astype(str)
        .str.strip()
        .str.zfill(CHOICE_CODE_WIDTH)
    )
    logger.debug("choice_code zero-padded to %d digits.", CHOICE_CODE_WIDTH)
    return df


def step_validate_round_year(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Coerce round/academic_year to int.
    Flag rows where round < 1 or academic_year < YEAR_MIN as warnings.
    """
    df["round"] = pd.to_numeric(df["round"], errors="coerce").astype("Int64")
    df["academic_year"] = pd.to_numeric(
        df["academic_year"], errors="coerce"
    ).astype("Int64")

    bad_round = df["round"].notna() & (df["round"] < 1)
    if bad_round.any():
        logger.warning(
            "%d row(s) have round < 1 – possible extraction error.",
            bad_round.sum(),
        )

    bad_year = df["academic_year"].notna() & (df["academic_year"] < YEAR_MIN)
    if bad_year.any():
        logger.warning(
            "%d row(s) have academic_year < %d – possible extraction error.",
            bad_year.sum(), YEAR_MIN,
        )

    logger.debug("round / academic_year validated.")
    return df


def step_drop_duplicates(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Remove exact duplicate rows (all columns identical)."""
    before = len(df)
    df.drop_duplicates(inplace=True)
    dropped = before - len(df)
    if dropped:
        logger.warning("Dropped %d exact duplicate row(s).", dropped)
    else:
        logger.debug("No duplicate rows found.")
    return df


def step_audit_missing(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """
    Log the count of NaN values per column.

    Emits WARNING for columns with critical missing data (rank, college_code,
    branch, stage, category) and INFO for everything else.

    Does NOT drop any rows – that is a downstream decision.
    """
    critical_cols = {"rank", "college_code", "branch", "stage", "category"}
    na_counts = df.isna().sum()
    na_nonzero = na_counts[na_counts > 0]

    if na_nonzero.empty:
        logger.info("Missing-value audit: no NaN values found. ✓")
        return df

    logger.info("Missing-value audit:")
    for col, count in na_nonzero.items():
        pct = count / len(df) * 100
        msg = "  %-20s  %d NaN  (%.2f%%)" % (col, count, pct)
        if col in critical_cols:
            logger.warning(msg)
        else:
            logger.info(msg)

    return df


def step_sort(
    df: pd.DataFrame, logger: logging.Logger
) -> pd.DataFrame:
    """Sort by college_code → choice_code → category → stage."""
    available = [c for c in SORT_COLUMNS if c in df.columns]
    df.sort_values(by=available, inplace=True, ignore_index=True)
    logger.debug("DataFrame sorted by: %s", available)
    return df


# ===========================================================================
# CLEANING PIPELINE
# ===========================================================================


PIPELINE: list[tuple[str, CleanStep]] = [
    ("Schema validation",        step_validate_schema),
    ("Whitespace normalisation", step_normalise_whitespace),
    ("College name casing",      step_college_name_casing),
    ("Branch casing",            step_branch_casing),
    ("Institute type casing",    step_institute_type_casing),
    ("Category normalisation",   step_category_normalise),
    ("Stage normalisation",      step_stage_normalise),
    ("Numeric coercion",         step_numeric_coercion),
    ("Range validation",         step_range_validation),
    ("College code padding",     step_pad_college_code),
    ("Choice code padding",      step_pad_choice_code),
    ("Round / year validation",  step_validate_round_year),
    ("Duplicate removal",        step_drop_duplicates),
    ("Missing-value audit",      step_audit_missing),
    ("Sort",                     step_sort),
]


# ===========================================================================
# DATA CLEANER  (top-level orchestrator)
# ===========================================================================


@dataclass
class CleanResult:
    """Summary of one clean run."""
    raw_path: Path
    clean_path: Optional[Path]
    rows_in: int
    rows_out: int
    success: bool
    error: str = ""


class DataCleaner:
    """
    Orchestrates the full cleaning pipeline for a single raw CSV.

    Parameters
    ----------
    logger : logging.Logger
        Shared application logger.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger

    def clean(self, raw_path: Path) -> CleanResult:
        """
        Load *raw_path*, run the full pipeline, write the clean CSV.

        Parameters
        ----------
        raw_path : Path
            A raw cap_round_N.csv file produced by extract_pdf.py.

        Returns
        -------
        CleanResult
            Metadata about the cleaning run.
        """
        self.log.info("─── Cleaning: %s", raw_path.name)

        # ── Load ─────────────────────────────────────────────────────────────
        try:
            df = pd.read_csv(raw_path, dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            self.log.error("Failed to read %s: %s", raw_path.name, exc)
            return CleanResult(
                raw_path=raw_path,
                clean_path=None,
                rows_in=0,
                rows_out=0,
                success=False,
                error=str(exc),
            )

        rows_in = len(df)
        self.log.info("  Loaded %d rows.", rows_in)

        # ── Run pipeline ──────────────────────────────────────────────────────
        try:
            for step_name, step_fn in tqdm(
                PIPELINE,
                desc=f"  {raw_path.stem}",
                unit="step",
                leave=False,
            ):
                self.log.debug("  Step: %s", step_name)
                df = step_fn(df, self.log)
        except ValueError as exc:
            # Schema validation failure – hard stop for this file
            self.log.error("Pipeline aborted for %s: %s", raw_path.name, exc)
            return CleanResult(
                raw_path=raw_path,
                clean_path=None,
                rows_in=rows_in,
                rows_out=0,
                success=False,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error(
                "Unexpected error in pipeline for %s: %s",
                raw_path.name, exc, exc_info=True,
            )
            return CleanResult(
                raw_path=raw_path,
                clean_path=None,
                rows_in=rows_in,
                rows_out=0,
                success=False,
                error=str(exc),
            )

        rows_out = len(df)

        # ── Write ─────────────────────────────────────────────────────────────
        out_path = _output_path(raw_path)
        try:
            df.to_csv(out_path, index=False, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.log.error(
                "Failed to write %s: %s", out_path, exc, exc_info=True
            )
            return CleanResult(
                raw_path=raw_path,
                clean_path=None,
                rows_in=rows_in,
                rows_out=rows_out,
                success=False,
                error=str(exc),
            )

        self.log.info(
            "  Written %d rows → %s  (dropped %d rows total)",
            rows_out, out_path.name, rows_in - rows_out,
        )
        return CleanResult(
            raw_path=raw_path,
            clean_path=out_path,
            rows_in=rows_in,
            rows_out=rows_out,
            success=True,
        )


# ===========================================================================
# MAIN
# ===========================================================================


def main() -> None:
    """
    Discover all raw cap_round_*.csv files and clean them one by one.
    """
    logger = configure_logging()
    start = time.perf_counter()

    logger.info("MAHA-DSE Data Cleaner started.")
    logger.info("Processed directory: %s", PROCESSED_DIR)

    # ── Discover ─────────────────────────────────────────────────────────────
    try:
        raw_files = _discover_raw_csvs(PROCESSED_DIR)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if not raw_files:
        logger.warning(
            "No raw cap_round_*.csv files found in %s. "
            "Run extract_pdf.py first.",
            PROCESSED_DIR,
        )
        sys.exit(0)

    logger.info("Found %d file(s) to clean:", len(raw_files))
    for f in raw_files:
        logger.info("  %s", f.name)

    # ── Clean ─────────────────────────────────────────────────────────────────
    cleaner = DataCleaner(logger)
    results: list[CleanResult] = [cleaner.clean(f) for f in raw_files]

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - start
    success = sum(1 for r in results if r.success)
    failed  = len(results) - success

    logger.info("=" * 65)
    logger.info("Cleaning complete in %.1f s", elapsed)
    logger.info("  Success : %d  |  Failed : %d", success, failed)
    logger.info("")
    logger.info(
        "  %-35s  %8s  %8s  %8s",
        "File", "Rows in", "Rows out", "Dropped",
    )
    logger.info("  " + "-" * 63)
    for r in results:
        if r.success:
            logger.info(
                "  %-35s  %8d  %8d  %8d",
                r.raw_path.name,
                r.rows_in,
                r.rows_out,
                r.rows_in - r.rows_out,
            )
        else:
            logger.error(
                "  %-35s  FAILED: %s",
                r.raw_path.name, r.error,
            )
    logger.info("=" * 65)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()