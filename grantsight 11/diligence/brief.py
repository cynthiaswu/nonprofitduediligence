"""Assemble a diligence brief from every source, degrading source by source."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from . import irs_status, metrics as metrics_mod, peers
from .http import FetchError
from .normalize import Filing, coverage_report, normalize_filings
from .propublica import (
    ATTRIBUTION,
    NotFound,
    fetch_organization,
    filing_years,
    format_ein,
    ntee_label,
)

SUBSECTION_LABELS = {
    3: "501(c)(3)", 4: "501(c)(4)", 5: "501(c)(5)", 6: "501(c)(6)",
    7: "501(c)(7)", 8: "501(c)(8)", 9: "501(c)(9)", 10: "501(c)(10)",
    19: "501(c)(19)", 92: "4947(a)(1) trust",
}


@dataclass
class Brief:
    ein: str
    name: str
    generated_at: str
    subtitle: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    subsection: str | None = None
    ntee: str | None = None
    status: Any = None
    filings: list[Filing] = field(default_factory=list)
    pdf_filings: list[dict] = field(default_factory=list)
    metrics: Any = None
    flags: list[Any] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    peer: Any = None
    coverage: dict[str, bool] = field(default_factory=dict)
    # The most recent return ON FILE. Distinct from metrics.latest_fiscal_year,
    # which is the most recent year with PUBLISHED FIGURES. Conflating them
    # reports a current filer as years delinquent.
    latest_filing_year: int | None = None
    filing_years: dict = field(default_factory=dict)
    source: str = "propublica"
    xml: Any = None
    highlights: Any = None
    narrative: str | None = None
    errors: list[str] = field(default_factory=list)

    # ---- headline ------------------------------------------------------
    @property
    def verdict(self) -> str:
        """One sentence a program officer can read and stop."""
        if self.status and self.status.state == irs_status.REVOKED:
            return "Tax-exempt status has been automatically revoked."
        if not self.filings:
            head = (
                "Exempt status current"
                if self.status and self.status.state == irs_status.CLEAR
                else "Exempt status previously revoked, since reinstated"
                if self.status and self.status.state == irs_status.REINSTATED
                else None
            )
            if head and self.status.epostcard_years:
                return (f"{head}; files the 990-N postcard, so no financial "
                        f"detail is published.")
            if head:
                return f"{head}. No financial data is published for this organization."
            return "No published financial data. Status could not be established."

        figures_year = self.metrics.latest_fiscal_year
        filed_year = self.latest_filing_year or figures_year
        lag = self.metrics.filing_lag_years or 0
        status_clause = (
            "Exempt status current"
            if self.status and self.status.state == irs_status.CLEAR
            else "Exempt status previously revoked, since reinstated"
            if self.status and self.status.state == irs_status.REINSTATED
            else "Exempt status not verified"
        )
        if filed_year and figures_year and filed_year > figures_year:
            # They have filed more recently than the published figures show.
            # Saying "last filing is FY2023" here is simply false.
            age_clause = (f"filed through FY{filed_year}, published figures "
                          f"are FY{figures_year}")
        elif lag >= 3:
            age_clause = f"no return on record since FY{filed_year}"
        elif lag >= 1.5:
            age_clause = f"financials are from FY{figures_year}"
        else:
            age_clause = f"financials current through FY{figures_year}"
        return f"{status_clause}; {age_clause}."

    @property
    def severity(self) -> str:
        for flag in self.flags:
            if flag.severity == metrics_mod.CRITICAL:
                return metrics_mod.CRITICAL
        for flag in self.flags:
            if flag.severity == metrics_mod.WATCH:
                return metrics_mod.WATCH
        return metrics_mod.CONTEXT

    @property
    def location(self) -> str:
        parts = [p for p in (self.city, self.state) if p]
        return ", ".join(parts)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["verdict"] = self.verdict
        payload["severity"] = self.severity
        payload["attribution"] = ATTRIBUTION
        return payload


def build(ein: str, include_peers: bool = False, include_narrative: bool = False,
          include_xml: bool = True, include_profile: bool = True,
          search=None) -> Brief:
    """Build a brief for one EIN. Any failing source degrades to a stated gap."""
    try:
        record = fetch_organization(ein)
    except NotFound:
        # ProPublica indexes Form 990 filers. Organizations under $50,000 in
        # receipts file the 990-N postcard instead and are absent entirely --
        # a large share of what a small funder looks at. The IRS files still
        # know them, so build from those rather than reporting "no record".
        return _build_from_irs_only(ein, include_narrative)

    org = record["organization"]
    ein_clean = str(org.get("ein", ein)).zfill(9)

    brief = Brief(
        ein=format_ein(ein_clean),
        name=org.get("name") or "Unnamed organization",
        subtitle=(org.get("sub_name") or "").strip() or None,
        address=org.get("address"),
        city=(org.get("city") or "").title() or None,
        state=org.get("state"),
        subsection=SUBSECTION_LABELS.get(org.get("subseccd"), f"501(c)({org.get('subseccd')})" if org.get("subseccd") else None),
        ntee=ntee_label(org.get("ntee_code")),
        generated_at=datetime.now().strftime("%B %-d, %Y"),
    )
    if brief.subtitle and brief.subtitle.upper() == brief.name.upper():
        brief.subtitle = None

    brief.filings = normalize_filings(record.get("filings_with_data") or [], ein_clean)
    brief.pdf_filings = sorted(
        record.get("filings_without_data") or [],
        key=lambda f: f.get("tax_prd_yr") or 0,
        reverse=True,
    )[:6]
    brief.coverage = coverage_report(brief.filings)

    try:
        brief.status = irs_status.check(ein_clean)
    except Exception as exc:  # noqa: BLE001
        brief.status = irs_status.ExemptStatus()
        brief.errors.append(f"IRS status check unavailable: {exc}")

    if include_xml:
        try:
            from . import xml990
            brief.xml = xml990.load_for(ein_clean)
            if brief.xml is None and xml990.LAST_FETCH_ERROR:
                # Distinguish "no filing" from "the source refused to serve
                # it". The second leaves stale figures on screen and must not
                # look like the first.
                brief.errors.append(
                    f"Current-year figures unavailable: {xml990.LAST_FETCH_ERROR}"
                )
        except Exception as exc:  # noqa: BLE001
            brief.errors.append(f"Form 990 XML unavailable: {exc}")

    # Filing recency is resolved from ProPublica alone, then only ever raised
    # by the XML -- never lowered. The XML feed lagging or a source going dead
    # must not be able to make a known FY2025 filing disappear.
    years = filing_years(record)
    latest_filing_year = years["latest_filed"]
    if brief.xml and brief.xml.tax_year:
        latest_filing_year = max(
            [y for y in (latest_filing_year, brief.xml.tax_year) if y]
        )
    if include_xml:
        # The IRS e-file index is the authority on whether a return was filed.
        # It answers from the index alone, so a dead document source can no
        # longer hide a return that the IRS has already accepted.
        try:
            from . import xml990
            indexed = xml990.latest_indexed_year(ein_clean)
            if indexed:
                latest_filing_year = max(
                    [y for y in (latest_filing_year, indexed) if y]
                )
        except Exception:  # noqa: BLE001
            pass
    brief.filing_years = years

    brief.latest_filing_year = latest_filing_year
    brief.metrics = metrics_mod.compute(brief.filings, xml=brief.xml)
    brief.flags = metrics_mod.evaluate(
        brief.filings, brief.metrics, brief.status,
        latest_filing_year=latest_filing_year,
    )
    brief.gaps = metrics_mod.gaps(brief.filings, brief.metrics, brief.status, xml=brief.xml)

    if include_peers and brief.filings:
        try:
            brief.peer = peers.compare(org, brief.filings[0].get("total_revenue"))
        except FetchError as exc:
            brief.errors.append(f"Peer comparison unavailable: {exc}")

    if include_profile:
        from . import profile as profile_mod
        try:
            brief.highlights = profile_mod.build(brief, search=search)
        except Exception as exc:  # noqa: BLE001
            brief.errors.append(f"Summary unavailable: {exc}")

    if include_narrative:
        from .narrative import summarize

        try:
            brief.narrative = summarize(brief)
        except Exception as exc:  # noqa: BLE001
            brief.errors.append(f"Narrative unavailable: {exc}")

    return brief


def _build_from_irs_only(ein: str, include_narrative: bool = False) -> Brief:
    """A brief assembled without ProPublica, for 990-N filers and the newly
    registered. Financially thin by necessity, but the exemption-status
    question -- the one that actually gates a disbursement -- is still
    answered, and the thinness is labelled as expected rather than adverse."""
    ein_clean = format_ein(ein).replace("-", "")
    status = irs_status.check(ein_clean)
    name = irs_status.lookup_name(ein_clean)

    if not (name or status.epostcard_years or status.in_pub78
            or status.state in (irs_status.REVOKED, irs_status.REINSTATED)):
        # Nothing anywhere. Let the caller render a not-found page.
        raise NotFound(
            f"EIN {format_ein(ein_clean)} is not in ProPublica's index and does "
            f"not appear in the IRS exemption files either."
        )

    brief = Brief(
        ein=format_ein(ein_clean),
        name=name or f"Organization {format_ein(ein_clean)}",
        generated_at=datetime.now().strftime("%B %-d, %Y"),
        status=status,
        source="irs-only",
    )
    brief.metrics = metrics_mod.compute([])
    brief.flags = metrics_mod.evaluate([], brief.metrics, status)
    brief.gaps = metrics_mod.gaps([], brief.metrics, status)
    brief.gaps.insert(
        0,
        "Any financial figure. This organization has no Form 990 on record "
        "with ProPublica, so revenue, expenses, assets, and liabilities are "
        "all unavailable here. Request financial statements directly.",
    )
    if not name:
        brief.gaps.insert(
            1,
            "The organization's legal name, which appears in none of the "
            "indexed IRS files. Confirm the EIN is correct.",
        )

    from . import profile as profile_mod
    try:
        brief.highlights = profile_mod.build(brief, check_site=False)
    except Exception:  # noqa: BLE001
        brief.highlights = None

    if include_narrative:
        from .narrative import summarize
        try:
            brief.narrative = summarize(brief)
        except Exception as exc:  # noqa: BLE001
            brief.errors.append(f"Narrative unavailable: {exc}")
    return brief
