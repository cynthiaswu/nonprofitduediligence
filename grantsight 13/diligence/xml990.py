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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree

from .propublica import clean_ein

DATA_DIR = Path(os.environ.get("GRANTSIGHT_DATA", "./data"))
XML_DB = DATA_DIR / "xml_index.sqlite3"
XML_CORPUS = Path(os.environ.get("GRANTSIGHT_XML_DIR", "./data/xml"))

# Per-object mirrors, tried AFTER the IRS monthly zip (see irszip.py).
#
# Every per-object URL was measured on 20 September 2026 against object id
# 202641349349313769 -- Planned Parenthood Federation of America's FY2025
# return, filed 14 May 2026:
#
#   s3.amazonaws.com/irs-form-990          NoSuchKey. The IRS said on 16 Dec
#                                          2021 it would stop updating this
#                                          bucket, and it did.
#   gt990datalake-rawdata (GivingTuesday)  NoSuchKey. The mirror carries older
#                                          filings; recent ones are absent.
#   projects.propublica.org/download-xml   HTTP 403 "Security Check". The link
#                                          works in a browser and is refused to
#                                          servers. Not a usable source.
#
# So these mirrors answer quickly for older filings and cannot answer for
# recent ones at all. Recency and current figures come from the IRS monthly
# archives instead, which is the only distribution the IRS actually operates.
# They are kept here because they are cheap when they do hit, and because a
# local corpus or a private mirror can be supplied through the environment.
OBJECT_URL_TEMPLATES = [
    url for url in (
        os.environ.get("GRANTSIGHT_XML_OBJECT_URL"),
        "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/"
        "{object_id}_public.xml",
    ) if url
]
OBJECT_URL = OBJECT_URL_TEMPLATES[0] if OBJECT_URL_TEMPLATES else ""

# Why the last fetch failed, so a silent miss can be told from a real absence.
LAST_FETCH_ERROR: str | None = None

# Which source actually served the last document, so the answer to "where did
# this number come from" is recorded rather than inferred.
LAST_SUCCESSFUL_SOURCE: str | None = None

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
        # Scoped to the Filer. In the ReturnHeader schema PreparerFirmGrp comes
        # BEFORE Filer, so an unscoped search for a business name returns the
        # accounting firm that prepared the return -- Planned Parenthood's
        # FY2025 filing resolved to "KPMG LLP" this way.
        (root.first("Filer") or root).text(
            "BusinessNameLine1Txt", "BusinessNameLine1"
        )
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

    # Fiscal year END, not <TaxYr>.
    #
    # <TaxYr> is the year the tax year BEGINS. Planned Parenthood's return for
    # the year ending 30 June 2025 carries <TaxYr>2024</TaxYr>. ProPublica, the
    # IRS index (TAX_PERIOD 202506) and every funder call that FY2025, so
    # preferring <TaxYr> labelled the newest filing a year early and made a
    # current return look like a stale one -- the same symptom, from a third
    # cause. The period end is the authority; <TaxYr> is only a fallback for
    # filings that omit it.
    period = root.text("TaxPeriodEndDt", "TaxPeriodEndDate")
    year = root.text("TaxYr", "TaxYear")
    if period and len(period) >= 4 and period[:4].isdigit():
        result.tax_year = int(period[:4])
    elif year and year.strip().isdigit():
        result.tax_year = int(year.strip())

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
def default_index_urls(years: int = 4) -> list[str]:
    """Current processing year back `years`, as IRS index CSV URLs.

    The index year is the year the IRS PROCESSED the return, not the tax year
    it covers: a FY2025 return filed in April 2026 appears in the 2026 index.
    Indexing only the tax years you care about therefore misses exactly the
    most recent filings.
    """
    from datetime import date as _date

    current = _date.today().year
    return [
        f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
        for year in range(current, current - years, -1)
    ]


def _materialize_index_source(source: str) -> Path | None:
    """Accept a URL or a local path. An earlier version accepted only paths,
    so passing the documented URLs indexed precisely nothing."""
    if str(source).startswith("http"):
        from .http import FetchError, get_bytes

        name = re.sub(r"\W+", "_", str(source).rsplit("/", 1)[-1]) or "index"
        try:
            return get_bytes(source, DATA_DIR / "xml-index" / name,
                             ttl_s=60 * 60 * 24 * 7)
        except FetchError as exc:
            print(f"  ! {source}: {exc}")
            return None
    path = Path(source)
    return path if path.exists() else None


def build_index(index_csvs: list[str] | None = None) -> dict:
    """Index the IRS per-year index CSVs: EIN -> object id, year, form type."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = XML_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        CREATE TABLE filings (
            ein TEXT, tax_year INTEGER, object_id TEXT, form_type TEXT,
            sub_date TEXT, batch_id TEXT,
            PRIMARY KEY (ein, tax_year, object_id)
        );
        CREATE INDEX filings_ein ON filings (ein, tax_year DESC);
        """
    )

    # Explicit URLs add to the defaults rather than replacing them, so a stale
    # environment variable cannot pin the index to obsolete processing years.
    index_csvs = list(dict.fromkeys([*default_index_urls(), *(index_csvs or [])]))

    loaded = 0
    for source in index_csvs:
        path = _materialize_index_source(source)
        if path is None:
            print(f"  ! {source}: could not be downloaded or read")
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
            year_i = cols.get("TAX_PERIOD")
            if year_i is None:
                year_i = cols.get("TAXPERIOD")
            form_i = cols.get("RETURN_TYPE")
            # SUB_DATE sounds like a date and is not: in every index from 2023
            # to 2026 it holds the four-digit processing year and nothing more.
            # Deriving a month from it is impossible, which is why the first
            # attempt at locating documents failed.
            #
            # XML_BATCH_ID is the real answer, and it is better than a month:
            # it names the exact archive the filing was published in, e.g.
            # "2026_TEOS_XML_05A". Present from the 2024 index onward; absent
            # in 2023 and earlier, where the mirrors still work.
            #
            # `is None` rather than `or`, because column zero is a valid index
            # and a falsy one.
            sub_i = cols.get("SUB_DATE")
            if sub_i is None:
                sub_i = cols.get("SUBMISSION_DATE")
            batch_i = cols.get("XML_BATCH_ID")
            if batch_i is None:
                batch_i = cols.get("XMLBATCHID")
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
                sub = row[sub_i].strip() if sub_i is not None and len(row) > sub_i else None
                bid = row[batch_i].strip() if batch_i is not None and len(row) > batch_i else None
                batch.append((ein, year, row[obj_i].strip(), form, sub, bid or None))
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
    tmp.replace(XML_DB)
    print(f"  indexed {loaded:,} filings")
    return {"rows": loaded, "db": str(XML_DB)}


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


def object_ids(ein: str, limit: int = 3) -> list[tuple[str, int | None]]:
    """Most recent object ids for an EIN, newest first."""
    if not XML_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{XML_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT object_id, tax_year FROM filings WHERE ein = ? "
            "ORDER BY tax_year DESC LIMIT ?",
            (clean_ein(ein), limit),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def indexed_filings(ein: str, limit: int = 10) -> list[dict]:
    """Every return the IRS index says this EIN filed, newest first.

    This is deliberately separate from `load_years`. `load_years` returns the
    filings whose *documents* could be downloaded and parsed; this returns the
    filings the IRS says exist. They are different questions and conflating
    them is what produced the bug this function exists to kill: when the
    document fetch 404s, the app threw away its own index's knowledge that a
    FY2025 return was on file and fell back to ProPublica's FY2023, then
    reported the organization as three years late. The IRS index is the
    authority on *whether and when* a return was filed. A failed download says
    nothing about that.
    """
    if not XML_DB.exists():
        return []
    conn = sqlite3.connect(f"file:{XML_DB}?mode=ro", uri=True)
    try:
        has_sub = _has_column(conn, "filings", "sub_date")
        has_batch = _has_column(conn, "filings", "batch_id")
        cols = "object_id, tax_year, form_type"
        cols += ", sub_date" if has_sub else ", NULL"
        cols += ", batch_id" if has_batch else ", NULL"
        rows = conn.execute(
            f"SELECT {cols} FROM filings WHERE ein = ? "
            "ORDER BY tax_year DESC LIMIT ?",
            (clean_ein(ein), limit),
        ).fetchall()
        return [
            {
                "object_id": r[0],
                "tax_year": r[1],
                "form_type": r[2],
                "sub_date": r[3],
                "batch_id": r[4],
            }
            for r in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def latest_indexed_year(ein: str) -> int | None:
    """Newest tax year the IRS index has a return for. No network."""
    for row in indexed_filings(ein, limit=1):
        if row["tax_year"]:
            return int(row["tax_year"])
    return None


def load_for(ein: str) -> Form990XML | None:
    """Find and parse the most recent XML filing available for an EIN.

    Returns None when nothing is available; `LAST_FETCH_ERROR` then says
    whether that was because the organization has no indexed filing or because
    every source refused to serve it.
    """
    global LAST_FETCH_ERROR
    LAST_FETCH_ERROR = None
    ein = clean_ein(ein)

    found = object_ids(ein)
    if not found:
        LAST_FETCH_ERROR = (
            "no filing for this EIN in the XML index"
            if XML_DB.exists() else
            "XML index not built (set GRANTSIGHT_XML_INDEX_URLS)"
        )
        return None

    for row in indexed_filings(ein, limit=3):
        parsed = _load_object(row["object_id"], row.get("batch_id"))
        if parsed is not None:
            return parsed
    return None


def load_years(ein: str, limit: int = 3) -> list[Form990XML]:
    """Parse the most recent `limit` filings, newest first.

    Leadership turnover needs two years side by side, so a single-filing
    loader is not enough.
    """
    ein = clean_ein(ein)
    out: list[Form990XML] = []
    for row in indexed_filings(ein, limit=limit):
        parsed = _load_object(row["object_id"], row.get("batch_id"))
        if parsed is not None:
            out.append(parsed)
    out.sort(key=lambda f: f.tax_year or 0, reverse=True)
    return out


def _load_object(object_id: str, batch_id: str | None = None) -> Form990XML | None:
    """Local corpus first, then each configured per-object source in turn.

    Records why it failed rather than returning a bare None: an unreachable
    source and an organization with no filing are different problems and were
    indistinguishable here for far too long.
    """
    global LAST_FETCH_ERROR

    local = XML_CORPUS / f"{object_id}_public.xml"
    if local.exists():
        return parse(local)

    from .http import FetchError, get_bytes

    if not OBJECT_URL_TEMPLATES:
        LAST_FETCH_ERROR = (
            "no per-object source configured and no local corpus at "
            f"{XML_CORPUS}"
        )
        return None

    global LAST_SUCCESSFUL_SOURCE
    errors = []

    # The IRS monthly zip first: it is the only source that carries filings
    # from the last two years, which is precisely the window a funder cares
    # about. The mirrors below are kept because they answer instantly for
    # older filings, but they 404 on anything recent.
    if batch_id:
        from . import irszip
        try:
            path = irszip.fetch_object(object_id, batch_id)
            parsed = parse(path)
            LAST_FETCH_ERROR = None
            LAST_SUCCESSFUL_SOURCE = "IRS monthly zip"
            return parsed
        except irszip.ZipSourceError as exc:
            errors.append(f"IRS monthly zip: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"IRS monthly zip: {exc}")

    for template in OBJECT_URL_TEMPLATES:
        url = template.format(object_id=object_id)
        try:
            path = get_bytes(
                url, DATA_DIR / "xml-cache" / f"{object_id}.xml",
                ttl_s=60 * 60 * 24 * 30,
            )
            parsed = parse(path)
            LAST_FETCH_ERROR = None
            LAST_SUCCESSFUL_SOURCE = url
            return parsed
        except FetchError as exc:
            errors.append(f"{url}: {exc}")
        except Exception as exc:  # noqa: BLE001 - malformed XML at the source
            errors.append(f"{url}: unparseable ({exc})")
    LAST_FETCH_ERROR = "; ".join(errors)
    return None


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


def diagnose_index(ein: str, source: str) -> dict:
    """Search one raw IRS index file for an EIN and report what is really there.

    Reads the actual header and the actual matching rows rather than assuming
    a schema. Three wrong diagnoses were made about this file without anyone
    looking at it.
    """
    ein = clean_ein(ein)
    report: dict = {"ein": ein, "source": source}
    path = _materialize_index_source(source)
    if path is None:
        report["error"] = "could not download or open this index"
        return report

    report["bytes"] = path.stat().st_size
    matches, rows_scanned, header = [], 0, None
    for stream in _text_members(path):
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            continue
        report["header"] = header
        for row in reader:
            rows_scanned += 1
            if any(re.sub(r"\D", "", cell or "").zfill(9) == ein for cell in row[:4]):
                matches.append(dict(zip(header, row)))
                if len(matches) >= 5:
                    break
        if matches:
            break

    report["rows_scanned"] = rows_scanned
    report["matches"] = matches
    report["verdict"] = (
        f"{len(matches)} row(s) for this EIN in {source.rsplit('/', 1)[-1]}"
        if matches else
        f"EIN absent from {source.rsplit('/', 1)[-1]} after scanning "
        f"{rows_scanned:,} rows. Either it filed in a different processing "
        f"year, or this file does not contain it."
    )
    return report


def diagnose(ein: str) -> dict:
    """Walk the whole chain for one EIN and report where it breaks.

    Written because three separate wrong diagnoses were made from the outside:
    the index was fine, the object id resolved, and the fetch was failing
    silently at the last step.
    """
    global LAST_FETCH_ERROR
    ein = clean_ein(ein)
    report: dict = {
        "ein": ein,
        "index_exists": XML_DB.exists(),
        "index_path": str(XML_DB),
        "local_corpus": str(XML_CORPUS),
        "sources": list(OBJECT_URL_TEMPLATES),
    }

    rows = indexed_filings(ein, limit=5)
    found = [(r["object_id"], r["tax_year"]) for r in rows]
    report["object_ids"] = rows
    report["latest_indexed_year"] = latest_indexed_year(ein)
    report["recency_source"] = (
        "IRS e-file index (independent of whether the document downloads)"
    )
    if not found:
        report["verdict"] = (
            "This EIN has no filing in the XML index. Either the organization "
            "has not e-filed, or the year it filed in was not indexed."
        )
        return report

    LAST_FETCH_ERROR = None
    top = rows[0]
    parsed = _load_object(top["object_id"], top.get("batch_id"))
    if parsed is not None:
        report["verdict"] = (
            f"Working: fetched and parsed FY{parsed.tax_year}, "
            f"revenue {parsed.financials.total_revenue}"
        )
        report["fetched"] = True
        report["served_by"] = LAST_SUCCESSFUL_SOURCE
    else:
        report["fetched"] = False
        report["fetch_error"] = LAST_FETCH_ERROR
        report["verdict"] = (
            f"FY{top['tax_year']} return is on file with the IRS and filing "
            "recency reflects that, but no source would serve the document, "
            "so the published figures stay on the last year that has them. "
            "See source_probe for which source failed and how."
        )

    # Measure every source rather than reasoning about them. This block is the
    # reason this file stopped being guesswork.
    from . import irszip
    report["source_probe"] = {
        "irs_monthly_zip": irszip.probe(top["object_id"], top.get("batch_id")),
        "mirrors": _probe_mirrors(top["object_id"]),
    }
    return report


def _probe_mirrors(object_id: str) -> list[dict]:
    """GET each configured per-object mirror and report exactly what came back."""
    import httpx

    out = []
    for template in OBJECT_URL_TEMPLATES:
        url = template.format(object_id=object_id)
        entry: dict = {"url": url}
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                response = client.get(url)
            body = response.content[:200].decode("utf-8", "replace")
            entry.update(
                status=response.status_code,
                bytes=len(response.content),
                usable=response.status_code == 200 and "<Error" not in body,
                head=body,
            )
        except Exception as exc:  # noqa: BLE001
            entry.update(usable=False, error=str(exc)[:300])
        out.append(entry)
    return out


if __name__ == "__main__":  # pragma: no cover - CLI
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Parse Form 990 e-file XML.")
    # Zero URLs is a valid and useful invocation: build_index always merges in
    # the current default processing years, so `--build-index` on its own is
    # the right way to refresh. Requiring a value meant an empty environment
    # variable turned the refresh into an argparse error instead.
    parser.add_argument("--build-index", action="extend", nargs="*", default=None,
                        metavar="URL", help="build the index (optional extra CSV URLs)")
    parser.add_argument("--file", type=Path, help="parse one XML file")
    parser.add_argument("--ein", help="find and parse the latest filing for an EIN")
    parser.add_argument("--diagnose", metavar="EIN",
                        help="report exactly where the XML lookup breaks")
    parser.add_argument("--diagnose-index", metavar="EIN",
                        help="search a raw IRS index file for an EIN")
    parser.add_argument("--url", help="index CSV to search with --diagnose-index")
    args = parser.parse_args()

    if args.diagnose_index:
        sources = [args.url] if args.url else default_index_urls()
        for source in sources:
            print(json.dumps(diagnose_index(args.diagnose_index, source), indent=2))
        raise SystemExit(0)

    if args.diagnose:
        print(json.dumps(diagnose(args.diagnose), indent=2))
        raise SystemExit(0)

    if args.build_index is not None:
        build_index(args.build_index)
    target = parse(args.file) if args.file else (load_for(args.ein) if args.ein else None)
    if target is None:
        if args.build_index is None:
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
