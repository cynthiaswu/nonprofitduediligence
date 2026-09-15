"""Normalize IRS SOI extract fields across Form 990 / 990-EZ / 990-PF.

Every mapping below is taken from the published IRS SOI extract data
dictionaries (`{YY}eofinextractdoc.xls`), not guessed. The three forms use
genuinely different element names for the same concept -- total revenue is
`totrevenue` on the 990, `totrevnue` on the 990-EZ, and `totrcptperbks` on the
990-PF -- which is why a per-form map exists at all.

ProPublica aliases five fields across forms (totrevenue, totfuncexpns,
totassetsend, totliabend, pct_compnsatncurrofcr). Everything else arrives under
the raw element name for whichever form was filed.

THE SINGLE MOST IMPORTANT THING IN THIS FILE
--------------------------------------------
The functional expense split does not exist in this data. Every Part IX line in
the extract is column (A), the total column; columns (B) program, (C)
management, and (D) fundraising are never extracted. There is no
`progsrvcexpns` element. The program expense ratio therefore cannot be computed
from the SOI extract at any level of effort -- it requires the Form 990 XML or
PDF. `NOT_IN_EXTRACT` below records this so the brief can say "not published
here" rather than "did not resolve", which wrongly implies a mapping problem.

Resolution rules:
  * form-specific candidates are tried first, then cross-form fallbacks
  * a candidate may be a tuple, meaning "sum these elements"
  * an unresolved metric is None, never 0.0, and never estimated
  * the element that matched is recorded in `provenance`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORM_TYPES = {0: "990", 1: "990-EZ", 2: "990-PF"}
ANY = "*"

# metric -> {form type: [candidate element names]}. A tuple sums its members.
FIELD_MAP: dict[str, dict[str, list]] = {
    "total_revenue": {
        "990": ["totrevenue"],
        "990-EZ": ["totrevnue"],
        "990-PF": ["totrcptperbks"],
        ANY: ["totrevenue", "totrevnue", "totrcptperbks"],
    },
    "total_expenses": {
        "990": ["totfuncexpns"],
        "990-EZ": ["totexpns"],
        "990-PF": ["totexpnspbks", "totexpnsexempt"],
        ANY: ["totfuncexpns", "totexpns", "totexpnspbks"],
    },
    "total_assets": {
        "990": ["totassetsend"],
        "990-EZ": ["totassetsend"],
        "990-PF": ["totassetsend", "fairmrktvaleoy"],
        ANY: ["totassetsend"],
    },
    "total_liabilities": {ANY: ["totliabend"]},
    "net_assets": {
        "990": ["totnetassetend"],
        "990-EZ": ["totnetassetsend", "networthend"],
        "990-PF": ["tfundnworth"],
        ANY: ["totnetassetend", "totnetassetsend", "tfundnworth"],
    },
    # Part X lines 27-29. The IRS never updated the form for ASU 2016-14, so
    # post-2018 filers report "net assets with donor restrictions" on line 28
    # or line 29 depending on which guidance they followed. Both are captured
    # and summed downstream, which is correct either way.
    "unrestricted_net_assets": {"990": ["unrstrctnetasstsend"]},
    "temp_restricted_net_assets": {"990": ["temprstrctnetasstsend"]},
    "perm_restricted_net_assets": {"990": ["permrstrctnetasstsend"]},
    "contributions": {
        "990": ["totcntrbgfts"],
        "990-EZ": ["totcntrbs"],
        "990-PF": ["grscontrgifts"],
        ANY: ["totcntrbgfts", "totcntrbs", "grscontrgifts"],
    },
    "program_revenue": {
        "990": ["totprgmrevnue"],
        "990-EZ": ["prgmservrev"],
        ANY: ["totprgmrevnue", "prgmservrev"],
    },
    "investment_income": {
        "990": ["invstmntinc"],
        "990-EZ": ["othrinvstinc"],
        "990-PF": [("intrstrvnue", "dividndsamt")],
        ANY: ["invstmntinc", "othrinvstinc"],
    },
    "officer_comp": {
        "990": ["compnsatncurrofcr"],
        "990-PF": ["compofficers"],
        ANY: ["compnsatncurrofcr", "compofficers"],
    },
    "officer_comp_pct": {ANY: ["pct_compnsatncurrofcr"]},
    "reportable_comp": {"990": ["totreprtabled"]},
    "related_org_comp": {"990": ["totcomprelatede"]},
    "staff_over_100k": {"990": ["noindiv100kcnt"]},
    "contractors_over_100k": {"990": ["nocontractor100kcnt"]},
    "other_salaries": {"990": ["othrsalwages"]},
    "grants_paid": {
        # Part IX lines 1-3 are separate elements; the 990 has no single total.
        "990": [("grntstogovt", "grnsttoindiv", "grntstofrgngovt")],
        "990-PF": ["contrpdpbks"],
        ANY: ["contrpdpbks"],
    },
    "grants_approved_future": {"990-PF": ["grntapprvfut"]},
    "fundraising_fees": {"990": ["profndraising"]},
    "employee_count": {"990": ["noemplyeesw3cnt"]},
    # Schedule A public support test. This is the real answer to "is revenue
    # concentrated": the 170(b)(1)(A)(vi) test measures how much support comes
    # from the broad public rather than a handful of large donors.
    "public_support_170": {ANY: ["pubsupplesspct170"]},
    "total_support_170": {ANY: ["totsupp170", "totsupport170"]},
    "public_support_509": {ANY: ["pubsupplesub509", "pubsupplesssub509"]},
    "total_support_509": {ANY: ["totsupp509"]},
    # Contributions from any one donor in excess of 2% of total support. A
    # large value relative to support means a few donors dominate.
    "excess_over_2pct": {ANY: ["exceeds2pct170", "excds2pct170"]},
    "orgs_supported": {ANY: ["totnooforgscnt", "totnoforgscnt"]},
}

# Yes/no indicators. "Y" is meaningful; blank or "N" is not a finding.
FLAG_FIELDS: dict[str, dict[str, list[str]]] = {
    "terminated": {
        "990": ["ceaseoperationscd"],
        "990-EZ": ["contractioncd"],
        "990-PF": ["contractncd"],
        ANY: ["ceaseoperationscd", "contractioncd", "contractncd"],
    },
    "partial_liquidation": {"990": ["sellorexchcd"]},
    "excess_benefit": {"990": ["engageexcessbnftcd"], "990-EZ": ["s4958excessbenefcd"]},
    "prior_excess_benefit": {"990": ["awarexcessbnftcd"]},
    "loan_to_officer": {"990": ["loantofficercd"], "990-EZ": ["loanstoofficerscd"]},
    "schedule_j_required": {"990": ["rptyestocompnstncd"]},
    "audited_financials": {"990": ["sepindaudfinstmtcd"]},
    "filed_990t": {ANY: ["filedf990tcd"]},
}

# Reason for non-private-foundation status (Schedule A Part I). Changes what
# diligence is even appropriate: churches need not file at all, and supporting
# organizations carry different rules for the funder.
NONPF_REASON = {
    0: "not reported", 1: "church", 2: "school", 3: "hospital",
    4: "government unit", 5: "medical research organization",
    6: "supports a public college or university",
    7: "substantially government-supported", 8: "community trust",
    9: "509(a)(2) organization", 11: "tests for public safety",
    12: "Type I supporting organization", 13: "Type II supporting organization",
    14: "Type III supporting organization, functionally integrated",
    15: "Type III supporting organization, other",
}

# Things a reader might expect that this source genuinely does not carry.
# Used to word the gaps section precisely.
NOT_IN_EXTRACT = {
    "program_expenses": (
        "The program / management / fundraising split. Every Part IX line in "
        "the SOI extract is column (A), the total column. Columns B, C and D "
        "are not extracted, so the program expense ratio is unavailable from "
        "this source at any level of effort. It is in the Form 990 XML and PDF."
    ),
    "officer_names": (
        "Named officers and their individual compensation. The extract carries "
        "aggregates only. Per-person figures are in Part VII and Schedule J of "
        "the filing itself."
    ),
    "grant_recipients": (
        "Which organizations received grants. Schedule I recipient detail is "
        "in the Form 990 XML, not the extract."
    ),
    "donor_detail": (
        "Individual donor identities and amounts. Schedule B is not public. "
        "Where the filing reports it, the Schedule A public support test "
        "is the closest available proxy."
    ),
}

_STRUCTURAL = {"ein", "EIN", "tax_prd", "tax_prd_yr", "tax_pd", "formtype",
               "pdf_url", "updated", "organization", "elf", "eostatus", "taxpd",
               "a_tax_prd", "tax_yr", "subseccd", "subcd",
               # handled outside FIELD_MAP but not unmapped
               "nonpfrea", "operatingcd", "schedbind", "schdbind"}


@dataclass
class Filing:
    """One fiscal year, normalized. Unresolved metrics stay None."""

    ein: str
    fiscal_year: int | None
    tax_period: int | None
    form_type: str
    pdf_url: str | None
    updated: str | None
    values: dict[str, float | None] = field(default_factory=dict)
    flags: dict[str, bool | None] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    nonpf_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, metric: str) -> float | None:
        return self.values.get(metric)

    def flag(self, name: str) -> bool | None:
        return self.flags.get(name)

    @property
    def is_private_foundation(self) -> bool:
        return self.form_type == "990-PF"

    def source_of(self, metric: str) -> str | None:
        return self.provenance.get(metric)


def _coerce(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _candidates(spec: dict[str, list], form_type: str) -> list:
    """Form-specific candidates first, then any cross-form fallbacks."""
    ordered = list(spec.get(form_type, []))
    for candidate in spec.get(ANY, []):
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _resolve(raw: dict[str, Any], candidate) -> tuple[float | None, str | None]:
    """A candidate is an element name, or a tuple meaning 'sum these'."""
    if isinstance(candidate, tuple):
        total, used = 0.0, []
        for key in candidate:
            value = _coerce(raw.get(key))
            if value is not None:
                total += value
                used.append(key)
        return (total, " + ".join(used)) if used else (None, None)
    value = _coerce(raw.get(candidate))
    return (value, candidate) if value is not None else (None, None)


def _yes(raw: dict[str, Any], key: str) -> bool | None:
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in ("Y", "YES", "1", "TRUE"):
        return True
    if text in ("N", "NO", "0", "FALSE"):
        return False
    return None


def normalize_filing(raw: dict[str, Any], ein: str) -> Filing:
    """Map one raw ProPublica filing object onto canonical metric names."""
    form_type = FORM_TYPES.get(raw.get("formtype"), "unknown")
    filing = Filing(
        ein=ein,
        fiscal_year=int(raw["tax_prd_yr"]) if raw.get("tax_prd_yr") else None,
        tax_period=int(raw["tax_prd"]) if raw.get("tax_prd") else None,
        form_type=form_type,
        pdf_url=raw.get("pdf_url"),
        updated=raw.get("updated"),
        raw=raw,
    )

    for metric, spec in FIELD_MAP.items():
        filing.values[metric] = None
        for candidate in _candidates(spec, form_type):
            value, used = _resolve(raw, candidate)
            if value is not None:
                filing.values[metric] = value
                filing.provenance[metric] = used
                break

    for name, spec in FLAG_FIELDS.items():
        filing.flags[name] = None
        for key in _candidates(spec, form_type):
            result = _yes(raw, key)
            if result is not None:
                filing.flags[name] = result
                filing.provenance[name] = key
                break

    reason = _coerce(raw.get("nonpfrea"))
    if reason is not None:
        filing.nonpf_reason = NONPF_REASON.get(int(reason))

    return filing


def normalize_filings(raw_filings: list[dict[str, Any]], ein: str) -> list[Filing]:
    """Newest fiscal year first. Filings without a year are dropped."""
    normalized = [normalize_filing(item, ein) for item in raw_filings]
    dated = [f for f in normalized if f.fiscal_year is not None]
    return sorted(dated, key=lambda f: f.fiscal_year or 0, reverse=True)


def coverage_report(filings: list[Filing]) -> dict[str, bool]:
    """Which canonical metrics resolved at least once."""
    return {
        metric: any(f.get(metric) is not None for f in filings)
        for metric in FIELD_MAP
    }


def introspect(raw_filing: dict[str, Any]) -> dict[str, list[str]]:
    """Split a live filing's keys into mapped vs unmapped.

    Now a regression check rather than a mapping tool: with the dictionaries
    transcribed, a large 'unmapped' list on a real filing means either the IRS
    changed the extract or this filing carries fields worth adding.
    """
    mapped: set[str] = set()
    for spec in (*FIELD_MAP.values(), *FLAG_FIELDS.values()):
        for candidates in spec.values():
            for candidate in candidates:
                if isinstance(candidate, tuple):
                    mapped.update(candidate)
                else:
                    mapped.add(candidate)
    present = set(raw_filing) - _STRUCTURAL
    return {
        "matched": sorted(present & mapped),
        "unmapped": sorted(present - mapped),
    }


if __name__ == "__main__":  # pragma: no cover - developer utility
    import json
    import sys

    from .propublica import fetch_organization

    if len(sys.argv) < 2:
        print("usage: python -m diligence.normalize <EIN>")
        raise SystemExit(2)

    record = fetch_organization(sys.argv[1])
    filings = record.get("filings_with_data") or []
    if not filings:
        print("no filings with extracted data for this EIN")
        raise SystemExit(1)

    latest = filings[0]
    form = FORM_TYPES.get(latest.get("formtype"), "?")
    print(f"form {form}, FY {latest.get('tax_prd_yr')}, {len(latest)} raw keys\n")
    print(json.dumps(introspect(latest), indent=2))

    parsed = normalize_filing(latest, sys.argv[1])
    print("\nresolved:")
    for metric, value in sorted(parsed.values.items()):
        if value is not None:
            print(f"  {metric:24s} {value:>18,.0f}  <- {parsed.source_of(metric)}")
