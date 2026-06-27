"""
parser/merge_data.py
====================
Production-ready merger for Maharashtra DSE CAP Round cleaned CSVs.

Responsibilities
----------------
- Discover every clean_cap_round_*.csv in data/processed/
- Validate schema consistency across rounds before merging
- Concatenate all rounds into one unified DataFrame
- Detect and resolve cross-round conflicts (same choice_code + category
  appearing in multiple rounds → kept as separate rows, flagged clearly)
- Produce two output files:

    data/processed/cutoff_data.csv    ← All rounds stacked (long format)
    data/processed/final_dataset.csv  ← Pivoted: one row per
                                         college/branch/category with
                                         Stage-I and Stage-II ranks from
                                         each round as separate columns

NOT responsible for
-------------------
- PDF parsing   → extract_pdf.py
- Data cleaning → clean_data.py
- Prediction    → predictor/predictor.py

Merge logic
-----------
1. Schema validation     – all input files share the same required columns
2. Load & tag            – each file is loaded with its round number confirmed
3. Concatenate           – pd.concat with ignore_index
4. Cross-round duplicate audit – log rows that appear identical across rounds
5. Sort                  – college_code, choice_code, round, category, stage
6. Write cutoff_data.csv – the unified long-format file
7. Pivot                 – one row per (college_code, college_name,
                           institute_type, choice_code, branch, category)
                           columns: round1_stage1_rank, round1_stage1_pct,
                                    round1_stage2_rank, round1_stage2_pct,
                                    round2_stage1_rank, …
8. Write final_dataset.csv

Usage
-----
    python parser/merge_data.py

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
from typing import Optional

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
LOG_FILE: Path = LOG_DIR / "merge_data.log"

# Input glob  – only the cleaned files
CLEAN_GLOB: str = "clean_cap_round_*.csv"

# Output files
CUTOFF_DATA_PATH: Path = PROCESSED_DIR / "cutoff_data.csv"
FINAL_DATASET_PATH: Path = PROCESSED_DIR / "final_dataset.csv"

# Columns every clean CSV must contain (mirrors clean_data.py REQUIRED_COLUMNS)
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

# Natural-key columns that uniquely identify one cutoff data point
# (used for duplicate detection and pivot index)
NATURAL_KEY: list[str] = [
    "college_code",
    "choice_code",
    "category",
    "stage",
    "round",
]

# Columns that form the pivot index for final_dataset.csv
PIVOT_INDEX: list[str] = [
    "college_code",
    "college_name",
    "institute_type",
    "choice_code",
    "branch",
    "category",
    "academic_year",
]

# Sort order for cutoff_data.csv
SORT_COLUMNS: list[str] = [
    "college_code",
    "choice_code",
    "round",
    "category",
    "stage",
]

# Canonical stage values expected in clean data
KNOWN_STAGES: frozenset[str] = frozenset({"Stage-I", "Stage-II", "Stage-III"})


# ===========================================================================
# LOGGING
# ===========================================================================


def configure_logging() -> logging.Logger:
    """Configure file + console logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("merge_data")
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


def _round_from_stem(stem: str) -> int:
    """
    Extract the trailing integer from a filename stem.

    Example
    -------
    "clean_cap_round_2" → 2
    """
    import re
    m = re.search(r"(\d+)$", stem)
    return int(m.group(1)) if m else 0


def _discover_clean_csvs(directory: Path) -> list[Path]:
    """
    Return all clean_cap_round_*.csv files sorted by round number.

    Raises
    ------
    FileNotFoundError
        When *directory* does not exist.
    """
    if not directory.exists():
        raise FileNotFoundError(
            f"Processed directory not found: {directory}. "
            "Run extract_pdf.py and clean_data.py first."
        )
    files = sorted(
        directory.glob(CLEAN_GLOB),
        key=lambda p: _round_from_stem(p.stem),
    )
    return files


# ===========================================================================
# DATA CLASSES
# ===========================================================================


@dataclass
class LoadResult:
    """Outcome of loading one clean CSV."""
    path: Path
    round_num: int
    rows: int
    df: Optional[pd.DataFrame]
    success: bool
    error: str = ""


@dataclass
class MergeStats:
    """Aggregate statistics for a complete merge run."""
    files_loaded: int = 0
    files_failed: int = 0
    total_rows_in: int = 0
    exact_duplicates_dropped: int = 0
    rows_in_cutoff: int = 0
    rows_in_final: int = 0
    rounds_found: list[int] = field(default_factory=list)


# ===========================================================================
# LOADER
# ===========================================================================


class RoundLoader:
    """
    Loads and validates a single clean_cap_round_N.csv.

    Validation
    ----------
    - All REQUIRED_COLUMNS must be present.
    - The ``round`` column values must be consistent with the filename.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger

    def load(self, path: Path) -> LoadResult:
        """
        Read *path* and return a LoadResult.

        Never raises – errors are captured in LoadResult.error.
        """
        round_num = _round_from_stem(path.stem)
        self.log.info("  Loading %s  (round=%d)", path.name, round_num)

        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=True)
        except Exception as exc:  # noqa: BLE001
            self.log.error("    Cannot read %s: %s", path.name, exc)
            return LoadResult(
                path=path, round_num=round_num, rows=0,
                df=None, success=False, error=str(exc),
            )

        # ── Schema check ───────────────────────────────────────────────
        missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing_cols:
            msg = f"Missing columns {missing_cols}. Re-run clean_data.py."
            self.log.error("    %s: %s", path.name, msg)
            return LoadResult(
                path=path, round_num=round_num, rows=len(df),
                df=None, success=False, error=msg,
            )

        # ── Numeric coercion ───────────────────────────────────────────
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce").astype("Int64")
        df["percentile"] = pd.to_numeric(df["percentile"], errors="coerce")
        df["round"] = pd.to_numeric(df["round"], errors="coerce").astype("Int64")
        df["academic_year"] = pd.to_numeric(
            df["academic_year"], errors="coerce"
        ).astype("Int64")

        # ── Round consistency check ────────────────────────────────────
        if round_num > 0:
            observed_rounds = df["round"].dropna().unique().tolist()
            if observed_rounds and round_num not in observed_rounds:
                self.log.warning(
                    "    %s: filename says round=%d but data contains rounds=%s.",
                    path.name, round_num, observed_rounds,
                )

        self.log.info("    Loaded %d rows.", len(df))
        return LoadResult(
            path=path, round_num=round_num, rows=len(df),
            df=df, success=True,
        )


# ===========================================================================
# MERGER
# ===========================================================================


class DataMerger:
    """
    Concatenates multiple round DataFrames into unified output files.

    Parameters
    ----------
    logger : logging.Logger
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def merge(self, load_results: list[LoadResult]) -> MergeStats:
        """
        Run the full merge pipeline on a list of LoadResults.

        Parameters
        ----------
        load_results : list[LoadResult]
            One entry per clean CSV, as returned by RoundLoader.

        Returns
        -------
        MergeStats
            Aggregate statistics for the caller to summarise.
        """
        stats = MergeStats()
        frames: list[pd.DataFrame] = []

        for result in load_results:
            if result.success and result.df is not None:
                frames.append(result.df)
                stats.files_loaded += 1
                stats.total_rows_in += result.rows
                stats.rounds_found.append(result.round_num)
            else:
                stats.files_failed += 1

        if not frames:
            self.log.error("No data frames loaded – aborting merge.")
            return stats

        # ── Step 1: Concatenate ────────────────────────────────────────
        self.log.info("Concatenating %d round(s)…", len(frames))
        combined: pd.DataFrame = pd.concat(frames, ignore_index=True)
        self.log.info("  Combined rows: %d", len(combined))

        # ── Step 2: Cross-round duplicate audit ───────────────────────
        combined, dropped = self._drop_exact_duplicates(combined)
        stats.exact_duplicates_dropped = dropped

        # ── Step 3: Natural-key collision report ──────────────────────
        self._report_natural_key_collisions(combined)

        # ── Step 4: Sort ──────────────────────────────────────────────
        combined = self._sort(combined)

        # ── Step 5: Write cutoff_data.csv ─────────────────────────────
        self._write_csv(combined, CUTOFF_DATA_PATH)
        stats.rows_in_cutoff = len(combined)
        self.log.info(
            "cutoff_data.csv written (%d rows).", stats.rows_in_cutoff
        )

        # ── Step 6: Pivot → final_dataset.csv ─────────────────────────
        final_df = self._build_final_dataset(combined)
        self._write_csv(final_df, FINAL_DATASET_PATH)
        stats.rows_in_final = len(final_df)
        self.log.info(
            "final_dataset.csv written (%d rows, %d columns).",
            stats.rows_in_final, len(final_df.columns),
        )

        return stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drop_exact_duplicates(
        self, df: pd.DataFrame
    ) -> tuple[pd.DataFrame, int]:
        """
        Drop rows that are completely identical across ALL columns.

        These can arise when the same PDF is accidentally processed twice,
        or when two rounds share some carry-over rows.
        """
        before = len(df)
        df = df.drop_duplicates(ignore_index=True)
        dropped = before - len(df)
        if dropped:
            self.log.warning(
                "Dropped %d exact duplicate row(s) across rounds.", dropped
            )
        else:
            self.log.debug("No exact cross-round duplicates found.")
        return df, dropped

    def _report_natural_key_collisions(self, df: pd.DataFrame) -> None:
        """
        Detect rows that share the same natural key
        (college_code, choice_code, category, stage, round)
        but differ in rank or percentile.

        These are NOT dropped – they may be legitimate (e.g. ties, data
        corrections in later rounds). They are logged as warnings so the
        data team can inspect them.
        """
        available_key = [c for c in NATURAL_KEY if c in df.columns]
        if len(available_key) < len(NATURAL_KEY):
            self.log.warning(
                "Cannot check natural-key collisions – missing columns: %s",
                set(NATURAL_KEY) - set(available_key),
            )
            return

        dupes = df[df.duplicated(subset=available_key, keep=False)]
        if dupes.empty:
            self.log.info(
                "Natural-key collision check: no collisions found. ✓"
            )
            return

        collision_count = dupes[available_key].drop_duplicates().shape[0]
        self.log.warning(
            "%d natural-key collision(s) detected "
            "(same college/choice/category/stage/round with different rank/pct). "
            "Review these rows manually.",
            collision_count,
        )
        # Log the first 10 colliding keys for quick inspection
        sample = (
            dupes[available_key]
            .drop_duplicates()
            .head(10)
            .to_string(index=False)
        )
        self.log.warning("Sample collisions:\n%s", sample)

    def _sort(self, df: pd.DataFrame) -> pd.DataFrame:
        """Sort the combined DataFrame by the canonical column order."""
        available = [c for c in SORT_COLUMNS if c in df.columns]
        return df.sort_values(by=available, ignore_index=True)

    def _build_final_dataset(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Pivot the long-format combined DataFrame into one row per
        (college, branch, category) with round/stage columns spread wide.

        Output column naming convention
        --------------------------------
        round{N}_stage{S}_rank        e.g. round1_stageI_rank
        round{N}_stage{S}_percentile  e.g. round1_stageI_percentile

        Where N is the round number and S is I / II / III.

        Rows that have no rank in a particular round/stage get NaN.
        """
        self.log.info("Building final_dataset (pivot)…")

        # Resolve which rounds and stages exist in the data
        rounds = sorted(df["round"].dropna().unique().astype(int).tolist())
        stages = sorted(
            [s for s in df["stage"].dropna().unique() if s in KNOWN_STAGES]
        )

        if not rounds or not stages:
            self.log.warning(
                "No valid rounds (%s) or stages (%s) found – "
                "final_dataset will be empty.",
                rounds, stages,
            )
            return pd.DataFrame()

        self.log.info(
            "  Rounds: %s  |  Stages: %s", rounds, stages
        )

        # Build pivot index columns (those that should exist in the output)
        pivot_index = [c for c in PIVOT_INDEX if c in df.columns]

        # Pivot rank
        rank_pivot = self._pivot_metric(df, pivot_index, rounds, stages, "rank")

        # Pivot percentile
        pct_pivot = self._pivot_metric(
            df, pivot_index, rounds, stages, "percentile"
        )

        # Interleave rank and percentile columns for readability:
        # round1_stageI_rank, round1_stageI_percentile, round1_stageII_rank …
        final = rank_pivot.copy()
        for col in pct_pivot.columns:
            if col not in final.columns:
                final[col] = pct_pivot[col]

        # Re-order columns: index first, then interleaved rank/pct
        ordered_value_cols: list[str] = []
        for r in rounds:
            for s in stages:
                stage_key = _stage_to_key(s)
                rank_col = f"round{r}_{stage_key}_rank"
                pct_col  = f"round{r}_{stage_key}_percentile"
                if rank_col in final.columns:
                    ordered_value_cols.append(rank_col)
                if pct_col in pct_pivot.columns:
                    ordered_value_cols.append(pct_col)
                    if pct_col not in final.columns:
                        final[pct_col] = pct_pivot[pct_col]

        final = final[pivot_index + ordered_value_cols].reset_index(drop=True)

        self.log.info(
            "  Pivot complete: %d rows × %d columns.",
            len(final), len(final.columns),
        )
        return final

    def _pivot_metric(
        self,
        df: pd.DataFrame,
        pivot_index: list[str],
        rounds: list[int],
        stages: list[str],
        metric: str,          # "rank" or "percentile"
    ) -> pd.DataFrame:
        """
        Pivot *metric* into wide format.

        One column per (round, stage) combination:
            round1_stageI_rank, round1_stageII_rank, round2_stageI_rank …
        """
        # We only need the index + stage + round + the metric column
        cols_needed = pivot_index + ["stage", "round", metric]
        cols_present = [c for c in cols_needed if c in df.columns]
        sub = df[cols_present].copy()

        # Create a column label for each (round, stage) pair
        sub["_col"] = sub.apply(
            lambda row: _make_col_name(row["round"], row["stage"], metric),
            axis=1,
        )

        # Keep only the first value for each (index + _col) group
        # (collision handling – first occurrence wins; logged earlier)
        sub = sub.drop_duplicates(subset=pivot_index + ["_col"], keep="first")

        pivoted = sub.pivot_table(
            index=pivot_index,
            columns="_col",
            values=metric,
            aggfunc="first",
        ).reset_index()

        pivoted.columns.name = None  # Remove the axis name set by pivot_table

        return pivoted

    @staticmethod
    def _write_csv(df: pd.DataFrame, path: Path) -> None:
        """Write a DataFrame to a UTF-8 CSV, creating parent dirs if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False, encoding="utf-8")


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================


def _stage_to_key(stage: str) -> str:
    """
    Convert a canonical stage string to a compact column-name-safe key.

    Examples
    --------
    "Stage-I"   → "stageI"
    "Stage-II"  → "stageII"
    "Stage-III" → "stageIII"
    """
    return stage.replace("-", "").replace(" ", "")


def _make_col_name(round_val, stage_val: str, metric: str) -> str:
    """
    Build a pivot column name like ``round1_stageI_rank``.

    Parameters
    ----------
    round_val : int-like
        The round number (may be a pandas nullable Int64).
    stage_val : str
        Canonical stage string e.g. "Stage-I".
    metric : str
        "rank" or "percentile".

    Returns
    -------
    str
        e.g. "round1_stageI_rank"
    """
    try:
        r = int(round_val)
    except (TypeError, ValueError):
        r = 0
    stage_key = _stage_to_key(str(stage_val))
    return f"round{r}_{stage_key}_{metric}"


# ===========================================================================
# MAIN
# ===========================================================================


def main() -> None:
    """
    Discover all clean_cap_round_*.csv files and merge them.
    """
    logger = configure_logging()
    start = time.perf_counter()

    logger.info("MAHA-DSE Data Merger started.")
    logger.info("Processed directory : %s", PROCESSED_DIR)
    logger.info("Output cutoff_data  : %s", CUTOFF_DATA_PATH)
    logger.info("Output final_dataset: %s", FINAL_DATASET_PATH)

    # ── Discover ─────────────────────────────────────────────────────────────
    try:
        clean_files = _discover_clean_csvs(PROCESSED_DIR)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if not clean_files:
        logger.warning(
            "No clean_cap_round_*.csv files found in %s. "
            "Run clean_data.py first.",
            PROCESSED_DIR,
        )
        sys.exit(0)

    logger.info("Found %d clean file(s):", len(clean_files))
    for f in clean_files:
        logger.info("  %s", f.name)

    # ── Load ─────────────────────────────────────────────────────────────────
    loader = RoundLoader(logger)
    load_results: list[LoadResult] = []

    for path in tqdm(clean_files, desc="Loading", unit="file"):
        load_results.append(loader.load(path))

    failed_loads = [r for r in load_results if not r.success]
    if failed_loads:
        logger.error(
            "%d file(s) failed to load:", len(failed_loads)
        )
        for r in failed_loads:
            logger.error("  %s → %s", r.path.name, r.error)
        if all(not r.success for r in load_results):
            logger.error("All files failed – aborting.")
            sys.exit(1)

    # ── Merge ─────────────────────────────────────────────────────────────────
    merger = DataMerger(logger)
    stats = merger.merge(load_results)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - start

    logger.info("=" * 65)
    logger.info("Merge complete in %.1f s", elapsed)
    logger.info("")
    logger.info("  Rounds merged          : %s", stats.rounds_found)
    logger.info("  Files loaded           : %d", stats.files_loaded)
    logger.info("  Files failed           : %d", stats.files_failed)
    logger.info("  Total input rows       : %d", stats.total_rows_in)
    logger.info("  Exact duplicates dropped: %d", stats.exact_duplicates_dropped)
    logger.info("")
    logger.info(
        "  %-30s : %d rows",
        CUTOFF_DATA_PATH.name, stats.rows_in_cutoff,
    )
    logger.info(
        "  %-30s : %d rows",
        FINAL_DATASET_PATH.name, stats.rows_in_final,
    )
    logger.info("=" * 65)

    if stats.files_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()