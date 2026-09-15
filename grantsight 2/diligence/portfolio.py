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
from .propublica import NotFound, fetch_organization, format_ein, search as search_orgs

# Portfolio thresholds, separate from the single-brief ones so a funder can
# tune "flag it in the quarterly sweep" differently from "flag it in a brief".
PORTFOLIO_THRESHOLDS = {
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
    revenue: float | None = None
    expenses: float | None = None
    source: str = "extract"
    signals: list[Signal] = field(default_factory=list)
    percentile: int | None = None
    peer_population: int | None = None
    error: str | None = None

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
                 today: date | None = None, use_xml: bool = True) -> OrgResult:
    today = today or date.today()
    result = OrgResult(ein=format_ein(ein), listed_as=listed_as)

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

    # XML first: the SOI extract runs 12-24 months behind, so an extract-only
    # portfolio sweep can miss a whole year of trouble.
    xml_years: list = []
    if use_xml:
        try:
            xml_years = xml990.load_years(ein, limit=3)
        except Exception:  # noqa: BLE001 - XML is an enhancement, not a gate
            xml_years = []

    latest_xml = xml_years[0] if xml_years else None
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
    return result


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
    year = result.latest_fiscal_year
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
            "late", WATCH, f"No filing since FY{year}",
            f"Roughly {lag:.0f} years behind. Three consecutive missed years "
            f"triggers automatic revocation. Note that IRS processing itself "
            f"lags, so a recent filing may not be published yet.",
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
             progress=None) -> PortfolioReport:
    names = names or {}
    report = PortfolioReport(
        generated_at=(today or date.today()).strftime("%B %-d, %Y")
    )

    for index, ein in enumerate(eins):
        if progress:
            progress(index + 1, len(eins), ein)
        try:
            result = evaluate_one(ein, names.get(ein), today=today, use_xml=use_xml)
        except Exception as exc:  # noqa: BLE001 - one bad EIN must not stop a sweep
            result = OrgResult(ein=format_ein(ein), listed_as=names.get(ein),
                               error=str(exc))
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
