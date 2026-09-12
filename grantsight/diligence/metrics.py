"""Derived metrics and flags -- the part ProPublica leaves to you.

Three rules run through this module:

  A ratio with a missing input is None, never zero and never estimated.
  A flag states the number it fired on and the fiscal year it came from.
  "Not published in this source" and "did not resolve" are different things
  and are worded differently, because the first is permanent and the second
  is a bug someone could fix.

Severity is deliberately coarse. CRITICAL means do not disburse until
resolved. WATCH means ask the organization about it. CONTEXT means a reader
should know it before interpreting the figures. Nothing here scores an
organization or recommends a decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .normalize import NOT_IN_EXTRACT, Filing

CRITICAL = "critical"
WATCH = "watch"
CONTEXT = "context"

# Thresholds are conventions, not standards. Every funder disagrees about
# them, so they live here in one place, named, for you to argue with.
THRESHOLDS = {
    "months_reserve_low": 3.0,
    "revenue_drop": 0.30,
    "officer_comp_share_high": 0.20,
    "stale_filing_years": 3,
    "consecutive_deficit_years": 3,
    "program_expense_ratio_low": 0.65,
    "public_support_low": 0.34,      # the 170(b)(1)(A)(vi) one-third test
    "donor_concentration_high": 0.25,
    "trend_years": 3,
}


@dataclass
class Flag:
    severity: str
    title: str
    detail: str
    fiscal_year: int | None = None


@dataclass
class Metrics:
    """`None` on any field means not determined from public data."""

    months_of_reserve: float | None = None
    months_of_unrestricted_reserve: float | None = None
    reserve_basis: str | None = None
    net_assets: float | None = None
    unrestricted_net_assets: float | None = None
    restricted_net_assets: float | None = None
    restricted_share: float | None = None
    net_margin: float | None = None
    revenue_change: float | None = None
    revenue_trend: str | None = None
    revenue_cagr: float | None = None
    trend_span: int | None = None
    officer_comp_share: float | None = None
    contribution_share: float | None = None
    public_support_ratio: float | None = None
    public_support_basis: str | None = None
    donor_concentration: float | None = None
    grants_paid: float | None = None
    grant_share_of_expenses: float | None = None
    staff_over_100k: float | None = None
    latest_fiscal_year: int | None = None
    filing_lag_years: float | None = None
    # Only ever populated from the Form 990 XML: the functional expense split
    # is absent from the SOI extract. See NOT_IN_EXTRACT.
    program_expense_ratio: float | None = None
    overhead_ratio: float | None = None
    highest_paid_comp: float | None = None
    people_listed: int | None = None
    grants_listed: int | None = None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def compute(filings: list[Filing], today: date | None = None, xml=None) -> Metrics:
    today = today or date.today()
    if not filings:
        return Metrics(
            program_expense_ratio=xml.expenses.ratio if xml else None,
            overhead_ratio=xml.expenses.overhead_ratio if xml else None,
        )

    latest = filings[0]
    prior = filings[1] if len(filings) > 1 else None

    revenue = latest.get("total_revenue")
    expenses = latest.get("total_expenses")
    assets = latest.get("total_assets")
    liabilities = latest.get("total_liabilities")

    net_assets = latest.get("net_assets")
    if net_assets is None and assets is not None and liabilities is not None:
        net_assets = assets - liabilities

    months = (net_assets / expenses) * 12 if net_assets is not None and expenses else None

    # Separate donor-restricted money from what the organization can actually
    # spend on operations. Part X line 27 is unrestricted; lines 28 and 29 are
    # donor-restricted and are summed, since which line a filer uses varies.
    unrestricted = latest.get("unrestricted_net_assets")
    restricted_parts = [
        v for v in (latest.get("temp_restricted_net_assets"),
                    latest.get("perm_restricted_net_assets"))
        if v is not None
    ]
    restricted = sum(restricted_parts) if restricted_parts else None

    unrestricted_months = None
    if unrestricted is not None and expenses:
        unrestricted_months = (unrestricted / expenses) * 12
    reserve_basis = (
        "net assets without donor restrictions" if unrestricted_months is not None
        else "total net assets" if months is not None
        else None
    )

    revenue_change = None
    if prior and prior.get("total_revenue") and revenue is not None:
        prior_revenue = prior.get("total_revenue")
        revenue_change = (revenue - prior_revenue) / abs(prior_revenue)

    officer_share = latest.get("officer_comp_pct")
    if officer_share is not None and officer_share > 1:
        officer_share = officer_share / 100.0
    if officer_share is None:
        officer_share = _ratio(latest.get("officer_comp"), expenses)

    # Public support test: the closest public proxy for revenue concentration.
    # Schedule B donor detail is not public, but the 170 and 509 tests measure
    # exactly how much support comes from the broad public versus a few sources.
    support_ratio = _ratio(latest.get("public_support_170"), latest.get("total_support_170"))
    basis = "170(b)(1)(A)(vi)" if support_ratio is not None else None
    if support_ratio is None:
        support_ratio = _ratio(latest.get("public_support_509"), latest.get("total_support_509"))
        basis = "509(a)(2)" if support_ratio is not None else None

    trend, cagr, span = _trend(filings)

    grants_paid = latest.get("grants_paid")

    return Metrics(
        program_expense_ratio=xml.expenses.ratio if xml else None,
        overhead_ratio=xml.expenses.overhead_ratio if xml else None,
        highest_paid_comp=(
            xml.highest_paid.total_comp if xml and xml.highest_paid else None
        ),
        people_listed=len(xml.people) if xml else None,
        grants_listed=len(xml.grants) if xml else None,
        months_of_reserve=months,
        months_of_unrestricted_reserve=unrestricted_months,
        reserve_basis=reserve_basis,
        net_assets=net_assets,
        unrestricted_net_assets=unrestricted,
        restricted_net_assets=restricted,
        restricted_share=_ratio(restricted, net_assets),
        net_margin=_ratio(
            (revenue - expenses) if revenue is not None and expenses is not None else None,
            revenue,
        ),
        revenue_change=revenue_change,
        revenue_trend=trend,
        revenue_cagr=cagr,
        trend_span=span,
        officer_comp_share=officer_share,
        contribution_share=_ratio(latest.get("contributions"), revenue),
        public_support_ratio=support_ratio,
        public_support_basis=basis,
        donor_concentration=_ratio(
            latest.get("excess_over_2pct"), latest.get("total_support_170")
        ),
        grants_paid=grants_paid,
        grant_share_of_expenses=_ratio(grants_paid, expenses),
        staff_over_100k=latest.get("staff_over_100k"),
        latest_fiscal_year=latest.fiscal_year,
        filing_lag_years=(
            today.year - latest.fiscal_year + (today.month - 12) / 12.0
            if latest.fiscal_year else None
        ),
    )


def _trend(filings: list[Filing]) -> tuple[str | None, float | None, int | None]:
    """Direction of revenue over the available window, with a growth rate.

    Uses first and last observed years rather than adjacent pairs, so a single
    unusual year does not flip the direction.
    """
    series = [
        (f.fiscal_year, f.get("total_revenue"))
        for f in reversed(filings)
        if f.fiscal_year and f.get("total_revenue") is not None
    ]
    if len(series) < 2:
        return None, None, None

    (first_year, first_value), (last_year, last_value) = series[0], series[-1]
    span = last_year - first_year
    if span <= 0 or first_value <= 0:
        return None, None, len(series)

    cagr = (last_value / first_value) ** (1 / span) - 1
    if cagr > 0.05:
        direction = "growing"
    elif cagr < -0.05:
        direction = "declining"
    else:
        direction = "flat"
    return direction, cagr, len(series)


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
    return "not determined" if value is None else f"{value * 100:.0f}%"


def _consecutive_deficits(filings: list[Filing]) -> int:
    count = 0
    for filing in filings:
        revenue, expenses = filing.get("total_revenue"), filing.get("total_expenses")
        if revenue is None or expenses is None:
            break
        if expenses > revenue:
            count += 1
        else:
            break
    return count


def evaluate(filings, m: Metrics, status, today: date | None = None) -> list[Flag]:
    today = today or date.today()
    flags: list[Flag] = []
    latest = filings[0] if filings else None
    fy = m.latest_fiscal_year

    def push(severity, title, detail, year=None):
        flags.append(Flag(severity, title, detail, year))

    # --- exemption status -------------------------------------------------
    if status.state == "revoked":
        push(CRITICAL, "Tax-exempt status auto-revoked",
             f"{status.detail} Grants to a revoked organization are not "
             f"deductible and may not count toward a foundation's qualifying "
             f"distributions. Confirm current status directly with the "
             f"organization before disbursing.")
    elif status.state == "reinstated":
        push(WATCH, "Previously auto-revoked, since reinstated",
             f"{status.detail} Worth asking what caused three consecutive "
             f"years of non-filing and what changed.")
    elif status.state == "unknown":
        push(CONTEXT, "Revocation status not checked",
             "The IRS status index is not built, so this brief cannot confirm "
             "the organization is in good standing.")

    if status.state != "unknown" and status.in_pub78 is False and status.state != "revoked":
        push(WATCH, "Not listed in Pub. 78",
             "The organization does not appear on the IRS list of entities "
             "eligible to receive tax-deductible contributions. Common for "
             "(c)(4)s, subordinates in a group ruling, and some religious "
             "organizations, but confirm the reason.")
    elif status.in_pub78 and status.deductibility_label and \
            status.deductibility_label != "public charity":
        push(CONTEXT, f"Pub. 78 classification: {status.deductibility_label}",
             "Deductibility limits and, for private foundations, expenditure "
             "responsibility rules differ from those for a public charity.")

    if getattr(status, "stale", False) and status.index_age_days:
        push(CONTEXT, "IRS status data is not current",
             f"The local copy of the IRS exemption files is "
             f"{status.index_age_days} days old. The IRS republishes them "
             f"monthly, so a revocation or reinstatement in the interim would "
             f"not appear here.")

    # --- data availability ------------------------------------------------
    if not filings:
        if status.epostcard_years:
            push(CONTEXT, "Files the 990-N postcard only",
                 f"Gross receipts are under the $50,000 threshold, so the "
                 f"organization files the e-Postcard and publishes no financial "
                 f"detail. On record for "
                 f"{', '.join(str(y) for y in sorted(status.epostcard_years, reverse=True)[:4])}. "
                 f"Exemption status was still verified against the IRS files. "
                 f"Request financial statements directly; this is normal for a "
                 f"small organization and is not a finding against it.")
        else:
            push(CONTEXT, "No published financial data",
                 "No Form 990, 990-EZ, 990-PF, or e-Postcard record was found. "
                 "The organization may be newly formed, a church or church "
                 "auxiliary (exempt from filing), fiscally sponsored under "
                 "another EIN, or inactive.")
        return _sorted(flags)

    # --- existence and continuity ----------------------------------------
    # The 990 asks directly whether the organization terminated or liquidated.
    # This is a far better liveness signal than a stale filing date, and it
    # carries no risk of matching the wrong organization by name.
    if latest.flag("terminated"):
        push(CRITICAL, "Reported termination or dissolution",
             f"On the FY{fy} filing the organization answered yes to ceasing "
             f"operations or dissolving. Confirm whether it still exists and "
             f"whether assets were transferred elsewhere before proceeding.", fy)
    if latest.flag("partial_liquidation"):
        push(WATCH, "Reported partial liquidation or asset sale",
             f"The FY{fy} filing reports a sale, exchange, or disposition of "
             f"more than 25% of net assets.", fy)

    if latest.nonpf_reason in ("church",):
        push(CONTEXT, "Classified as a church",
             "Churches are not required to file an annual return, so an "
             "absence of recent filings is expected and is not evidence of "
             "dormancy.", fy)
    elif latest.nonpf_reason and "supporting organization" in latest.nonpf_reason:
        push(CONTEXT, f"Classified as a {latest.nonpf_reason}",
             "Supporting organizations carry additional requirements for "
             "private foundation grantors, including expenditure "
             "responsibility for some types.", fy)

    if latest.is_private_foundation:
        push(CONTEXT, "Private foundation (Form 990-PF)",
             "990-PF reports on a different basis than the 990. Reserve months "
             "and public support ratios are not comparable to public charities "
             "and are omitted where they would mislead.", fy)

    if m.filing_lag_years and m.filing_lag_years >= THRESHOLDS["stale_filing_years"]:
        push(WATCH, "No recent filing on record",
             f"The most recent extracted filing covers FY{fy}, roughly "
             f"{m.filing_lag_years:.0f} years ago. Three consecutive missed "
             f"years triggers automatic revocation. Note that IRS extract "
             f"processing itself runs 12–24 months behind, so a recent filing "
             f"may simply not be published yet.", fy)

    # --- governance -------------------------------------------------------
    if latest.flag("excess_benefit") or latest.flag("prior_excess_benefit"):
        push(WATCH, "Excess benefit transaction reported",
             f"The FY{fy} filing reports a section 4958 excess benefit "
             f"transaction with a disqualified person, in the current or a "
             f"prior year. Ask what it was and how it was resolved.", fy)
    if latest.flag("loan_to_officer"):
        push(CONTEXT, "Loan outstanding to or from an officer",
             f"Reported on the FY{fy} filing. Common and often benign, but "
             f"worth understanding.", fy)

    # --- financial condition ----------------------------------------------
    if m.net_assets is not None and m.net_assets < 0:
        push(WATCH, "Negative net assets",
             f"Liabilities exceeded assets by {_money(abs(m.net_assets))} at "
             f"the close of FY{fy}.", fy)

    effective_months = (
        m.months_of_unrestricted_reserve
        if m.months_of_unrestricted_reserve is not None else m.months_of_reserve
    )
    if effective_months is not None and \
            effective_months < THRESHOLDS["months_reserve_low"] and not latest.is_private_foundation:
        if m.months_of_unrestricted_reserve is not None:
            detail = (
                f"Net assets without donor restrictions cover roughly "
                f"{m.months_of_unrestricted_reserve:.1f} months of FY{fy} "
                f"expenses"
            )
            if m.months_of_reserve is not None:
                detail += (
                    f", against {m.months_of_reserve:.1f} months on total net "
                    f"assets. The difference is money the organization cannot "
                    f"spend on general operations"
                )
            detail += "."
        else:
            detail = (
                f"Total net assets cover roughly {m.months_of_reserve:.1f} "
                f"months of FY{fy} expenses. This filing does not report the "
                f"Part X split, so the spendable figure may be lower."
            )
        push(WATCH, "Thin operating reserve", detail, fy)

    # A healthy-looking total reserve can be mostly restricted money.
    if m.restricted_share is not None and m.restricted_share > 0.5 and \
            m.months_of_reserve is not None and \
            m.months_of_reserve >= THRESHOLDS["months_reserve_low"] and \
            effective_months is not None and \
            effective_months < THRESHOLDS["months_reserve_low"]:
        push(WATCH, "Reserve is mostly donor-restricted",
             f"{_pct(m.restricted_share)} of net assets carry donor "
             f"restrictions, so the {m.months_of_reserve:.1f} months of total "
             f"reserve overstates what is available for operations: "
             f"{m.months_of_unrestricted_reserve:.1f} months are unrestricted.", fy)

    if m.revenue_change is not None and m.revenue_change <= -THRESHOLDS["revenue_drop"]:
        push(WATCH, "Sharp revenue decline",
             f"Total revenue fell {_pct(abs(m.revenue_change))} against the "
             f"prior year. One-time gifts and multi-year pledges recognized up "
             f"front both produce this pattern without indicating distress.", fy)

    if m.revenue_trend == "declining" and m.trend_span and m.trend_span >= THRESHOLDS["trend_years"]:
        push(WATCH, f"Revenue declining across {m.trend_span} years",
             f"Compound change of {_pct(m.revenue_cagr)} a year over the "
             f"filings on record, which is a sustained direction rather than a "
             f"single bad year.", fy)
    elif m.revenue_trend and m.trend_span and m.trend_span >= THRESHOLDS["trend_years"]:
        push(CONTEXT, f"Revenue {m.revenue_trend} across {m.trend_span} years",
             f"Compound change of {_pct(m.revenue_cagr)} a year over the "
             f"filings on record.", fy)

    deficits = _consecutive_deficits(filings)
    if deficits >= THRESHOLDS["consecutive_deficit_years"]:
        push(WATCH, f"Expenses exceeded revenue for {deficits} consecutive years",
             "Sustained deficits draw down net assets. Ask how the gap is "
             "being closed and whether it reflects planned spend-down.", fy)

    # --- revenue concentration -------------------------------------------
    if m.public_support_ratio is not None and \
            m.public_support_ratio < THRESHOLDS["public_support_low"]:
        push(WATCH, "Public support below the one-third test",
             f"Public support was {_pct(m.public_support_ratio)} of total "
             f"support on the {m.public_support_basis} test. Below one third, "
             f"support is concentrated in a small number of sources, and "
             f"sustained failure of the test can cost public charity status.", fy)

    if m.donor_concentration is not None and \
            m.donor_concentration > THRESHOLDS["donor_concentration_high"]:
        push(WATCH, "Revenue concentrated in a few large donors",
             f"Contributions exceeding 2% of total support from individual "
             f"donors amounted to {_pct(m.donor_concentration)} of all support. "
             f"Schedule B names are not public, but this is the size of the "
             f"concentration, and it means losing one funder would be material.", fy)

    # --- functional expenses, from the XML only ---------------------------
    if m.program_expense_ratio is not None and \
            m.program_expense_ratio < THRESHOLDS["program_expense_ratio_low"]:
        push(WATCH, "Program expense ratio below convention",
             f"{_pct(m.program_expense_ratio)} of FY{fy} expenses were "
             f"classified as program, with {_pct(m.overhead_ratio)} on "
             f"management and fundraising. The 65% convention is a weak "
             f"signal: the split is self-reported and varies with how an "
             f"organization allocates shared costs.", fy)

    # --- compensation -----------------------------------------------------
    if m.officer_comp_share is not None and \
            m.officer_comp_share > THRESHOLDS["officer_comp_share_high"]:
        push(WATCH, "Officer compensation is a large share of expenses",
             f"Compensation of current officers and directors was "
             f"{_pct(m.officer_comp_share)} of FY{fy} expenses. Expected in "
             f"very small organizations where the staff is the program.", fy)

    if m.staff_over_100k:
        push(CONTEXT, f"{int(m.staff_over_100k)} staff paid over $100,000",
             f"Reported on the FY{fy} filing. Individual names and amounts are "
             f"in Part VII and Schedule J of the filing itself, not in this "
             f"data.", fy)

    return _sorted(flags)


def _sorted(flags: list[Flag]) -> list[Flag]:
    rank = {CRITICAL: 0, WATCH: 1, CONTEXT: 2}
    return sorted(flags, key=lambda f: (rank[f.severity], -(f.fiscal_year or 0)))


def gaps(filings, m: Metrics, status, xml=None) -> list[str]:
    """What this brief could not determine, worded by reason.

    A permanent absence in the source and a failed lookup are different
    problems and are stated differently.
    """
    unknown: list[str] = []

    if status.state == "unknown":
        unknown.append(
            "Whether the organization is currently in good standing with the "
            "IRS. The revocation index is not built."
        )

    if filings:
        if m.program_expense_ratio is None:
            unknown.append(NOT_IN_EXTRACT["program_expenses"])
        if xml is None or not xml.people:
            unknown.append(NOT_IN_EXTRACT["officer_names"])
        if xml is not None and not xml.has_schedule_j and xml.people:
            unknown.append(
                "The compensation breakdown into base, bonus, deferred and "
                "benefits. Schedule J is only required above certain "
                "thresholds and this filing does not include it."
            )
        if m.public_support_ratio is None:
            unknown.append(NOT_IN_EXTRACT["donor_detail"])
        if (xml is None or not xml.has_schedule_i) and (
                filings[0].is_private_foundation or filings[0].get("grants_paid")):
            unknown.append(NOT_IN_EXTRACT["grant_recipients"])
        if m.months_of_reserve is None:
            unknown.append("Operating reserve, because net assets did not resolve.")
        elif m.months_of_unrestricted_reserve is None:
            unknown.append(
                "How much of the reserve is donor-restricted. Part X lines "
                "27–29 are in the extract for Form 990 filers, but this "
                "filing does not report them, so the reserve figure above is "
                "total net assets and may overstate what is spendable."
            )

    unknown.append(
        "Anything that happened after the filing date, including leadership "
        "change, litigation, or closure. The termination question on the "
        "filing itself is checked above, but it only covers the filed year."
    )
    if filings and m.filing_lag_years and m.filing_lag_years >= 1.5:
        unknown.append(
            "Current financial condition. The most recent published figures "
            f"are from FY{m.latest_fiscal_year}."
        )
    return unknown
