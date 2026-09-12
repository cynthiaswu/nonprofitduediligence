"""Read the Form 990 e-file XML: the things the SOI extract leaves out.

Four of them, and they are the four a program officer actually asks about:

  Part IX columns B/C/D   the program / management / fundraising split, which
                          is simply not in the SOI extract (every element
                          there is column A). This is where it lives.
  Part VII Section A      named officers, directors, trustees and key
                          employees, with titles, hours and compensation.
  Schedule J Part II      the detailed compensation breakdown -- base, bonus,
                          deferred, nontaxable benefits -- for the people who
                          cross the reporting thresholds.
  Schedule I Part II      grants made, by recipient name and EIN, with
                          purpose and amount.

ON NAMING PEOPLE
----------------
An earlier version of this codebase declined to surface individual
compensation, reasoning that assembling information about named people is a
different kind of product. That was the wrong call. Part VII exists precisely
so that funders, regulators and the public can see who runs an exempt
organization and what it pays them; the IRS requires the organization to
disclose it, and the organization must hand a copy to anyone who asks. Leaving
it out did not protect anyone, it just made the brief worse at its job.

What this does not do: it reports people in their organizational capacity --
name, title, hours, compensation -- and nothing else. Home addresses present
in some filings are never extracted, and nothing here is aggregated across
organizations to build a profile of a person.

PARSING APPROACH
----------------
Schema versions drift (the 2013 rewrite renamed much of the form, and element
names still shift between annual versions), so nothing here uses a rigid
xpath. Every lookup is a namespace-agnostic search by local element name
against an ordered list of candidates, and the tag that matched is recorded.
Same discipline as the SOI field map: an unresolved value is None, never zero,
and provenance is inspectable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import sqlite3
import struct
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

from .http import FetchError, content_length, get_bytes, get_range
from .propublica import clean_ein

DATA_DIR = Path(os.environ.get("GRANTSIGHT_DATA", "./data"))
XML_DB = DATA_DIR / "xml_index.sqlite3"
BATCH_DB = DATA_DIR / "xml_batches.sqlite3"
XML_CORPUS = Path(os.environ.get("GRANTSIGHT_XML_DIR", "./data/xml"))
XML_CACHE = DATA_DIR / "xml-cache"

# The IRS publishes the corpus as yearly batch zips (70-500 MB each), named in
# the index CSV's XML_BATCH_ID column. A filing is pulled out of its zip with
# HTTP range requests, so only the zip's directory and that one member are
# ever downloaded.
BATCH_URL = os.environ.get(
    "GRANTSIGHT_XML_BATCH_URL",
    "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch}.zip",
)

# Optional per-object URL for operators who mirror the corpus themselves. The
# historical public per-object bucket (s3://irs-form-990) is gone, so no default.
OBJECT_URL = os.environ.get("GRANTSIGHT_XML_OBJECT_URL")

MAX_PEOPLE = 25
MAX_GRANTS = 50


# ---------------------------------------------------------------------------
# namespace-agnostic tree access
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class Node:
    """An element, searched by local name against candidate lists."""

    __slots__ = ("el", "matched")

    def __init__(self, el):
        self.el = el
        self.matched: dict[str, str] = {}

    def descendants(self, *names: str) -> Iterator["Node"]:
        wanted = set(names)
        for child in self.el.iter():
            if _local(child.tag) in wanted:
                yield Node(child)

    def first(self, *names: str) -> "Node | None":
        # Candidate order matters, so try each name across the whole subtree
        # rather than taking whichever appears first in document order.
        for name in names:
            for child in self.el.iter():
                if _local(child.tag) == name:
                    return Node(child)
        return None

    def text(self, *names: str) -> str | None:
        node = self.first(*names)
        if node is None:
            return None
        value = (node.el.text or "").strip()
        return value or None

    def number(self, *names: str) -> float | None:
        node = self.first(*names)
        if node is None:
            return None
        raw = (node.el.text or "").replace(",", "").replace("$", "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    def truth(self, *names: str) -> bool | None:
        value = self.text(*names)
        if value is None:
            return None
        return value.strip().lower() in ("1", "true", "x", "yes")

    def which(self, *names: str) -> str | None:
        """Which candidate actually matched, for provenance."""
        for name in names:
            for child in self.el.iter():
                if _local(child.tag) == name:
                    return name
        return None


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------
@dataclass
class FunctionalExpenses:
    """Part IX columns. `ratio` is program over total."""

    total: float | None = None
    program: float | None = None
    management: float | None = None
    fundraising: float | None = None
    source_tag: str | None = None

    @property
    def ratio(self) -> float | None:
        if self.program is None or not self.total:
            return None
        return self.program / self.total

    @property
    def overhead_ratio(self) -> float | None:
        if not self.total:
            return None
        parts = [p for p in (self.management, self.fundraising) if p is not None]
        return sum(parts) / self.total if parts else None

    @property
    def complete(self) -> bool:
        return None not in (self.total, self.program)


@dataclass
class Person:
    """A Part VII Section A listing, in organizational capacity only."""

    name: str
    title: str | None = None
    hours: float | None = None
    related_hours: float | None = None
    reportable_comp: float | None = None
    related_comp: float | None = None
    other_comp: float | None = None
    roles: list[str] = field(default_factory=list)
    is_institution: bool = False
    # Schedule J detail, when the person appears there too.
    base_comp: float | None = None
    bonus_comp: float | None = None
    deferred_comp: float | None = None
    benefits: float | None = None
    total_comp_schedule_j: float | None = None

    @property
    def total_comp(self) -> float | None:
        parts = [
            p for p in (self.reportable_comp, self.related_comp, self.other_comp)
            if p is not None
        ]
        return sum(parts) if parts else None

    @property
    def is_unpaid(self) -> bool:
        return self.total_comp in (0, None) and bool(self.roles)


@dataclass
class Grant:
    """A Schedule I Part II recipient. Names and EINs only, no addresses."""

    recipient: str
    ein: str | None = None
    purpose: str | None = None
    irc_section: str | None = None
    cash: float | None = None
    non_cash: float | None = None

    @property
    def total(self) -> float | None:
        parts = [p for p in (self.cash, self.non_cash) if p is not None]
        return sum(parts) if parts else None


@dataclass
class Form990XML:
    ein: str | None = None
    tax_year: int | None = None
    form_type: str | None = None
    name: str | None = None
    website: str | None = None
    mission: str | None = None
    principal_officer: str | None = None
    employee_count: float | None = None
    volunteer_count: float | None = None
    expenses: FunctionalExpenses = field(default_factory=FunctionalExpenses)
    people: list[Person] = field(default_factory=list)
    grants: list[Grant] = field(default_factory=list)
    grants_truncated: bool = False
    total_grants_reported: float | None = None
    no_listed_persons: bool = False
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def has_schedule_i(self) -> bool:
        return bool(self.grants) or self.total_grants_reported is not None

    @property
    def has_schedule_j(self) -> bool:
        return any(p.total_comp_schedule_j is not None for p in self.people)

    @property
    def highest_paid(self) -> Person | None:
        paid = [p for p in self.people if p.total_comp]
        return max(paid, key=lambda p: p.total_comp) if paid else None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
FORM_ROOTS = {"IRS990": "990", "IRS990EZ": "990-EZ", "IRS990PF": "990-PF"}

ROLE_TAGS = {
    "IndividualTrusteeOrDirectorInd": "trustee or director",
    "InstitutionalTrusteeInd": "institutional trustee",
    "OfficerInd": "officer",
    "KeyEmployeeInd": "key employee",
    "HighestCompensatedEmployeeInd": "highest compensated employee",
    "FormerOfcrDirectorTrusteeInd": "former",
    # pre-2013 spellings
    "IndividualTrusteeOrDirector": "trustee or director",
    "Officer": "officer",
    "KeyEmployee": "key employee",
    "HighestCompensatedEmployee": "highest compensated employee",
    "FormerOfficerDirectorTrustee": "former",
}

PERSON_GROUPS = (
    "Form990PartVIISectionAGrp",          # 2013+
    "Form990PartVIISectionA",             # legacy
    "Form990PartViiSectionAGrp",
)
SCHEDULE_J_GROUPS = (
    "RltdOrgOfficerTrstKeyEmplGrp",       # 2013+
    "RelatedOrgOfficerTrstKeyEmpl",       # legacy
)
GRANT_GROUPS = (
    "RecipientTable",
    "GrantsOtherAsstToGovtInsdUSGrp",     # some versions
    "RecipientEntity",
)


def _clean_name(value: str | None) -> str | None:
    if not value:
        return None
    # Collapse whitespace; some filers pad names heavily.
    return re.sub(r"\s+", " ", value).strip() or None


def _person_name(node: Node) -> tuple[str | None, bool]:
    """Individual name, or a business name for institutional trustees."""
    person = _clean_name(
        node.text("PersonNm", "NamePerson", "PersonName", "NameOfPerson")
    )
    if person:
        return person, False
    business = node.first("BusinessName", "BusinessNameLine1Txt", "NameBusiness")
    if business is not None:
        parts = [
            (child.el.text or "").strip()
            for child in business.descendants(
                "BusinessNameLine1Txt", "BusinessNameLine2Txt",
                "BusinessNameLine1", "BusinessNameLine2",
            )
        ]
        joined = _clean_name(" ".join(p for p in parts if p))
        if joined:
            return joined, True
        return _clean_name(business.el.text), True
    return None, False


def _parse_people(root: Node) -> tuple[list[Person], bool]:
    people: list[Person] = []
    no_listed = bool(root.truth("NoListedPersonsCompensatedInd"))

    for group in root.descendants(*PERSON_GROUPS):
        name, is_institution = _person_name(group)
        if not name:
            continue
        roles = [
            label for tag, label in ROLE_TAGS.items()
            if group.truth(tag) is True
        ]
        people.append(Person(
            name=name,
            title=_clean_name(group.text("TitleTxt", "Title")),
            hours=group.number("AverageHoursPerWeekRt", "AverageHoursPerWeek"),
            related_hours=group.number(
                "AverageHoursPerWeekRltdOrgRt", "AverageHoursPerWeekRelatedOrg"
            ),
            reportable_comp=group.number(
                "ReportableCompFromOrgAmt", "ReportableCompFromOrganization",
                "CompensationAmt",
            ),
            related_comp=group.number(
                "ReportableCompFromRltdOrgAmt",
                "ReportableCompFromRelatedOrgs",
            ),
            other_comp=group.number(
                "OtherCompensationAmt", "OtherCompensation",
            ),
            roles=sorted(set(roles)),
            is_institution=is_institution,
        ))

    _merge_schedule_j(root, people)
    people.sort(key=lambda p: (p.total_comp or -1), reverse=True)
    return people[:MAX_PEOPLE], no_listed


def _merge_schedule_j(root: Node, people: list[Person]) -> None:
    """Attach Schedule J detail to the matching Part VII person."""
    by_name = {p.name.upper(): p for p in people}
    for group in root.descendants(*SCHEDULE_J_GROUPS):
        name, _ = _person_name(group)
        if not name:
            continue
        person = by_name.get(name.upper())
        if person is None:
            person = Person(name=name, title=_clean_name(group.text("TitleTxt", "Title")))
            people.append(person)
            by_name[name.upper()] = person
        person.base_comp = group.number(
            "BaseCompensationFilingOrgAmt", "BaseCompensationFilingOrg"
        )
        person.bonus_comp = group.number(
            "BonusFilingOrganizationAmount", "BonusFilingOrganizationAmt",
            "BonusFilingOrganization",
        )
        person.deferred_comp = group.number(
            "DeferredCompensationFlngOrgAmt", "DeferredCompensationFilingOrg"
        )
        person.benefits = group.number(
            "NontaxableBenefitsFilingOrgAmt", "NontaxableBenefitsFilingOrg"
        )
        person.total_comp_schedule_j = group.number(
            "TotalCompensationFilingOrgAmt", "TotalCompensationFilingOrg"
        )


def _parse_grants(root: Node) -> tuple[list[Grant], bool, float | None]:
    grants: list[Grant] = []
    for group in root.descendants(*GRANT_GROUPS):
        name, _ = _person_name(group)
        if not name:
            name = _clean_name(group.text("RecipientNameBusiness", "RecipientName"))
        if not name:
            continue
        ein = group.text("RecipientEIN", "EINOfRecipient", "RecipientEin")
        grants.append(Grant(
            recipient=name,
            ein=re.sub(r"\D", "", ein).zfill(9) if ein and re.sub(r"\D", "", ein) else None,
            purpose=_clean_name(group.text("PurposeOfGrantTxt", "PurposeOfGrant")),
            irc_section=group.text("IRCSectionDesc", "IRCSection"),
            cash=group.number("CashGrantAmt", "AmountOfCashGrant"),
            non_cash=group.number("NonCashAssistanceAmt", "AmountOfNonCashAssistance"),
        ))

    total = root.number("Total501c3OrgCnt")  # count, not amount; ignored below
    reported = root.number(
        "GrantsToDomesticOrgsGrp", "TotalGrantsToOrgsAmt",
    )
    grants.sort(key=lambda g: (g.total or -1), reverse=True)
    truncated = len(grants) > MAX_GRANTS
    del total
    return grants[:MAX_GRANTS], truncated, reported


def parse(source: bytes | str | Path) -> Form990XML:
    """Parse one filing. Accepts bytes, an XML string, or a path."""
    if isinstance(source, Path):
        tree = ElementTree.parse(source).getroot()
    elif isinstance(source, bytes):
        tree = ElementTree.fromstring(source)
    else:
        tree = ElementTree.fromstring(source.encode("utf-8", "replace"))

    root = Node(tree)
    result = Form990XML()

    # Header. The preparer firm's name precedes the filer's in the header, so
    # scope the lookups to the Filer block when there is one.
    filer = root.first("Filer") or root
    ein = filer.text("EIN")
    if ein and re.sub(r"\D", "", ein):
        result.ein = re.sub(r"\D", "", ein).zfill(9)
    result.name = _clean_name(
        filer.text("BusinessNameLine1Txt", "BusinessNameLine1")
    )
    result.website = root.text("WebsiteAddressTxt", "WebsiteAddress")
    result.principal_officer = _clean_name(
        root.text("PrincipalOfficerNm", "NameOfPrincipalOfficerPerson")
    )
    result.mission = _clean_name(
        root.text("MissionDesc", "ActivityOrMissionDesc", "MissionDescription")
    )
    result.employee_count = root.number("TotalEmployeeCnt", "TotalNbrEmployees")
    result.volunteer_count = root.number("TotalVolunteersCnt", "TotalNbrVolunteers")

    year = root.text("TaxYr", "TaxYear")
    if year and year.strip().isdigit():
        result.tax_year = int(year.strip())
    else:
        period = root.text("TaxPeriodEndDt", "TaxPeriodEndDate")
        if period and len(period) >= 4 and period[:4].isdigit():
            result.tax_year = int(period[:4])

    for tag, label in FORM_ROOTS.items():
        if root.first(tag) is not None:
            result.form_type = label
            break

    # Part IX columns B, C, D -- the split the SOI extract does not carry.
    group = root.first("TotalFunctionalExpensesGrp", "TotalFunctionalExpenses")
    if group is not None:
        result.expenses = FunctionalExpenses(
            total=group.number("TotalAmt", "Total"),
            program=group.number("ProgramServicesAmt", "ProgramServices"),
            management=group.number("ManagementAndGeneralAmt", "ManagementAndGeneral"),
            fundraising=group.number("FundraisingAmt", "Fundraising"),
            source_tag=root.which("TotalFunctionalExpensesGrp", "TotalFunctionalExpenses"),
        )
        result.provenance["functional_expenses"] = result.expenses.source_tag or ""

    result.people, result.no_listed_persons = _parse_people(root)
    if result.people:
        result.provenance["people"] = root.which(*PERSON_GROUPS) or ""

    result.grants, result.grants_truncated, result.total_grants_reported = _parse_grants(root)
    if result.grants:
        result.provenance["grants"] = root.which(*GRANT_GROUPS) or ""

    return result


# ---------------------------------------------------------------------------
# locating a filing
# ---------------------------------------------------------------------------
def build_index(index_csvs: list[str]) -> dict:
    """Index the IRS per-year index CSVs: EIN -> object id, year, form, batch."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = XML_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        CREATE TABLE filings (
            ein TEXT, tax_year INTEGER, object_id TEXT, form_type TEXT, batch TEXT,
            PRIMARY KEY (ein, tax_year, object_id)
        );
        CREATE INDEX filings_ein ON filings (ein, tax_year DESC);
        """
    )

    loaded = 0
    for source in index_csvs:
        path = _resolve_source(source)
        if path is None:
            continue
        before = loaded
        for stream in _text_members(path):
            reader = csv.reader(stream)
            try:
                header = [h.strip().upper() for h in next(reader)]
            except StopIteration:
                continue
            cols = {name: i for i, name in enumerate(header)}
            ein_i = cols.get("EIN")
            obj_i = cols.get("OBJECT_ID")
            year_i = cols.get("TAX_PERIOD") or cols.get("TAXPERIOD")
            form_i = cols.get("RETURN_TYPE")
            batch_i = cols.get("XML_BATCH_ID")
            if ein_i is None or obj_i is None:
                print(f"  ! {path.name}: no EIN/OBJECT_ID column; saw {header[:6]}",
                      file=sys.stderr)
                continue
            rows = []
            for row in reader:
                if len(row) <= max(ein_i, obj_i):
                    continue
                ein = re.sub(r"\D", "", row[ein_i]).zfill(9)
                if len(ein) != 9:
                    continue
                year = None
                if year_i is not None and len(row) > year_i:
                    digits = re.sub(r"\D", "", row[year_i])
                    if len(digits) >= 4:
                        year = int(digits[:4])
                form = row[form_i].strip() if form_i is not None and len(row) > form_i else None
                batch = row[batch_i].strip() if batch_i is not None and len(row) > batch_i else None
                rows.append((ein, year, row[obj_i].strip(), form, batch or None))
                loaded += 1
                if len(rows) >= 10_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?)", rows
                    )
                    rows.clear()
            if rows:
                conn.executemany("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?)", rows)
        print(f"  {source}: {loaded - before:,} filings")
    conn.commit()
    conn.close()
    if not loaded:
        tmp.unlink(missing_ok=True)
        print("  !! no index rows loaded; xml_index.sqlite3 not written", file=sys.stderr)
        return {"rows": 0, "db": None}
    tmp.replace(XML_DB)
    print(f"  indexed {loaded:,} filings")
    return {"rows": loaded, "db": str(XML_DB)}


def _resolve_source(source: str) -> Path | None:
    """A local path, or an http(s) URL downloaded into DATA_DIR."""
    source = source.strip()
    if not source:
        return None
    if source.startswith(("http://", "https://")):
        digest = hashlib.sha256(source.encode()).hexdigest()[:12]
        try:
            path = get_bytes(source, DATA_DIR / f"xml-index-{digest}.csv")
        except FetchError as exc:
            print(f"\n  !! DOWNLOAD FAILED: {source}\n     {exc}\n", file=sys.stderr)
            return None
        head = path.open("rb").read(200).lstrip().lower()
        if head.startswith((b"<!doctype html", b"<html")):
            print(f"\n  !! {source}\n     returned an HTML page, not an index CSV. The "
                  f"directory year in the URL must match the filename year.\n",
                  file=sys.stderr)
            path.unlink(missing_ok=True)
            return None
        return path
    path = Path(source)
    if not path.is_file():
        what = "is a directory, not a file" if path.is_dir() else "does not exist"
        print(f"  ! {path} {what}", file=sys.stderr)
        return None
    return path


def _text_members(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".csv", ".txt")):
                    with archive.open(name) as handle:
                        yield io.TextIOWrapper(handle, encoding="latin-1", errors="replace")
    else:
        with path.open(encoding="latin-1", errors="replace") as handle:
            yield handle


@dataclass
class IndexedFiling:
    object_id: str
    tax_year: int | None
    form_type: str | None
    batch: str | None


# A 990-T (unrelated business income) carries none of the Part VII / Part IX
# content this module exists for, so it sorts after the information return.
_RETURN_PRIORITY = "CASE WHEN form_type LIKE '990T%' THEN 1 ELSE 0 END"


def index_is_current() -> bool:
    """True when xml_index.sqlite3 exists and has the batch column."""
    if not XML_DB.exists():
        return False
    conn = sqlite3.connect(f"file:{XML_DB}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(filings)")}
        return "batch" in cols
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def indexed_filings(ein: str, limit: int = 3) -> list[IndexedFiling]:
    """Most recent indexed filings for an EIN, newest first, 990-T last."""
    if not XML_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{XML_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT object_id, tax_year, form_type, batch FROM filings WHERE ein = ? "
            f"ORDER BY {_RETURN_PRIORITY}, tax_year DESC LIMIT ?",
            (clean_ein(ein), limit),
        ).fetchall()
        return [IndexedFiling(*r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def object_ids(ein: str, limit: int = 3) -> list[tuple[str, int | None]]:
    """Most recent object ids for an EIN, newest first."""
    return [(f.object_id, f.tax_year) for f in indexed_filings(ein, limit)]


def load_for(ein: str) -> Form990XML | None:
    """Find and parse the most recent XML filing available for an EIN.

    Looks in a local corpus directory, then the per-object cache, then pulls
    the member out of the IRS batch zip (or a configured per-object URL).
    Returns None when nothing is available -- callers degrade to the SOI-only
    brief and say which fields are therefore missing.
    """
    ein = clean_ein(ein)
    filings = indexed_filings(ein)

    for f in filings:
        for local in (XML_CORPUS / f"{f.object_id}_public.xml",
                      XML_CACHE / f"{f.object_id}.xml"):
            if local.exists():
                return parse(local)

    for f in filings:
        try:
            data = _fetch_object(f)
        except FetchError:
            continue
        if data is None:
            continue
        XML_CACHE.mkdir(parents=True, exist_ok=True)
        (XML_CACHE / f"{f.object_id}.xml").write_bytes(data)
        return parse(data)
    return None


def _fetch_object(f: IndexedFiling) -> bytes | None:
    if OBJECT_URL:
        path = get_bytes(
            OBJECT_URL.format(object_id=f.object_id),
            XML_CACHE / f"{f.object_id}.xml",
            ttl_s=60 * 60 * 24 * 30,
        )
        return path.read_bytes()
    if not f.batch or not BATCH_URL:
        return None
    url = BATCH_URL.format(year=f.batch[:4], batch=f.batch)
    return _remote_zip_member(url, f.batch, f"{f.object_id}_public.xml")


# ---------------------------------------------------------------------------
# one member out of a remote zip, by HTTP range requests
# ---------------------------------------------------------------------------
def _batch_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BATCH_DB, timeout=30)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS members (
            batch TEXT, name TEXT, method INTEGER, csize INTEGER, offset INTEGER,
            PRIMARY KEY (batch, name)
        );
        CREATE TABLE IF NOT EXISTS batches (batch TEXT PRIMARY KEY, entries INTEGER);
        """
    )
    return conn


def _remote_zip_member(url: str, batch: str, name: str) -> bytes | None:
    conn = _batch_conn()
    try:
        if not conn.execute("SELECT 1 FROM batches WHERE batch = ?", (batch,)).fetchone():
            entries = list(_remote_zip_directory(url))
            conn.executemany(
                "INSERT OR REPLACE INTO members VALUES (?,?,?,?,?)",
                [(batch, n, m, c, o) for n, m, c, o in entries],
            )
            conn.execute("INSERT OR REPLACE INTO batches VALUES (?,?)", (batch, len(entries)))
            conn.commit()
        row = conn.execute(
            "SELECT method, csize, offset FROM members WHERE batch = ? AND name = ?",
            (batch, name),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    method, csize, offset = row
    head = get_range(url, offset, offset + 29)
    if head[:4] != b"PK\x03\x04":
        raise FetchError(f"{url}: bad local header for {name}")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    start = offset + 30 + name_len + extra_len
    data = get_range(url, start, start + csize - 1)
    if method == 8:
        return zlib.decompress(data, -15)
    if method == 0:
        return data
    raise FetchError(f"{url}: unsupported compression method {method}")


def _remote_zip_directory(url: str) -> Iterator[tuple[str, int, int, int]]:
    """(name, method, compressed size, local header offset) for every member
    of the zip at `url`, reading only its central directory. Handles ZIP64."""
    size = content_length(url)
    tail_len = min(size, 1 << 16)
    tail_start = size - tail_len
    tail = get_range(url, tail_start, size - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise FetchError(f"{url}: not a zip file")
    cd_count, cd_size, cd_off = struct.unpack("<HII", tail[eocd + 10:eocd + 20])
    if cd_count == 0xFFFF or 0xFFFFFFFF in (cd_size, cd_off):
        loc = tail.rfind(b"PK\x06\x07")
        if loc < 0:
            raise FetchError(f"{url}: zip64 locator missing")
        z64_off = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        if z64_off >= tail_start:
            z64 = tail[z64_off - tail_start:z64_off - tail_start + 56]
        else:
            z64 = get_range(url, z64_off, z64_off + 55)
        cd_count, cd_size, cd_off = struct.unpack("<QQQ", z64[32:56])

    cd = get_range(url, cd_off, cd_off + cd_size - 1)
    p = 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        method = struct.unpack("<H", cd[p + 10:p + 12])[0]
        csize, usize = struct.unpack("<II", cd[p + 20:p + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[p + 28:p + 34])
        offset = struct.unpack("<I", cd[p + 42:p + 46])[0]
        name = cd[p + 46:p + 46 + name_len].decode("utf-8", "replace")
        extra = cd[p + 46 + name_len:p + 46 + name_len + extra_len]
        if 0xFFFFFFFF in (csize, usize, offset):
            q = 0
            while q + 4 <= len(extra):
                header_id, length = struct.unpack("<HH", extra[q:q + 4])
                if header_id == 1:
                    values = list(struct.unpack(f"<{length // 8}Q", extra[q + 4:q + 4 + length]))
                    if usize == 0xFFFFFFFF and values:
                        usize = values.pop(0)
                    if csize == 0xFFFFFFFF and values:
                        csize = values.pop(0)
                    if offset == 0xFFFFFFFF and values:
                        offset = values.pop(0)
                    break
                q += 4 + length
        yield name, method, csize, offset
        p += 46 + name_len + extra_len + comment_len


if __name__ == "__main__":  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Parse Form 990 e-file XML.")
    parser.add_argument("--build-index", action="extend", nargs="+", default=[],
                        metavar="INDEX_CSV",
                        help="IRS index CSV paths or URLs (one or more; repeatable)")
    parser.add_argument("--check-index", action="store_true",
                        help="exit 0 if the built index has the current schema")
    parser.add_argument("--file", type=Path, help="parse one XML file")
    parser.add_argument("--ein", help="find and parse the latest filing for an EIN")
    args = parser.parse_args()

    if args.check_index:
        raise SystemExit(0 if index_is_current() else 1)
    if args.build_index:
        if not build_index(args.build_index)["rows"]:
            raise SystemExit(1)
    target = parse(args.file) if args.file else (load_for(args.ein) if args.ein else None)
    if target is None:
        if not args.build_index:
            parser.print_help()
        raise SystemExit(0)

    print(f"{target.name} ({target.ein}) FY{target.tax_year} {target.form_type}")
    exp = target.expenses
    if exp.complete:
        print(f"  program {exp.program:,.0f} of {exp.total:,.0f} "
              f"= {exp.ratio:.1%}; management {exp.management or 0:,.0f}; "
              f"fundraising {exp.fundraising or 0:,.0f}")
    print(f"  {len(target.people)} people, {len(target.grants)} grants")
    for person in target.people[:5]:
        print(f"    {person.name} — {person.title or 'no title'}: "
              f"{person.total_comp or 0:,.0f}")
    print(json.dumps(target.provenance, indent=2))
