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
import io
import os
import re
import sqlite3
import zipfile
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

from .propublica import clean_ein

DATA_DIR = Path(os.environ.get("GRANTSIGHT_DATA", "./data"))
XML_DB = DATA_DIR / "xml_index.sqlite3"
XML_CORPUS = Path(os.environ.get("GRANTSIGHT_XML_DIR", "./data/xml"))

# ProPublica still exposes the current IRS XML by object id. The old
# `irs-form-990` S3 bucket was retired years ago and must not be the default.
# A caller can override this, but the built-in fallback is the current IRS
# monthly TEOS batch corpus (see `_load_from_irs_batch`).
OBJECT_URL = os.environ.get(
    "GRANTSIGHT_XML_OBJECT_URL",
    "https://projects.propublica.org/nonprofits/download-xml?object_id={object_id}",
)

MAX_PEOPLE = 25
MAX_GRANTS = 50


def default_index_urls(years: int = 4) -> list[str]:
    """Return the IRS XML index URLs for the current and recent processing years.

    The IRS publishes Form 990 e-file XML by *processing year*, not tax year.
    A return for FY2025 filed in 2026 therefore appears in the 2026 index.
    Keeping this derived from the current calendar year means the deployment
    automatically starts looking at 2027 when the IRS begins publishing it;
    no code or Railway variable needs to be edited each January.
    """
    current = datetime.utcnow().year
    return [
        f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
        for year in range(current, max(current - years + 1, 2019) - 1, -1)
    ]


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
class XmlFinancials:
    """Part VIII, IX and X, straight from the filing.

    This is the point of going XML-first: the SOI extract runs 12-24 months
    behind, so a brief built on it shows FY2023 when the FY2024 return has
    been public for a year. The XML also carries the prior-year column, so one
    filing yields two years of revenue and expenses.
    """

    total_revenue: float | None = None
    total_expenses: float | None = None
    prior_revenue: float | None = None
    prior_expenses: float | None = None
    contributions: float | None = None
    prior_contributions: float | None = None
    program_revenue: float | None = None
    investment_income: float | None = None
    grants_paid: float | None = None
    total_assets: float | None = None
    total_liabilities: float | None = None
    net_assets: float | None = None
    unrestricted_net_assets: float | None = None
    restricted_net_assets: float | None = None
    public_support: float | None = None
    total_support: float | None = None
    provenance: dict = field(default_factory=dict)

    @property
    def has_financials(self) -> bool:
        return self.total_revenue is not None or self.total_expenses is not None

    @property
    def public_support_ratio(self) -> float | None:
        if self.public_support is None or not self.total_support:
            return None
        return self.public_support / self.total_support


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
    financials: XmlFinancials = field(default_factory=XmlFinancials)
    filed_on: str | None = None
    period_end: str | None = None
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


# Candidate element names per metric, same discipline as the SOI field map:
# first match wins, a miss is None rather than zero, and what matched is
# recorded. The CY/PY pairs are the current and prior year columns of Part I.
_FINANCIAL_FIELDS = {
    "total_revenue": ("CYTotalRevenueAmt", "TotalRevenueAmt", "TotalRevenueCurrentYear"),
    "prior_revenue": ("PYTotalRevenueAmt", "TotalRevenuePriorYear"),
    "total_expenses": ("CYTotalExpensesAmt", "TotalExpensesAmt", "TotalExpensesCurrentYear"),
    "prior_expenses": ("PYTotalExpensesAmt", "TotalExpensesPriorYear"),
    "contributions": ("CYContributionsGrantsAmt", "ContributionsGrantsCurrentYear"),
    "prior_contributions": ("PYContributionsGrantsAmt", "ContributionsGrantsPriorYear"),
    "program_revenue": ("CYProgramServiceRevenueAmt", "ProgramServiceRevenueCY"),
    "investment_income": ("CYInvestmentIncomeAmt", "InvestmentIncomeCurrentYear"),
    "grants_paid": ("CYGrantsAndSimilarPaidAmt", "GrantsAndSimilarAmountsPaidCY"),
    "total_assets": ("TotalAssetsEOYAmt", "TotalAssetsEOY"),
    "total_liabilities": ("TotalLiabilitiesEOYAmt", "TotalLiabilitiesEOY"),
    "net_assets": ("NetAssetsOrFundBalancesEOYAmt", "NetAssetsOrFundBalancesEOY"),
}

# Part X lines 27-29. Post-2018 filings use the donor-restriction wording;
# earlier ones use unrestricted/temporarily/permanently.
_UNRESTRICTED_GROUPS = ("NoDonorRestrictionNetAssetsGrp", "UnrestrictedNetAssetsGrp")
_RESTRICTED_GROUPS = (
    "DonorRestrictionNetAssetsGrp",
    "TemporarilyRstrNetAssetsGrp",
    "PermanentlyRstrNetAssetsGrp",
)
_SCHEDULE_A_SUPPORT = {
    "public_support": ("PublicSupportTotal170Amt", "PublicSupportTotalAmt",
                       "TotalPublicSupportAmt", "PublicSupport170Amt"),
    "total_support": ("TotalSupportAmt", "TotalSupport170Amt", "GrossSupportAmt"),
}


def _parse_financials(root: Node) -> XmlFinancials:
    result = XmlFinancials()
    for metric, candidates in _FINANCIAL_FIELDS.items():
        value = root.number(*candidates)
        if value is not None:
            setattr(result, metric, value)
            result.provenance[metric] = root.which(*candidates) or ""

    for group_names, attribute in (
        (_UNRESTRICTED_GROUPS, "unrestricted_net_assets"),
        (_RESTRICTED_GROUPS, "restricted_net_assets"),
    ):
        total, used = None, []
        for name in group_names:
            for group in root.descendants(name):
                amount = group.number("EOYAmt", "EOY")
                if amount is not None:
                    total = (total or 0) + amount
                    used.append(name)
        if total is not None:
            setattr(result, attribute, total)
            result.provenance[attribute] = " + ".join(sorted(set(used)))

    for metric, candidates in _SCHEDULE_A_SUPPORT.items():
        value = root.number(*candidates)
        if value is not None:
            setattr(result, metric, value)
            result.provenance[metric] = root.which(*candidates) or ""
    return result


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

    # Header
    ein = root.text("EIN")
    if ein and re.sub(r"\D", "", ein):
        result.ein = re.sub(r"\D", "", ein).zfill(9)
    result.name = _clean_name(
        root.text("BusinessNameLine1Txt", "BusinessNameLine1")
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

    result.financials = _parse_financials(root)
    result.filed_on = root.text("ReturnTs", "Timestamp")
    result.period_end = root.text("TaxPeriodEndDt", "TaxPeriodEndDate")

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
def build_index(index_csvs: list[str] | None = None) -> dict:
    """Index IRS per-year CSVs, atomically replacing the prior index on success.

    If no URLs are supplied, derive the current and recent processing years from
    the calendar year. A failed refresh never replaces a previously working
    index with an empty/partial one.
    """
    defaults = default_index_urls()
    extras = index_csvs or []
    # Explicit URLs are additions, not replacements. This prevents an old
    # Railway variable from pinning the index to obsolete processing years.
    index_csvs = list(dict.fromkeys([*defaults, *extras]))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = XML_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        CREATE TABLE filings (
            ein TEXT, tax_year INTEGER, object_id TEXT, form_type TEXT,
            batch_id TEXT, processing_year INTEGER,
            PRIMARY KEY (ein, tax_year, object_id)
        );
        CREATE INDEX filings_ein ON filings (ein, tax_year DESC);
        """
    )

    loaded = 0
    for source in index_csvs:
        path = _materialize_index_source(source)
        if path is None:
            print(f"  ! {source} could not be downloaded/read")
            continue
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
                print(f"  ! {path.name}: no EIN/OBJECT_ID column; saw {header[:6]}")
                continue
            batch = []
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
                batch_id = row[batch_i].strip() if batch_i is not None and len(row) > batch_i else None
                processing_year = None
                m = re.match(r"(20\d{2})", batch_id or "")
                if m:
                    processing_year = int(m.group(1))
                batch.append((ein, year, row[obj_i].strip(), form, batch_id, processing_year))
                loaded += 1
                if len(batch) >= 10_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?)", batch
                    )
                    batch.clear()
            if batch:
                conn.executemany("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?)", batch)
    conn.commit()
    conn.close()

    if loaded == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("IRS XML index refresh loaded zero filings; keeping existing index")

    tmp.replace(XML_DB)
    print(f"  indexed {loaded:,} filings")
    return {"rows": loaded, "db": str(XML_DB), "sources": len(index_csvs)}



def _materialize_index_source(source: str) -> Path | None:
    """Accept either a local IRS index file or an IRS URL.

    RAILWAY.md documents URLs in GRANTSIGHT_XML_INDEX_URLS, so the builder
    must actually download them rather than treating the URL as a filename.
    """
    source = str(source).strip()
    if not source:
        return None
    if source.startswith(("https://", "http://")):
        from .http import FetchError, get_bytes
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", source.rsplit("/", 1)[-1] or "index.csv")
        try:
            return get_bytes(
                source, DATA_DIR / "xml-index-cache" / name,
                ttl_s=60 * 60 * 24 * 7,
            )
        except FetchError as exc:
            print(f"  ! {source}: {exc}")
            return None
    path = Path(source)
    return path if path.exists() else None

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


def _filing_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(filings)").fetchall()}


def object_ids(ein: str, limit: int = 3) -> list[tuple[str, int | None, str | None, int | None]]:
    """Most recent 990-family object ids for an EIN, newest first.

    The extra batch metadata lets us recover the XML from the IRS's current
    monthly TEOS ZIPs when ProPublica's convenience download is unavailable.
    Old four-column indexes remain readable for backwards compatibility.
    """
    if not XML_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{XML_DB}?mode=ro", uri=True)
    try:
        cols = _filing_columns(conn)
        batch_expr = "batch_id" if "batch_id" in cols else "NULL"
        proc_expr = "processing_year" if "processing_year" in cols else "NULL"
        rows = conn.execute(
            f"SELECT object_id, tax_year, {batch_expr}, {proc_expr} FROM filings "
            "WHERE ein = ? AND (form_type IS NULL OR UPPER(REPLACE(form_type, '-', '')) IN "
            "('990','990EZ','990PF')) ORDER BY tax_year DESC, object_id DESC LIMIT ?",
            (clean_ein(ein), limit),
        ).fetchall()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _load_from_irs_batch(object_id: str, batch_id: str | None, processing_year: int | None) -> Form990XML | None:
    """Extract one object from the IRS's current TEOS monthly ZIP corpus."""
    if not batch_id or not processing_year:
        return None
    from .http import FetchError, get_bytes

    url = f"https://apps.irs.gov/pub/epostcard/990/xml/{processing_year}/{batch_id}.zip"
    dest = DATA_DIR / "xml-batch-cache" / f"{batch_id}.zip"
    try:
        archive_path = get_bytes(url, dest, ttl_s=60 * 60 * 24 * 30)
        with zipfile.ZipFile(archive_path) as archive:
            wanted = f"{object_id}_public.xml".lower()
            member = next((n for n in archive.namelist() if n.lower().endswith(wanted)), None)
            if member is None:
                return None
            raw = archive.read(member)
        cache = DATA_DIR / "xml-cache" / f"{object_id}.xml"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
        return parse(raw)
    except (FetchError, zipfile.BadZipFile, KeyError, OSError):
        return None


def _load_object(row) -> Form990XML | None:
    object_id, _year, batch_id, processing_year = row
    local = XML_CORPUS / f"{object_id}_public.xml"
    if local.exists():
        return parse(local)

    from .http import FetchError, get_bytes

    # ProPublica's current XML endpoint is the smallest and fastest request.
    if OBJECT_URL:
        try:
            path = get_bytes(
                OBJECT_URL.format(object_id=object_id),
                DATA_DIR / "xml-cache" / f"{object_id}.xml",
                ttl_s=60 * 60 * 24 * 30,
            )
            return parse(path)
        except FetchError:
            pass

    return _load_from_irs_batch(object_id, batch_id, processing_year)


def load_for(ein: str) -> Form990XML | None:
    """Find and parse the most recent 990-family XML filing for an EIN.

    Local corpus -> ProPublica object download -> current IRS TEOS batch.
    Failures are returned as ``None`` so callers can still use structured
    ProPublica financial data, but the dead pre-2022 AWS bucket is no longer
    part of the default path.
    """
    ein = clean_ein(ein)
    for row in object_ids(ein):
        parsed = _load_object(row)
        if parsed is not None:
            return parsed
    return None


def load_years(ein: str, limit: int = 3) -> list[Form990XML]:
    """Parse the most recent `limit` 990-family filings, newest first."""
    ein = clean_ein(ein)
    out: list[Form990XML] = []
    for row in object_ids(ein, limit=limit):
        parsed = _load_object(row)
        if parsed is not None:
            out.append(parsed)
    out.sort(key=lambda f: f.tax_year or 0, reverse=True)
    return out

def leadership_change(current: Form990XML, prior: Form990XML) -> dict:
    """Who left, who arrived, and whether the top role changed hands.

    Compared on normalized names from Part VII Section A. Institutions are
    excluded: a corporate trustee is not leadership turnover.
    """
    def roster(filing: Form990XML) -> dict[str, Person]:
        return {
            re.sub(r"[^a-z ]", "", p.name.lower()).strip(): p
            for p in filing.people if not p.is_institution and p.name
        }

    now, before = roster(current), roster(prior)
    departed = [before[k] for k in before.keys() - now.keys()]
    arrived = [now[k] for k in now.keys() - before.keys()]

    def top(filing: Form990XML) -> Person | None:
        paid = [p for p in filing.people if p.total_comp and not p.is_institution]
        return max(paid, key=lambda p: p.total_comp) if paid else None

    top_now, top_before = top(current), top(prior)
    top_changed = bool(
        top_now and top_before
        and top_now.name.lower().strip() != top_before.name.lower().strip()
    )

    total_before = len(before) or 1
    return {
        "departed": [p.name for p in departed],
        "arrived": [p.name for p in arrived],
        "departed_count": len(departed),
        "arrived_count": len(arrived),
        "turnover_rate": len(departed) / total_before,
        "top_role_changed": top_changed,
        "previous_top": top_before.name if top_before else None,
        "current_top": top_now.name if top_now else None,
        "compared_years": [current.tax_year, prior.tax_year],
    }


if __name__ == "__main__":  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Parse Form 990 e-file XML.")
    parser.add_argument("--build-index", action="append", default=[],
                        help="IRS index CSV path (repeatable)")
    parser.add_argument("--file", type=Path, help="parse one XML file")
    parser.add_argument("--ein", help="find and parse the latest filing for an EIN")
    args = parser.parse_args()

    if args.build_index:
        build_index(args.build_index)
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
