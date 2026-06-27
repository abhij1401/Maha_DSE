"""
parser/extract_pdf.py
=====================
Production-ready PDF extractor for Maharashtra Direct Second Year Engineering (DSE)
CAP Round Cutoff PDFs.

Responsibilities
----------------
- Discover all CAP Round PDFs in data/raw/
- Parse every college → course → category → stage → rank/percentile
- Write raw (uncleaned) CSV files to data/processed/

NOT responsible for
-------------------
- Data cleaning / normalisation  →  clean_data.py
- Merging rounds                 →  merge_data.py

Usage
-----
    python parser/extract_pdf.py

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Iterator, Optional

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import pandas as pd
    import pdfplumber
    from tqdm import tqdm
except ImportError as exc:
    sys.exit(
        f"[FATAL] Missing dependency: {exc}. "
        "Run:  pip install pdfplumber pandas tqdm"
    )

# ===========================================================================
# CONSTANTS
# ===========================================================================

# Project layout (resolved relative to this file so the script is runnable
# from any working directory).
_ROOT: Path = Path(__file__).resolve().parent.parent
RAW_DIR: Path = _ROOT / "data" / "raw"
PROCESSED_DIR: Path = _ROOT / "data" / "processed"
LOG_DIR: Path = _ROOT / "logs"
LOG_FILE: Path = LOG_DIR / "extract_pdf.log"

# CSV output columns (order matters – matches downstream scripts)
CSV_COLUMNS: list[str] = [
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

# ── Regex patterns ──────────────────────────────────────────────────────────

# College header line:  "1002 Government College of Engineering, Amravati"
RE_COLLEGE_HEADER: re.Pattern = re.compile(
    r"^(?P<code>\d{4})\s+(?P<name>.+)$"
)

# Institute type appears alone on its own line
RE_INSTITUTE_TYPE: re.Pattern = re.compile(
    r"^(?:Government|Autonomous|Unaided|Aided|University).*$",
    re.IGNORECASE,
)

# Choice code line: "Choice Code : 100224210"
RE_CHOICE_CODE: re.Pattern = re.compile(
    r"Choice\s+Code\s*[:\-]\s*(?P<code>\d+)",
    re.IGNORECASE,
)

# Course name line: "Course Name : Computer Science and Engineering"
RE_COURSE_NAME: re.Pattern = re.compile(
    r"Course\s+Name\s*[:\-]\s*(?P<name>.+)",
    re.IGNORECASE,
)

# Stage markers
RE_STAGE: re.Pattern = re.compile(
    r"^Stage[\s\-]?(?P<stage>[I]{1,3}|1|2|3)$",
    re.IGNORECASE,
)

# Rank + optional percentile on the same line:  "658 (93.21%)"
# OR rank alone: "658"
RE_RANK_PERC: re.Pattern = re.compile(
    r"^(?P<rank>\d+)\s*(?:\(\s*(?P<perc>[\d.]+)\s*%\s*\))?$"
)

# Percentile alone on its own line (continuation of previous rank)
RE_PERC_ONLY: re.Pattern = re.compile(
    r"^\(\s*(?P<perc>[\d.]+)\s*%\s*\)$"
)

# Category token – e.g. GOPENH, LSCH, EWS, PWD, DEF, ORPHAN, TFWS …
RE_CATEGORY: re.Pattern = re.compile(
    r"^(?:[GL]O?[PSCB]|[GL][SO]|EWS|PWD|DEF|ORPHAN|TFWS|NRI|OBC|NT[A-D]?|SC|ST|VJ|DT|SBC).*$",
    re.IGNORECASE,
)

# Academic year buried in the filename, e.g. "_2024.pdf"
RE_YEAR_IN_FILENAME: re.Pattern = re.compile(r"(\d{4})")

# Round number buried in the filename, e.g. "ROUND_I_", "ROUND_II_", "ROUND_1_"
RE_ROUND_IN_FILENAME: re.Pattern = re.compile(
    r"ROUND[_\s]?([IVX]+|\d+)",
    re.IGNORECASE,
)

# Map Roman numeral round suffixes to integers
_ROMAN_TO_INT: dict[str, int] = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
    "VI": 6, "VII": 7, "VIII": 8,
}

# ===========================================================================
# DATA CLASSES
# ===========================================================================


@dataclass
class ParsedRecord:
    """One row in the output CSV."""

    college_code: str = ""
    college_name: str = ""
    institute_type: str = ""
    choice_code: str = ""
    branch: str = ""
    stage: str = ""
    category: str = ""
    rank: Optional[int] = None
    percentile: Optional[float] = None
    round: int = 0
    academic_year: int = 0

    def is_complete(self) -> bool:
        """Return True only when all required fields are populated."""
        return all([
            self.college_code,
            self.college_name,
            self.choice_code,
            self.branch,
            self.stage,
            self.category,
            self.rank is not None,
        ])

    def to_dict(self) -> dict:
        return {
            "college_code": self.college_code,
            "college_name": self.college_name,
            "institute_type": self.institute_type,
            "choice_code": self.choice_code,
            "branch": self.branch,
            "stage": self.stage,
            "category": self.category,
            "rank": self.rank,
            "percentile": self.percentile,
            "round": self.round,
            "academic_year": self.academic_year,
        }


@dataclass
class ParserState:
    """
    Mutable state machine carried across pages while parsing a single PDF.

    Keeping state in a dedicated object makes the logic easy to reason about
    and to reset without touching unrelated variables.
    """

    # Current college context
    college_code: str = ""
    college_name: str = ""
    institute_type: str = ""

    # Current course context
    choice_code: str = ""
    branch: str = ""
    branch_continuation: bool = False   # True while collecting a multi-line name

    # Current category/stage context
    categories: list[str] = field(default_factory=list)
    current_stage: str = ""

    # Pending rank without percentile yet (rank appeared alone, perc on next line)
    pending_rank: Optional[int] = None
    pending_category: str = ""

    # Accumulated records for this PDF
    records: list[dict] = field(default_factory=list)

    def reset_course(self) -> None:
        """Clear course-level fields when a new course begins."""
        self.choice_code = ""
        self.branch = ""
        self.branch_continuation = False
        self.categories = []
        self.current_stage = ""
        self.pending_rank = None
        self.pending_category = ""

    def reset_stage(self) -> None:
        """Clear stage-level tracking (category index, pending rank)."""
        self.current_stage = ""
        self.pending_rank = None
        self.pending_category = ""


# ===========================================================================
# LOGGING
# ===========================================================================


def configure_logging() -> logging.Logger:
    """
    Set up a logger that writes to both the console and a rotating log file.

    Returns
    -------
    logging.Logger
        The configured application logger.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("extract_pdf")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler – DEBUG and above
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Console handler – INFO and above
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


def _roman_to_int(token: str) -> int:
    """Convert a Roman numeral string to an integer.  Returns 0 on failure."""
    return _ROMAN_TO_INT.get(token.upper(), 0)


def _detect_round(stem: str) -> int:
    """
    Infer the CAP round number from the PDF filename stem.

    Examples
    --------
    "DSE_CAP_ROUND_I_CUTOFF_2024"  → 1
    "DSE_CAP_ROUND_II_CUTOFF_2024" → 2
    "DSE_CAP_ROUND_2_CUTOFF_2025"  → 2
    """
    match = RE_ROUND_IN_FILENAME.search(stem)
    if not match:
        return 0
    token = match.group(1).upper()
    if token.isdigit():
        return int(token)
    return _roman_to_int(token)


def _detect_year(stem: str) -> int:
    """
    Extract the academic year (4-digit number) from the PDF filename stem.

    Returns 0 when no year can be found.
    """
    hits = RE_YEAR_IN_FILENAME.findall(stem)
    # Last 4-digit number in the name is typically the year
    return int(hits[-1]) if hits else 0


def _discover_pdfs(directory: Path) -> list[Path]:
    """
    Return all PDF files found directly inside *directory*, sorted by name.

    Raises
    ------
    FileNotFoundError
        When *directory* does not exist.
    """
    if not directory.exists():
        raise FileNotFoundError(
            f"Raw PDF directory not found: {directory}"
        )
    pdfs = sorted(directory.glob("*.pdf"))
    return pdfs


def _output_path(pdf_path: Path, round_num: int) -> Path:
    """
    Compute the output CSV path for a given PDF.

    Example
    -------
    pdf_path = .../data/raw/DSE_CAP_ROUND_I_CUTOFF_2024.pdf, round_num = 1
    → .../data/processed/cap_round_1.csv
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    return PROCESSED_DIR / f"cap_round_{round_num}.csv"


# ===========================================================================
# STAGE PARSER
# ===========================================================================


class StageParser:
    """
    Identifies stage markers (Stage-I / Stage-II) in a text line.
    """

    @staticmethod
    def detect(line: str) -> Optional[str]:
        """
        Return a normalised stage string ("Stage-I", "Stage-II", …) or None.
        """
        m = RE_STAGE.match(line.strip())
        if not m:
            return None
        token = m.group("stage").upper()
        mapping = {"I": "Stage-I", "1": "Stage-I",
                   "II": "Stage-II", "2": "Stage-II",
                   "III": "Stage-III", "3": "Stage-III"}
        return mapping.get(token, f"Stage-{token}")


# ===========================================================================
# COURSE PARSER
# ===========================================================================


class CourseParser:
    """
    Handles multi-line course name continuation and choice-code detection.
    """

    @staticmethod
    def is_choice_code_line(line: str) -> Optional[str]:
        """Return the choice code string if this line declares one."""
        m = RE_CHOICE_CODE.search(line)
        return m.group("code") if m else None

    @staticmethod
    def is_course_name_line(line: str) -> Optional[str]:
        """Return the course name text if this line declares one."""
        m = RE_COURSE_NAME.search(line)
        return m.group("name").strip() if m else None

    @staticmethod
    def looks_like_name_continuation(line: str) -> bool:
        """
        Heuristic: a line that isn't a keyword / code is likely a continuation
        of the previous multi-line course name.
        """
        stripped = line.strip()
        if not stripped:
            return False
        # Must start with a letter (not a digit, not a bracket)
        if not stripped[0].isalpha():
            return False
        # Must not look like a category or stage marker
        if RE_CATEGORY.match(stripped) or RE_STAGE.match(stripped):
            return False
        # Must not look like a college header
        if RE_COLLEGE_HEADER.match(stripped):
            return False
        return True


# ===========================================================================
# COLLEGE PARSER
# ===========================================================================


class CollegeParser:
    """
    Detects college header lines and institute type lines.
    """

    @staticmethod
    def is_college_header(line: str) -> Optional[re.Match]:
        """Return the regex match if this line is a college header, else None."""
        return RE_COLLEGE_HEADER.match(line.strip())

    @staticmethod
    def is_institute_type(line: str) -> bool:
        return bool(RE_INSTITUTE_TYPE.match(line.strip()))


# ===========================================================================
# PAGE PARSER
# ===========================================================================


class PageParser:
    """
    Converts the raw text lines of a single PDF page into ParsedRecord dicts,
    updating the shared ParserState in-place.

    One PageParser instance is reused across all pages of a PDF so that
    college/course context persists across page boundaries.
    """

    def __init__(
        self,
        state: ParserState,
        round_num: int,
        academic_year: int,
        logger: logging.Logger,
    ) -> None:
        self.state = state
        self.round_num = round_num
        self.academic_year = academic_year
        self.log = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_page(self, page_lines: list[str], page_num: int) -> int:
        """
        Parse one page worth of text lines.

        Parameters
        ----------
        page_lines : list[str]
            Non-empty, stripped text lines from the page.
        page_num : int
            1-based page number for logging.

        Returns
        -------
        int
            Number of new records appended to state.records during this page.
        """
        before = len(self.state.records)

        for line in page_lines:
            self._process_line(line, page_num)

        added = len(self.state.records) - before
        return added

    # ------------------------------------------------------------------
    # Internal line-level dispatch
    # ------------------------------------------------------------------

    def _process_line(self, line: str, page_num: int) -> None:
        """Route a single line to the appropriate handler."""
        stripped = line.strip()
        if not stripped:
            return

        # 1. College header?
        college_match = CollegeParser.is_college_header(stripped)
        if college_match:
            self._handle_college_header(college_match)
            return

        # 2. Institute type?
        if CollegeParser.is_institute_type(stripped):
            self.state.institute_type = stripped
            return

        # 3. Choice code?
        choice_code = CourseParser.is_choice_code_line(stripped)
        if choice_code:
            self._handle_choice_code(choice_code)
            return

        # 4. Course name?
        course_name = CourseParser.is_course_name_line(stripped)
        if course_name:
            self._handle_course_name(course_name)
            return

        # 5. Course-name continuation (must follow a partial course name)?
        if self.state.branch_continuation and CourseParser.looks_like_name_continuation(stripped):
            self.state.branch += " " + stripped
            return

        # Once we've begun accumulating categories, stop branch continuation
        self.state.branch_continuation = False

        # 6. Stage marker?
        stage = StageParser.detect(stripped)
        if stage:
            self._handle_stage(stage)
            return

        # 7. Category token?
        if RE_CATEGORY.match(stripped):
            self._handle_category(stripped)
            return

        # 8. Rank (+ optional percentile)?
        rank_match = RE_RANK_PERC.match(stripped)
        if rank_match:
            self._handle_rank(rank_match, page_num)
            return

        # 9. Percentile continuation of a pending rank?
        perc_match = RE_PERC_ONLY.match(stripped)
        if perc_match and self.state.pending_rank is not None:
            self._handle_pending_percentile(perc_match, page_num)
            return

        # 10. Unrecognised – log at DEBUG level only
        self.log.debug(
            "Page %d | Unrecognised line: %r", page_num, stripped[:120]
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_college_header(self, match: re.Match) -> None:
        """Start a new college context."""
        # Flush any pending rank from the previous course
        self._flush_pending_rank()
        self.state.reset_course()

        self.state.college_code = match.group("code").strip()
        self.state.college_name = match.group("name").strip()
        self.state.institute_type = ""
        self.log.debug(
            "College: %s – %s",
            self.state.college_code, self.state.college_name,
        )

    def _handle_choice_code(self, code: str) -> None:
        """Start a new course context."""
        self._flush_pending_rank()
        self.state.reset_course()
        self.state.choice_code = code
        self.log.debug("  Choice code: %s", code)

    def _handle_course_name(self, name: str) -> None:
        """Record the course name; flag that it may continue on the next line."""
        self.state.branch = name
        self.state.branch_continuation = True
        self.state.categories = []
        self.log.debug("  Branch: %s", name)

    def _handle_stage(self, stage: str) -> None:
        """Switch to a new stage (Stage-I / Stage-II)."""
        self._flush_pending_rank()
        self.state.current_stage = stage
        # Reset category index so they are consumed in order again for this stage
        self._stage_category_index = 0
        self.log.debug("    Stage: %s", stage)

    def _handle_category(self, token: str) -> None:
        """
        Accumulate a category token.

        Categories appear as a header row BEFORE the rank values.
        We collect them in order and later pair them with ranks by position.
        """
        self.state.branch_continuation = False
        # Only collect categories before any stage is encountered
        if not self.state.current_stage:
            self.state.categories.append(token)

    def _handle_rank(
        self, match: re.Match, page_num: int
    ) -> None:
        """
        Process a rank (and optionally percentile) value.

        Pairs with the next available category in the current stage.
        """
        if not self.state.current_stage:
            return  # Rank without stage context – skip

        rank_val = int(match.group("rank"))
        perc_str = match.group("perc")
        percentile = float(perc_str) if perc_str else None

        # Which category does this rank belong to?
        category = self._next_category()
        if not category:
            self.log.warning(
                "Page %d | Rank %d has no matching category "
                "(college=%s, branch=%s, stage=%s)",
                page_num, rank_val,
                self.state.college_code, self.state.branch,
                self.state.current_stage,
            )
            return

        if percentile is None:
            # Percentile may be on the NEXT line – store as pending
            self.state.pending_rank = rank_val
            self.state.pending_category = category
            return

        self._emit_record(category, rank_val, percentile)

    def _handle_pending_percentile(
        self, match: re.Match, page_num: int
    ) -> None:
        """Resolve a rank that was waiting for its percentile."""
        percentile = float(match.group("perc"))
        self._emit_record(
            self.state.pending_category,
            self.state.pending_rank,  # type: ignore[arg-type]
            percentile,
        )
        self.state.pending_rank = None
        self.state.pending_category = ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # Per-stage category index (reset each time a new stage begins)
    _stage_category_index: int = 0

    def _next_category(self) -> Optional[str]:
        """
        Return the next category token in order for the current stage.

        Categories from the header row are consumed sequentially.
        """
        idx = self._stage_category_index
        if idx < len(self.state.categories):
            self._stage_category_index += 1
            return self.state.categories[idx]
        return None

    def _flush_pending_rank(self) -> None:
        """
        Emit a pending rank WITHOUT percentile if we change context.

        This handles the edge case where the PDF ends mid-row.
        """
        if self.state.pending_rank is not None:
            self._emit_record(
                self.state.pending_category,
                self.state.pending_rank,
                None,
            )
            self.state.pending_rank = None
            self.state.pending_category = ""

    def _emit_record(
        self,
        category: str,
        rank: int,
        percentile: Optional[float],
    ) -> None:
        """Append one fully-formed record to the shared state list."""
        record = ParsedRecord(
            college_code=self.state.college_code,
            college_name=self.state.college_name,
            institute_type=self.state.institute_type,
            choice_code=self.state.choice_code,
            branch=self.state.branch,
            stage=self.state.current_stage,
            category=category,
            rank=rank,
            percentile=percentile,
            round=self.round_num,
            academic_year=self.academic_year,
        )
        self.state.records.append(record.to_dict())


# ===========================================================================
# CSV WRITER
# ===========================================================================


class CSVWriter:
    """
    Persists a list of record dicts to a CSV file.
    Drops duplicates and validates required columns before writing.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger

    def write(self, records: list[dict], output_path: Path) -> int:
        """
        Write *records* to *output_path* as a UTF-8 CSV.

        Parameters
        ----------
        records : list[dict]
            Raw record dicts produced by the parsers.
        output_path : Path
            Destination CSV file (created or overwritten).

        Returns
        -------
        int
            Number of rows written.
        """
        if not records:
            self.log.warning("No records to write to %s", output_path)
            return 0

        df = pd.DataFrame(records, columns=CSV_COLUMNS)

        # Drop fully duplicate rows
        before = len(df)
        df.drop_duplicates(inplace=True)
        dupes = before - len(df)
        if dupes:
            self.log.info("Dropped %d duplicate rows", dupes)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False, encoding="utf-8")

        self.log.info("Wrote %d rows → %s", len(df), output_path)
        return len(df)


# ===========================================================================
# PDF EXTRACTOR  (top-level orchestrator)
# ===========================================================================


class PDFExtractor:
    """
    Orchestrates end-to-end extraction for a single PDF file.

    Responsibilities
    ----------------
    1. Open the PDF with pdfplumber.
    2. Extract text lines page by page.
    3. Feed lines to PageParser (which updates ParserState).
    4. Delegate CSV output to CSVWriter.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self.log = logger
        self._csv_writer = CSVWriter(logger)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract(self, pdf_path: Path) -> Optional[Path]:
        """
        Parse *pdf_path* and write the resulting CSV.

        Returns
        -------
        Path | None
            Path to the written CSV, or None on failure.
        """
        stem = pdf_path.stem
        round_num = _detect_round(stem)
        year = _detect_year(stem)

        self.log.info(
            "=== Processing %s  (round=%d, year=%d) ===",
            pdf_path.name, round_num, year,
        )

        if round_num == 0:
            self.log.error(
                "Could not detect round number from filename: %s. Skipping.",
                pdf_path.name,
            )
            return None

        state = ParserState()
        page_parser = PageParser(state, round_num, year, self.log)

        try:
            total_pages = self._count_pages(pdf_path)
            self.log.info("Total pages: %d", total_pages)

            with pdfplumber.open(pdf_path) as pdf:
                page_iter = self._iter_page_lines(pdf, pdf_path.name)
                for page_num, lines in tqdm(
                    page_iter,
                    total=total_pages,
                    desc=f"  Round {round_num}",
                    unit="pg",
                    leave=False,
                ):
                    added = page_parser.parse_page(lines, page_num)
                    self.log.debug(
                        "Page %d → %d new records (running total: %d)",
                        page_num, added, len(state.records),
                    )

        except Exception as exc:  # noqa: BLE001
            self.log.error(
                "Fatal error while parsing %s: %s", pdf_path.name, exc,
                exc_info=True,
            )
            return None

        out_path = _output_path(pdf_path, round_num)
        written = self._csv_writer.write(state.records, out_path)

        self.log.info(
            "Finished %s → %d rows extracted.", pdf_path.name, written,
        )
        return out_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _count_pages(pdf_path: Path) -> int:
        """Quick page count without loading all content."""
        try:
            with pdfplumber.open(pdf_path) as pdf:
                return len(pdf.pages)
        except Exception:  # noqa: BLE001
            return 0

    def _iter_page_lines(
        self, pdf: pdfplumber.PDF, pdf_name: str
    ) -> Generator[tuple[int, list[str]], None, None]:
        """
        Yield (page_number, lines) for each page in the PDF.

        Skips blank pages and pages where text extraction fails.
        Lines are stripped and de-duplicated of consecutive blanks.
        """
        for i, page in enumerate(pdf.pages, start=1):
            try:
                raw_text: Optional[str] = page.extract_text()
            except Exception as exc:  # noqa: BLE001
                self.log.warning(
                    "%s | Page %d | Text extraction failed: %s", pdf_name, i, exc
                )
                continue

            if not raw_text or not raw_text.strip():
                self.log.debug("%s | Page %d | Blank – skipped", pdf_name, i)
                continue

            lines = [ln.strip() for ln in raw_text.splitlines()]
            # Remove consecutive blank lines to avoid noise
            cleaned: list[str] = []
            prev_blank = False
            for ln in lines:
                is_blank = not ln
                if is_blank and prev_blank:
                    continue
                cleaned.append(ln)
                prev_blank = is_blank

            yield i, cleaned


# ===========================================================================
# MAIN ENTRY POINT
# ===========================================================================


def main() -> None:
    """
    Discover all CAP PDFs in data/raw/ and extract them one by one.
    """
    logger = configure_logging()
    start_time = time.perf_counter()

    logger.info("MAHA-DSE PDF Extractor started.")
    logger.info("Raw PDF directory : %s", RAW_DIR)
    logger.info("Processed CSV dir : %s", PROCESSED_DIR)

    # ── Discover PDFs ────────────────────────────────────────────────────────
    try:
        pdfs = _discover_pdfs(RAW_DIR)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if not pdfs:
        logger.warning(
            "No PDF files found in %s. Nothing to process.", RAW_DIR
        )
        sys.exit(0)

    logger.info("Found %d PDF file(s):", len(pdfs))
    for p in pdfs:
        logger.info("  %s", p.name)

    # ── Extract each PDF ─────────────────────────────────────────────────────
    extractor = PDFExtractor(logger)
    results: list[tuple[Path, Optional[Path]]] = []

    for pdf_path in pdfs:
        out_csv = extractor.extract(pdf_path)
        results.append((pdf_path, out_csv))

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - start_time
    success = sum(1 for _, o in results if o is not None)
    failed = len(results) - success

    logger.info("=" * 60)
    logger.info("Extraction complete in %.1f s", elapsed)
    logger.info("  Success : %d", success)
    logger.info("  Failed  : %d", failed)
    for pdf_path, out_csv in results:
        status = str(out_csv) if out_csv else "FAILED"
        logger.info("  %-45s → %s", pdf_path.name, status)
    logger.info("=" * 60)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()