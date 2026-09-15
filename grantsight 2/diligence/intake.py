"""Pull EINs out of whatever a funder happens to have.

Grantee lists arrive as a board-report PDF, a spreadsheet exported from
accounting, or a CSV someone maintains by hand. All three are handled by
finding nine-digit EINs and, where the format allows, the name sitting next to
them.

Deliberately permissive about layout and strict about the EIN itself. A
column header is used when one exists, but a file with no headers, merged
cells, or a name column before the EIN still works, because the EIN pattern is
distinctive enough to find without structure.

What it will not do is guess. A row with no parseable EIN is reported as
unmatched with its original text, so the operator can see exactly what was
skipped rather than discovering a silently shorter portfolio.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

# 9 digits, optionally hyphenated after the second. Rejects longer digit runs
# so phone numbers and amounts do not masquerade as EINs.
EIN_PATTERN = re.compile(r"(?<!\d)(\d{2}-?\d{7})(?!\d)")

EIN_HEADERS = {"ein", "ein number", "einnumber", "tax id", "taxid", "tin",
               "federal ein", "fein", "employer id", "employer identification number"}
NAME_HEADERS = {"name", "organization", "organisation", "org", "grantee",
                "organization name", "org name", "legal name", "nonprofit",
                "recipient", "payee"}

MAX_ROWS = 2000


@dataclass
class IntakeRow:
    ein: str | None = None
    name: str | None = None
    source_row: int | None = None
    raw: str = ""


@dataclass
class IntakeResult:
    rows: list[IntakeRow] = field(default_factory=list)
    unmatched: list[IntakeRow] = field(default_factory=list)
    file_type: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def eins(self) -> list[str]:
        seen, out = set(), []
        for row in self.rows:
            if row.ein and row.ein not in seen:
                seen.add(row.ein)
                out.append(row.ein)
        return out

    @property
    def duplicate_count(self) -> int:
        return len([r for r in self.rows if r.ein]) - len(self.eins)


class UnsupportedFile(ValueError):
    pass


def normalize_ein(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 9 else None


def _find_ein(text: str) -> str | None:
    match = EIN_PATTERN.search(text or "")
    return normalize_ein(match.group(1)) if match else None


# Labels and amounts that sit alongside an EIN on a report line and are not
# part of the organization's name.
_LINE_NOISE = re.compile(
    r"\b(ein|fein|tin|tax\s*id|e\.i\.n\.)\b[:#]?|\$[\d,]+(?:\.\d{2})?|"
    r"\b(?:19|20)\d{2}\b",
    re.I,
)


def _name_from_line(line: str) -> str | None:
    """The organization name from a free-text line, minus the EIN and its label.

    Only ever a display label: every lookup is by EIN, so a wrong guess here
    is cosmetic rather than a mismatch.
    """
    without_ein = EIN_PATTERN.sub(" ", line or "")
    without_noise = _LINE_NOISE.sub(" ", without_ein)
    return re.sub(r"\s+", " ", without_noise).strip(" .,|-\t") or None


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


# ---------------------------------------------------------------------------
# tabular
# ---------------------------------------------------------------------------
def _header_indices(header: list[str]) -> tuple[int | None, int | None]:
    lowered = [_clean(cell).lower() for cell in header]
    ein_idx = next((i for i, h in enumerate(lowered) if h in EIN_HEADERS), None)
    name_idx = next((i for i, h in enumerate(lowered) if h in NAME_HEADERS), None)
    return ein_idx, name_idx


def _rows_to_result(table: list[list], result: IntakeResult) -> IntakeResult:
    if not table:
        result.notes.append("The file contained no rows.")
        return result

    ein_idx, name_idx = _header_indices(table[0])
    start = 1 if (ein_idx is not None or name_idx is not None) else 0
    if start == 1:
        result.notes.append(
            f"Using the header row: EIN from column {ein_idx + 1}" if ein_idx is not None
            else "Header row found, but no EIN column; scanning every cell instead."
        )

    for number, row in enumerate(table[start:], start=start + 1):
        cells = [_clean(c) for c in row]
        if not any(cells):
            continue
        joined = " | ".join(cells)

        ein = None
        if ein_idx is not None and len(cells) > ein_idx:
            ein = normalize_ein(cells[ein_idx])
        if ein is None:
            # Scan every cell: the header may be wrong, absent, or the EIN may
            # sit in a different column on some rows.
            for cell in cells:
                ein = _find_ein(cell)
                if ein:
                    break

        name = None
        if name_idx is not None and len(cells) > name_idx:
            name = cells[name_idx] or None
        if not name:
            # The longest cell that is not the EIN is almost always the name.
            candidates = [c for c in cells if c and normalize_ein(c) is None]
            name = max(candidates, key=len) if candidates else None

        entry = IntakeRow(ein=ein, name=name, source_row=number, raw=joined[:200])
        (result.rows if ein else result.unmatched).append(entry)
        if len(result.rows) >= MAX_ROWS:
            result.notes.append(
                f"Stopped at {MAX_ROWS} organizations; the rest of the file was "
                f"not read."
            )
            break

    # A one-row file whose only row looked like a header leaves nothing. Retry
    # including row 0 rather than reporting an empty portfolio.
    if not result.rows and start == 1:
        result.notes.append("Header row also scanned for data.")
        retry = IntakeResult(file_type=result.file_type)
        _rows_to_result([[""]] + table, retry)
        if retry.rows:
            result.rows = retry.rows
            result.unmatched = retry.unmatched
    return result


def from_csv(data: bytes) -> IntakeResult:
    result = IntakeResult(file_type="csv")
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    table = [row for row in csv.reader(io.StringIO(text), dialect)]
    return _rows_to_result(table, result)


def from_xlsx(data: bytes) -> IntakeResult:
    result = IntakeResult(file_type="xlsx")
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFile("openpyxl is required to read .xlsx files") from exc

    workbook = openpyxl.load_workbook(
        io.BytesIO(data), read_only=True, data_only=True
    )
    table: list[list] = []
    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        if table:
            result.notes.append(f"Also read sheet '{sheet.title}'.")
        table.extend([list(r) for r in rows])
        if len(table) > MAX_ROWS * 2:
            break
    workbook.close()
    return _rows_to_result(table, result)


# ---------------------------------------------------------------------------
# pdf
# ---------------------------------------------------------------------------
def from_pdf(data: bytes) -> IntakeResult:
    """Extract EINs from a PDF, preferring tables and falling back to text.

    A grant list in a PDF has no reliable structure, so the name is taken as
    the text on the same line as the EIN. That is right often enough to be
    useful and wrong often enough that the name is only ever a label -- every
    lookup is by EIN.
    """
    result = IntakeResult(file_type="pdf")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFile("pdfplumber is required to read PDFs") from exc

    table: list[list] = []
    lines: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        result.notes.append(f"Read {len(pdf.pages)} page(s).")
        for page in pdf.pages:
            for extracted in page.extract_tables() or []:
                table.extend([list(r) for r in extracted if r])
            lines.extend((page.extract_text() or "").splitlines())

    if table and any(_find_ein(" ".join(_clean(c) for c in row)) for row in table):
        result.notes.append("Found table structure; using it.")
        return _rows_to_result(table, result)

    result.notes.append("No usable table; scanning text line by line.")
    for number, line in enumerate(lines, start=1):
        cleaned = _clean(line)
        ein = _find_ein(cleaned)
        if not ein:
            continue
        result.rows.append(
            IntakeRow(ein=ein, name=_name_from_line(cleaned),
                      source_row=number, raw=cleaned[:200])
        )
        if len(result.rows) >= MAX_ROWS:
            break

    if not result.rows:
        result.notes.append(
            "No EINs found. If this is a scanned document there is no text "
            "layer to read; export the list as CSV or XLSX instead."
        )
    return result


def from_text(text: str) -> IntakeResult:
    """Pasted text, one organization per line or just a list of EINs."""
    result = IntakeResult(file_type="text")
    for number, line in enumerate((text or "").splitlines(), start=1):
        cleaned = _clean(line)
        if not cleaned:
            continue
        ein = _find_ein(cleaned)
        if ein:
            result.rows.append(
                IntakeRow(ein=ein, name=_name_from_line(cleaned),
                          source_row=number, raw=cleaned[:200])
            )
        else:
            result.unmatched.append(
                IntakeRow(name=cleaned, source_row=number, raw=cleaned[:200])
            )
    return result


def parse_upload(filename: str, data: bytes) -> IntakeResult:
    """Identify by content first, extension second.

    Content wins because export tools and browsers mislabel constantly -- an
    xlsx saved as .csv is common enough that trusting the extension produces a
    parse error on a perfectly good file.
    """
    if not data:
        raise UnsupportedFile("That file is empty.")

    # Magic numbers are definitive.
    if data[:4] == b"%PDF":
        return from_pdf(data)
    if data[:2] == b"PK":                      # zip container, so xlsx
        return from_xlsx(data)

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".pdf":
        raise UnsupportedFile(
            f"'{filename}' is named .pdf but does not start with a PDF header."
        )
    if suffix in (".xlsx", ".xlsm"):
        raise UnsupportedFile(
            f"'{filename}' is named {suffix} but is not a valid Excel file. "
            f"If it was renamed from CSV, change the extension back."
        )
    if suffix in (".csv", ".tsv", ".txt"):
        return from_csv(data)

    # Unknown extension: treat as text only if it actually looks like text.
    sample = data[:1024]
    if b"\x00" in sample:
        raise UnsupportedFile(
            f"'{filename}' looks like binary data, not a spreadsheet or PDF. "
            f"Upload a .csv, .xlsx or .pdf."
        )
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        if not sample or printable / len(sample) < 0.85:
            raise UnsupportedFile(
                f"Could not tell what kind of file '{filename}' is. Upload a "
                f".csv, .xlsx or .pdf."
            ) from None
    return from_csv(data)
