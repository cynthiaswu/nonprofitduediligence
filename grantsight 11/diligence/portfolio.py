"""Watch a whole portfolio, not one organization at a time.

This is the thing ProPublica deliberately does not do. Their site answers
"tell me about this organization" better than a brief ever will, and for free.
What no public tool does is take a funder's grantee list and answer "which of
these forty needs attention this quarter".

Signals, in the order a program officer would want them:

  revoked            exemption auto-revoked, or revoked and reinstated
  late               no filing on record for long enough to matter
  revenue_drop       a sharp year-over-year fall
  deficit            expenses over revenue, and for how many years running
  funder_dependence  public support below the one-third test, or a single
                     donor over the 2% threshold dominating support
  leadership         officers who left, arrived, or a change at the top
  size               where each organization sits against its sector

Two design rules carried over from the single-organization brief. A signal
fires on a number, and that number and its fiscal year travel with it. And
anything not determined is stated, never inferred -- a portfolio report that
quietly omits the three organizations whose data failed to load is worse than
one that names them.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from . import irs_status, peers as peers_mod, xml990
from .http import FetchError
from .metrics import THRESHOLDS
from .normalize import normalize_filings
from .propublica import (
    NotFound,
    fetch_organization,
    filing_years,
    format_ein,
    search as search_orgs,
)

# Portfolio thresholds, separate from the single-brief ones so a funder can
# tune "flag it in the quarterly sweep" differently from "flag it in a brief".
PORTFOLIO_THRESHOLDS = {
    "grant_share_high": 0.25,
    "grant_share_dominant": 0.5,
    "months_reserve_low": 3.0,
    "late_filing_years": 2.0,
    "revenue_drop": 0.25,
    "deficit_years": 2,
    "public_support_low": 0.34,
    "donor_concentration": 0.25,
    "leadership_turnover": 0.34,
}

CRITICAL, WATCH, CONTEXT = "critical", "watch", "context"


@dataclass
class Signal:
    kind: str
    severity: str
    headline: str
    detail: str
    fiscal_year: int | None = None


@dataclass
class OrgResult:
    ein: str
    name: str | None = None
    listed_as: str | None = None
    city: str | None = None
    state: str | None = None
    ntee: str | None = None
    latest_fiscal_year: int | None = None
    # The most recent return ON FILE, which is not the same as the most recent
    # one with published financials. ProPublica returns both lists; reading
    # only the one with numbers makes a current filer look like a delinquent.
    latest_filing_year: int | None = None
    revenue: float | None = None
    expenses: float | None = None
    source: str = "extract"
    signals: list[Signal] = field(default_factory=list)
    percentile: int | None = None
    peer_population: int | None = None
    award: float | None = None
    months_reserve: float | None = None
    is_foundation: bool = False
    error: str | None = None

    @property
    def grant_share(self) -> float | None:
        """Your award as a share of their total revenue.

        The one number no public database can produce, because none of them
        know what you granted. It answers the question a program officer
        actually has: if we stop, do they survive, and are we already their
        dominant funder?
        """
        if not self.award or not self.revenue:
            return None
        return self.award / self.revenue

    @property
    def severity(self) -> str:
        for level in (CRITICAL, WATCH, CONTEXT):
            if any(s.severity == level for s in self.signals):
                return level
        return "clear"

    @property
    def needs_attention(self) -> bool:
        return self.severity in (CRITICAL, WATCH)


@dataclass
class PortfolioReport:
    generated_at: str = ""
    organizations: list[OrgResult] = field(default_factory=list)
    failed: list[OrgResult] = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    size_distribution: dict = field(default_factory=dict)

    @property
    def attention(self) -> list[OrgResult]:
        order = {CRITICAL: 0, WATCH: 1, CONTEXT: 2, "clear": 3}
        return sorted(
            [o for o in self.organizations if o.needs_attention],
            key=lambda o: (order[o.severity], -(o.revenue or 0)),
        )

    @property
    def clear(self) -> list[OrgResult]:
        return [o for o in self.organizations if not o.needs_attention]

    def count_of(self, kind: str) -> int:
        return len([
            o for o in self.organizations if any(s.kind == kind for s in o.signals)
        ])

    @property
    def summary_counts(self) -> dict[str, int]:
        return {
            kind: self.count_of(kind)
            for kind in ("revoked", "late", "revenue_drop", "deficit",
                         "funder_dependence", "leadership")
        }

    @property
    def total_revenue(self) -> float:
        return sum(o.revenue or 0 for o in self.organizations)

    @property
    def total_awarded(self) -> float:
        return sum(o.award or 0 for o in self.organizations)

    @property
    def has_awards(self) -> bool:
        return any(o.award for o in self.organizations)

    @property
    def dollars_at_risk(self) -> float:
        """Your money sitting in organizations that were flagged.

        A count of flagged grantees treats a $5,000 grant and a $500,000 grant
        alike. This is the number that decides whether the portfolio's
        problems are material.
        """
        return sum(o.award or 0 for o in self.organizations if o.needs_attention)

    @property
    def dollars_in_thin_reserve(self) -> float:
        return sum(
            o.award or 0 for o in self.organizations
            if o.months_reserve is not None
            and o.months_reserve < PORTFOLIO_THRESHOLDS["months_reserve_low"]
        )

    @property
    def dominant_funder_of(self) -> list:
        return sorted(
            [o for o in self.organizations
             if (o.grant_share or 0) >= PORTFOLIO_THRESHOLDS["grant_share_high"]],
            key=lambda o: -(o.grant_share or 0),
        )


def _money(value: float | None) -> str:
    if value is None:
        return "not determined"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:,.1f}M"
    if value >= 1_000:
        return f"{sign}${value / 1_000:,.0f}K"
    return f"{sign}${value:,.0f}"


def _pct(value: float | None) -> str:
    return "n/d" if value is None else f"{value * 100:.0f}%"


# ---------------------------------------------------------------------------
# resolving names to EINs
# ---------------------------------------------------------------------------
@dataclass
class NameMatch:
    """One line of input that named an organization instead of an EIN."""

    query: str
    ein: str | None = None
    matched_name: str | None = None
    candidates: list = field(default_factory=list)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.ein is not None


def _normalize_name(value: str) -> str:
    """Compare names with legal suffixes removed.

    Without this, "HOPE HOUSE" matches the query exactly while "HOPE HOUSE
    INC" does not, so three candidates collapse to one apparent exact match
    and the wrong organization silently enters the portfolio. They are the
    same name; the difference is a filing convention.
    """
    from .profile import press_name

    stripped = press_name(value or "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", stripped.lower())).strip()


def resolve_name(query: str) -> NameMatch:
    """Turn an organization name into an EIN, or report why it could not.

    Never guesses. A name that matches several organizations comes back with
    the candidates attached so a person can choose, because quietly picking
    the first result would put the wrong organization in a funder's portfolio
    report -- the same collision problem as press matching, with worse
    consequences.
    """
    match = NameMatch(query=(query or "").strip())
    if not match.query:
        match.reason = "empty"
        return match

    try:
        results = (search_orgs(match.query).get("organizations") or [])
        if not results:
            results = (search_orgs(match.query, c_code=None).get("organizations") or [])
    except FetchError as exc:
        match.reason = f"search failed: {exc}"
        return match

    if not results:
        match.reason = "no organization of that name is in ProPublica's index"
        return match

    wanted = _normalize_name(match.query)
    exact = [r for r in results if _normalize_name(r.get("name", "")) == wanted]

    if len(exact) == 1:
        results = exact
    elif len(exact) > 1:
        match.candidates = exact[:8]
        match.reason = (
            f"{len(exact)} organizations are registered under that exact name"
        )
        return match
    elif len(results) > 1:
        match.candidates = results[:8]
        match.reason = f"{len(results)} organizations match that name"
        return match

    chosen = results[0]
    match.ein = str(chosen.get("ein", "")).zfill(9)
    match.matched_name = chosen.get("name")
    return match


def resolve_names(queries: list[str]) -> tuple[dict[str, str], list[NameMatch]]:
    """Resolve many names. Returns ({ein: name}, unresolved matches)."""
    resolved: dict[str, str] = {}
    unresolved: list[NameMatch] = []
    for query in queries:
        match = resolve_name(query)
        if match.resolved:
            resolved[match.ein] = match.matched_name or query
        else:
            unresolved.append(match)
    return resolved, unresolved


# ---------------------------------------------------------------------------
# one organization
# ---------------------------------------------------------------------------
def evaluate_one(ein: str, listed_as: str | None = None,
                 today: date | None = None, use_xml: bool = True,
                 award: float | None = None) -> OrgResult:
    today = today or date.today()
    result = OrgResult(ein=format_ein(ein), listed_as=listed_as, award=award)

    status = irs_status.check(ein)
    record = None
    try:
        record = fetch_organization(ein)
    except NotFound:
        pass
    except FetchError as exc:
        result.error = f"Could not reach ProPublica: {exc}"
        return result

    if record:
        org = record["organization"]
        result.name = org.get("name")
        result.city = (org.get("city") or "").title() or None
        result.state = org.get("state")
        result.ntee = org.get("ntee_code")
    else:
        result.name = irs_status.lookup_name(ein) or listed_as

    filings = normalize_filings(
        (record or {}).get("filings_with_data") or [], ein
    )

    # Filing recency comes from ProPublica, which knows about returns whose
    # figures are not published yet. The XML can raise this but never lower
    # it: a dead XML source must not make a known filing vanish.
    years = filing_years(record or {})
    filed_years = list(years["all"])

    # XML first: the SOI extract runs 12-24 months behind, so an extract-only
    # portfolio sweep can miss a whole year of trouble.
    xml_years: list = []
    if use_xml:
        try:
            xml_years = xml990.load_years(ein, limit=3)
        except Exception:  # noqa: BLE001 - XML is an enhancement, not a gate
            xml_years = []

    latest_xml = xml_years[0] if xml_years else None
    filed_years += [x.tax_year for x in xml_years if x.tax_year]

    # The IRS e-file index knows a return exists whether or not its document
    # can be downloaded, and it is months ahead of ProPublica's extract. This
    # line is the fix for the long-standing bug where an organization that had
    # filed for FY2025 was reported as not having filed since FY2023: the
    # document fetch failed, and a failed download was being read as an
    # absent return. Whether a return was filed and whether we can read it are
    # different questions, and only the first one decides lateness.
    if use_xml:
        try:
            indexed = xml990.latest_indexed_year(ein)
            if indexed:
                filed_years.append(indexed)
        except Exception:  # noqa: BLE001
            pass

    result.latest_filing_year = max(filed_years) if filed_years else None
    if latest_xml and latest_xml.financials.has_financials:
        money = latest_xml.financials
        result.source = "xml"
        result.latest_fiscal_year = latest_xml.tax_year
        result.revenue = money.total_revenue
        result.expenses = money.total_expenses
    elif filings:
        result.latest_fiscal_year = filings[0].fiscal_year
        result.revenue = filings[0].get("total_revenue")
        result.expenses = filings[0].get("total_expenses")

    _status_signals(result, status)
    _timeliness_signals(result, status, filings, latest_xml, today)
    _financial_signals(result, filings, latest_xml)
    _dependence_signals(result, filings, latest_xml)
    _leadership_signals(result, xml_years)
    _reserve(result, filings, latest_xml)
    _exposure_signals(result)
    return result


def _reserve(result: OrgResult, filings, latest_xml) -> None:
    """Months of spendable reserve, preferring unrestricted net assets.

    Not computed for private foundations: an endowed foundation holds decades
    of expenses by design, so "217 months of reserve" next to a public charity's
    2.4 is a category error rather than a comparison.
    """
    is_foundation = (
        (latest_xml and latest_xml.form_type == "990-PF")
        or (filings and filings[0].is_private_foundation)
    )
    result.is_foundation = bool(is_foundation)
    if is_foundation:
        return

    net = spendable = expenses = None
    if latest_xml and latest_xml.financials.has_financials:
        money = latest_xml.financials
        net, spendable, expenses = (
            money.net_assets, money.unrestricted_net_assets, money.total_expenses
        )
    elif filings:
        filing = filings[0]
        net = filing.get("net_assets")
        spendable = filing.get("unrestricted_net_assets")
        expenses = filing.get("total_expenses")
    basis = spendable if spendable is not None else net
    if basis is not None and expenses:
        result.months_reserve = (basis / expenses) * 12


def _exposure_signals(result: OrgResult) -> None:
    """How much of this organization's budget is your money.

    Nothing public can compute this, because nothing public knows your grants.
    It is also the finding a program officer can act on immediately: a grantee
    who gets half their revenue from you is a different relationship, and a
    different risk, from one where you are five percent.
    """
    share = result.grant_share
    if share is None:
        return
    if share >= PORTFOLIO_THRESHOLDS["grant_share_dominant"]:
        result.signals.append(Signal(
            "exposure", WATCH, f"Your grant is {_pct(share)} of their revenue",
            f"{_money(result.award)} against total revenue of "
            f"{_money(result.revenue)} in FY{result.latest_fiscal_year}. You "
            f"are effectively their principal funder; withdrawal would be "
            f"existential rather than difficult.",
            result.latest_fiscal_year,
        ))
    elif share >= PORTFOLIO_THRESHOLDS["grant_share_high"]:
        result.signals.append(Signal(
            "exposure", CONTEXT, f"Your grant is {_pct(share)} of their revenue",
            f"{_money(result.award)} against {_money(result.revenue)}. A large "
            f"enough share that your renewal decision is material to them.",
            result.latest_fiscal_year,
        ))


def _status_signals(result: OrgResult, status) -> None:
    if status.state == irs_status.REVOKED:
        result.signals.append(Signal(
            "revoked", CRITICAL, "Tax-exempt status auto-revoked",
            f"{status.detail} Do not disburse until this is resolved.",
        ))
    elif status.state == irs_status.REINSTATED:
        result.signals.append(Signal(
            "revoked", WATCH, "Previously revoked, since reinstated", status.detail,
        ))
    elif status.state == irs_status.UNKNOWN:
        result.signals.append(Signal(
            "revoked", CONTEXT, "Exemption status not verified",
            "The IRS revocation index is not built, so good standing could "
            "not be confirmed for any organization in this list.",
        ))
    if status.state != irs_status.UNKNOWN and status.in_pub78 is False \
            and status.state != irs_status.REVOKED:
        result.signals.append(Signal(
            "revoked", WATCH, "Not listed in Pub. 78",
            "Not on the IRS list of organizations eligible to receive "
            "tax-deductible contributions. Confirm why.",
        ))


def _timeliness_signals(result: OrgResult, status, filings, latest_xml, today) -> None:
    # Lateness is about whether a return was FILED, not whether the IRS has
    # published its numbers yet. Those are 12-24 months apart.
    year = result.latest_filing_year
    if year is None:
        if status.epostcard_years:
            result.signals.append(Signal(
                "late", CONTEXT, "Files the 990-N postcard",
                "Under the $50,000 threshold, so no financial detail is "
                "published. Expected for a small grantee, not a finding.",
            ))
        else:
            result.signals.append(Signal(
                "late", WATCH, "No filing on record",
                "No Form 990, 990-EZ, 990-PF or e-Postcard was found. May be "
                "newly formed, a church, or fiscally sponsored under another EIN.",
            ))
        return

    lag = today.year - year + (today.month - 12) / 12.0
    if lag >= PORTFOLIO_THRESHOLDS["late_filing_years"]:
        result.signals.append(Signal(
            "late", WATCH, f"No return filed since FY{year}",
            f"Roughly {lag:.0f} years behind. Three consecutive missed years "
            f"triggers automatic revocation.",
            year,
        ))

    # A filed return whose financials are not published yet is normal, and
    # must not be confused with either lateness or missing data.
    published = result.latest_fiscal_year
    if published and year and year > published:
        result.signals.append(Signal(
            "late", CONTEXT, f"FY{year} return filed, figures not yet published",
            f"The organization has filed through FY{year}, but extracted "
            f"financials are only available through FY{published}. Figures "
            f"here are FY{published}. This is the normal IRS processing lag, "
            f"not a filing problem.",
            year,
        ))


def _financial_signals(result: OrgResult, filings, latest_xml) -> None:
    year = result.latest_fiscal_year

    # A single 990 XML carries the prior-year column, so one filing gives the
    # year-over-year change without needing two filings on record.
    revenue = prior_revenue = None
    if latest_xml and latest_xml.financials.has_financials:
        revenue = latest_xml.financials.total_revenue
        prior_revenue = latest_xml.financials.prior_revenue
    if prior_revenue is None and len(filings) > 1:
        revenue = revenue if revenue is not None else filings[0].get("total_revenue")
        prior_revenue = filings[1].get("total_revenue")

    if revenue is not None and prior_revenue:
        change = (revenue - prior_revenue) / abs(prior_revenue)
        if change <= -PORTFOLIO_THRESHOLDS["revenue_drop"]:
            result.signals.append(Signal(
                "revenue_drop", WATCH, f"Revenue down {_pct(abs(change))}",
                f"{_money(prior_revenue)} to {_money(revenue)} in FY{year}. "
                f"One-time gifts and multi-year pledges recognized up front "
                f"both produce this without indicating distress.",
                year,
            ))

    deficits = _count_deficits(filings, latest_xml)
    if deficits >= PORTFOLIO_THRESHOLDS["deficit_years"]:
        result.signals.append(Signal(
            "deficit", WATCH, f"Deficit {deficits} years running",
            f"Expenses exceeded revenue in each of the last {deficits} years "
            f"on record. Ask how the gap is being closed.",
            year,
        ))
    elif result.revenue is not None and result.expenses is not None \
            and result.expenses > result.revenue:
        result.signals.append(Signal(
            "deficit", CONTEXT, "Ran a deficit in the latest year",
            f"Expenses of {_money(result.expenses)} against revenue of "
            f"{_money(result.revenue)} in FY{year}.",
            year,
        ))


def _count_deficits(filings, latest_xml) -> int:
    pairs: list[tuple[float, float]] = []
    if latest_xml and latest_xml.financials.has_financials:
        money = latest_xml.financials
        if money.total_revenue is not None and money.total_expenses is not None:
            pairs.append((money.total_revenue, money.total_expenses))
        if money.prior_revenue is not None and money.prior_expenses is not None:
            pairs.append((money.prior_revenue, money.prior_expenses))
    if not pairs:
        for filing in filings:
            revenue, expenses = filing.get("total_revenue"), filing.get("total_expenses")
            if revenue is None or expenses is None:
                break
            pairs.append((revenue, expenses))

    count = 0
    for revenue, expenses in pairs:
        if expenses > revenue:
            count += 1
        else:
            break
    return count


def _dependence_signals(result: OrgResult, filings, latest_xml) -> None:
    """How concentrated is the money.

    Schedule B donor identities are not public, so the public support test is
    the available measure: it is precisely the calculation of how much support
    comes from the broad public rather than a handful of sources.
    """
    year = result.latest_fiscal_year
    ratio = None
    basis = None

    if latest_xml and latest_xml.financials.public_support_ratio is not None:
        ratio = latest_xml.financials.public_support_ratio
        basis = "Schedule A public support test"
    elif filings:
        support = filings[0].get("public_support_170")
        total = filings[0].get("total_support_170")
        if support is not None and total:
            ratio = support / total
            basis = "170(b)(1)(A)(vi) test"

    if ratio is not None and ratio < PORTFOLIO_THRESHOLDS["public_support_low"]:
        result.signals.append(Signal(
            "funder_dependence", WATCH, f"Public support {_pct(ratio)}",
            f"Below the one-third threshold on the {basis}. Support is "
            f"concentrated in a small number of sources, and sustained failure "
            f"can cost public charity status.",
            year,
        ))

    if filings:
        excess = filings[0].get("excess_over_2pct")
        total = filings[0].get("total_support_170")
        if excess is not None and total:
            share = excess / total
            if share > PORTFOLIO_THRESHOLDS["donor_concentration"]:
                result.signals.append(Signal(
                    "funder_dependence", WATCH,
                    f"{_pct(share)} of support from donors above the 2% threshold",
                    "Schedule B names are not public, but this is the size of "
                    "the concentration. Losing one funder would be material.",
                    year,
                ))


def _leadership_signals(result: OrgResult, xml_years) -> None:
    if len(xml_years) < 2:
        return
    change = xml990.leadership_change(xml_years[0], xml_years[1])
    current_year, prior_year = change["compared_years"]

    if change["top_role_changed"]:
        result.signals.append(Signal(
            "leadership", WATCH, "Highest-paid officer changed",
            f"{change['previous_top']} in FY{prior_year}, "
            f"{change['current_top']} in FY{current_year}. Often a planned "
            f"succession; worth knowing either way.",
            current_year,
        ))
    if change["turnover_rate"] >= PORTFOLIO_THRESHOLDS["leadership_turnover"]:
        departed = ", ".join(change["departed"][:4])
        result.signals.append(Signal(
            "leadership", WATCH,
            f"{change['departed_count']} of "
            f"{change['departed_count'] + len(xml_years[1].people)} listed people left",
            f"Between FY{prior_year} and FY{current_year}: {departed}"
            + (" and others." if change["departed_count"] > 4 else "."),
            current_year,
        ))


# ---------------------------------------------------------------------------
# the portfolio
# ---------------------------------------------------------------------------
def evaluate(eins: list[str], names: dict[str, str] | None = None,
             today: date | None = None, use_xml: bool = True,
             progress=None, awards: dict[str, float] | None = None) -> PortfolioReport:
    names = names or {}
    awards = awards or {}
    report = PortfolioReport(
        generated_at=(today or date.today()).strftime("%B %-d, %Y")
    )

    for index, ein in enumerate(eins):
        if progress:
            progress(index + 1, len(eins), ein)
        try:
            result = evaluate_one(ein, names.get(ein), today=today,
                                  use_xml=use_xml, award=awards.get(ein))
        except Exception as exc:  # noqa: BLE001 - one bad EIN must not stop a sweep
            result = OrgResult(ein=format_ein(ein), listed_as=names.get(ein),
                               award=awards.get(ein), error=str(exc))
        (report.failed if result.error else report.organizations).append(result)

    report.size_distribution = _size_distribution(report.organizations)
    if report.failed:
        report.notes.append(
            f"{len(report.failed)} organization(s) could not be evaluated and "
            f"are listed separately. They are not counted anywhere above."
        )
    if not irs_status.check("000000000").is_known:
        report.notes.append(
            "The IRS revocation index is not built, so no organization in this "
            "portfolio had its exemption status verified."
        )
    return report


def _size_distribution(organizations: list[OrgResult]) -> dict:
    """Where the portfolio sits against each sector's own population.

    Comparing a portfolio's median revenue to the national median says little,
    because sector mix drives it. Percentiles within each organization's own
    sector are comparable to each other.
    """
    bands = Counter()
    placed = 0
    percentiles: list[int] = []
    sectors = Counter()

    for org in organizations:
        if org.revenue is None:
            bands["unknown"] += 1
            continue
        bands[_band(org.revenue)] += 1
        if org.ntee:
            sectors[org.ntee[0].upper()] += 1
        try:
            peer = peers_mod.compare(
                {"ntee_code": org.ntee, "state": org.state}, org.revenue
            )
        except Exception:  # noqa: BLE001
            peer = None
        if peer and peer.usable:
            org.percentile = peer.percentile
            org.peer_population = peer.population
            percentiles.append(peer.percentile)
            placed += 1

    median_percentile = None
    if percentiles:
        percentiles.sort()
        middle = len(percentiles) // 2
        median_percentile = (
            percentiles[middle] if len(percentiles) % 2
            else round((percentiles[middle - 1] + percentiles[middle]) / 2)
        )

    return {
        "bands": dict(bands),
        "placed": placed,
        "median_percentile": median_percentile,
        "sectors": dict(sectors.most_common(6)),
        "benchmarked": bool(percentiles),
    }


BANDS = (
    (50_000, "under $50K"),
    (250_000, "$50K–250K"),
    (1_000_000, "$250K–1M"),
    (5_000_000, "$1M–5M"),
    (25_000_000, "$5M–25M"),
    (float("inf"), "over $25M"),
)


def _band(revenue: float) -> str:
    for ceiling, label in BANDS:
        if revenue < ceiling:
            return label
    return "over $25M"


# ---------------------------------------------------------------------------
# finding real examples
# ---------------------------------------------------------------------------
def sample_real(kind: str = "revoked", count: int = 5, state: str | None = None,
                since_year: int | None = None) -> list[tuple[str, str]]:
    """Pull genuine examples out of the local IRS indexes.

    A demo built on invented organizations proves nothing, and inventing
    alerts against real names is worse than proving nothing. The revocation
    list is the IRS's own published record, so an organization drawn from it
    is really on it -- no fabrication, and nothing asserted that the source
    does not already say publicly.

    kind: "revoked" | "reinstated" | "large"
    Returns [(ein, name)], ready to paste into a sweep.
    """
    import sqlite3

    if not irs_status.DB_PATH.exists():
        return []
    conn = sqlite3.connect(f"file:{irs_status.DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if kind == "reinstated":
            where, params = "reinstatement_date != ''", []
        else:
            where, params = "reinstatement_date = '' AND revocation_date != ''", []

        rows = conn.execute(
            f"SELECT ein, name, revocation_date FROM revocation WHERE {where} "  # noqa: S608
            f"LIMIT 4000",
            params,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    picked: list[tuple[str, str]] = []
    for row in rows:
        revoked_on = irs_status.parse_date(row["revocation_date"])
        if since_year and (not revoked_on or revoked_on.year < since_year):
            continue
        picked.append((row["ein"], (row["name"] or "").strip()))
        if len(picked) >= count * 4:
            break

    if state:
        # The revocation file carries a state column only in some releases, so
        # confirm against ProPublica rather than assuming.
        confirmed = []
        for ein, name in picked:
            try:
                org = fetch_organization(ein)["organization"]
            except Exception:  # noqa: BLE001
                continue
            if (org.get("state") or "").upper() == state.upper():
                confirmed.append((ein, org.get("name") or name))
            if len(confirmed) >= count:
                break
        return confirmed
    return picked[:count]


def _sample_cli() -> None:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(
        description="Find real organizations in the local IRS indexes, for "
                    "testing a sweep against genuine data."
    )
    parser.add_argument("--kind", default="revoked",
                        choices=["revoked", "reinstated"])
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--state", help="filter by state (slower: confirms via ProPublica)")
    parser.add_argument("--since", type=int, help="only revocations from this year on")
    args = parser.parse_args()

    found = sample_real(args.kind, args.count, args.state, args.since)
    if not found:
        print("No examples found. Is the IRS index built? "
              "python -m diligence.irs_status --build")
        raise SystemExit(1)

    print(f"# {len(found)} real {args.kind} organizations from the IRS list.")
    print("# Paste into the sweep. Add award amounts to see exposure.")
    for ein, name in found:
        print(f"{ein[:2]}-{ein[2:]}, {name}")


if __name__ == "__main__":  # pragma: no cover
    _sample_cli()
