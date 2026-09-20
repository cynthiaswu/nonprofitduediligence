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
# The award column is the most valuable thing in a grant list and the one
# piece of information no public database has. It turns "is this organization
# healthy" into "how exposed are we, and are we their dominant funder".
AWARD_HEADERS = {"amount", "award", "award amount", "grant", "grant amount",
                 "total", "approved", "paid", "payment", "value", "$",
                 "grant size", "funding", "commitment"}

MONEY_PATTERN = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)|(?<![\w.])([\d,]{4,})(?:\.\d{2})?(?![\w])")

MAX_ROWS = 2000


@dataclass
class IntakeRow:
    ein: str | None = None
    name: str | None = None
    award: float | None = None
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
    def awards(self) -> dict[str, float]:
        """EIN to award amount, summed when an organization appears twice."""
        out: dict[str, float] = {}
        for row in self.rows:
            if row.ein and row.award:
                out[row.ein] = out.get(row.ein, 0) + row.award
        return out

    @property
    def total_awarded(self) -> float:
        return sum(self.awards.values())

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


def _award_from_line(line: str) -> float | None:
    """A dollar amount from a free-text line, with the EIN removed first.

    Without stripping it, "EIN 52-1693387" parses as an award of $1,693,387.
    """
    return parse_money(EIN_PATTERN.sub(" ", line or ""))


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
def parse_money(value) -> float | None:
    """A dollar amount from a cell or a line, if one is unambiguously there."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    text = _clean(value)
    if not text:
        return None
    match = MONEY_PATTERN.search(text)
    if not match:
        return None
    raw = (match.group(1) or match.group(2) or "").replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None
    # Four-digit bare numbers are usually years, not awards.
    if match.group(2) and 1900 <= amount <= 2100:
        return None
    return amount if amount > 0 else None


_SPLIT_CURRENCY = re.compile(r"^\$?\d{1,3}$")
_THOUSANDS = re.compile(r"^\d{3}$")


def _repair_split_currency(cells: list[str]) -> list[str]:
    """Rejoin "$275,000" that a comma-delimited export split into two columns.

    Exports that write currency unquoted produce ["$275", "000"]. Without
    repair that reads as an award of $275, which is worse than no figure at
    all: it is wrong in a direction that makes a grantee look trivially
    funded. Only joins when the first part looks like a truncated amount and
    the next is exactly three digits.
    """
    if len(cells) < 2:
        return cells
    out: list[str] = []
    index = 0
    while index < len(cells):
        current = cells[index]
        if (current.startswith("$") and _SPLIT_CURRENCY.match(current)
                and index + 1 < len(cells) and _THOUSANDS.match(cells[index + 1])):
            joined = current + "," + cells[index + 1]
            step = 2
            # Keep absorbing further thousands groups: $1,250,000.
            while (index + step < len(cells)
                   and _THOUSANDS.match(cells[index + step])):
                joined += "," + cells[index + step]
                step += 1
            out.append(joined)
            index += step
        else:
            out.append(current)
            index += 1
    return out


def _header_indices(header: list[str]) -> tuple[int | None, int | None, int | None]:
    lowered = [_clean(cell).lower() for cell in header]
    ein_idx = next((i for i, h in enumerate(lowered) if h in EIN_HEADERS), None)
    name_idx = next((i for i, h in enumerate(lowered) if h in NAME_HEADERS), None)
    award_idx = next(
        (i for i, h in enumerate(lowered)
         if h in AWARD_HEADERS or any(w in h for w in ("amount", "award", "grant $"))),
        None,
    )
    return ein_idx, name_idx, award_idx


def _rows_to_result(table: list[list], result: IntakeResult) -> IntakeResult:
    if not table:
        result.notes.append("The file contained no rows.")
        return result

    ein_idx, name_idx, award_idx = _header_indices(table[0])
    start = 1 if (ein_idx is not None or name_idx is not None) else 0
    if start == 1:
        result.notes.append(
            f"Using the header row: EIN from column {ein_idx + 1}" if ein_idx is not None
            else "Header row found, but no EIN column; scanning every cell instead."
        )

    for number, row in enumerate(table[start:], start=start + 1):
        cells = _repair_split_currency([_clean(c) for c in row])
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

        award = None
        if award_idx is not None and len(cells) > award_idx:
            # Read the repaired cell, not the raw one: the raw value is
            # exactly where a split "$275,000" reads as $275.
            award = parse_money(cells[award_idx])
            if award is None and len(row) > award_idx:
                # Spreadsheets store amounts as numbers; keep that path.
                award = parse_money(row[award_idx])
        if award is None:
            for index, cell in enumerate(cells):
                if index in (ein_idx, name_idx) or normalize_ein(cell):
                    continue
                award = parse_money(cell)
                if award:
                    break

        entry = IntakeRow(ein=ein, name=name, award=award,
                          source_row=number, raw=joined[:200])
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
                      award=_award_from_line(cleaned),
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
                          award=_award_from_line(cleaned),
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
