"""HTML output.

These briefs get printed and pasted into board memos, so the page is built as
a document: one column, a verdict sentence at the top set large, and the
"could not determine" section given the same weight as the findings rather
than being buried as a disclaimer.
"""

from __future__ import annotations

from html import escape as e

from .metrics import CRITICAL, WATCH, _money, _pct
from .propublica import ATTRIBUTION

CSS = """
:root {
  --paper:#FBFBF9; --ink:#16181D; --muted:#5C6068; --rule:#DEDDD5;
  --accent:#2F4B7C; --critical:#A32B24; --watch:#8A6A16; --clear:#2E6244;
  --serif: Charter, "Bitstream Charter", "Iowan Old Style", "Source Serif 4", Georgia, serif;
  --sans: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font-family:var(--serif); font-size:17px; line-height:1.62;
  -webkit-font-smoothing:antialiased;
}
.wrap { max-width:46rem; margin:0 auto; padding:3.5rem 1.5rem 6rem; }
a { color:var(--accent); text-underline-offset:2px; }
a:focus-visible, input:focus-visible, button:focus-visible {
  outline:2px solid var(--accent); outline-offset:2px;
}

.masthead {
  display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; padding-bottom:.6rem; border-bottom:1px solid var(--ink);
  font-family:var(--sans); font-size:.8rem; color:var(--muted);
}
.masthead b { color:var(--ink); font-weight:600; letter-spacing:.01em; }

h1 { font-size:1.5rem; line-height:1.25; margin:2rem 0 .2rem; font-weight:600;
     letter-spacing:-.005em; }
.sub { font-family:var(--sans); font-size:.9rem; color:var(--muted); margin:0; }
.meta {
  font-family:var(--sans); font-size:.88rem; color:var(--muted);
  margin:.9rem 0 0; display:flex; flex-wrap:wrap; gap:.4rem 1.4rem;
}

.verdict {
  margin:2.2rem 0 0; padding:1.1rem 0 1.1rem 1.2rem;
  border-left:4px solid var(--clear); font-size:1.35rem; line-height:1.38;
}
.verdict.critical { border-color:var(--critical); }
.verdict.watch { border-color:var(--watch); }
.verdict .why {
  display:block; margin-top:.55rem; font-size:.95rem;
  font-family:var(--sans); color:var(--muted);
}
.narrative { margin:1.6rem 0 0; }

h2 {
  font-family:var(--sans); font-size:.9rem; font-weight:600; letter-spacing:.01em;
  margin:3rem 0 .9rem; padding-bottom:.4rem; border-bottom:1px solid var(--rule);
}

.finding { margin:0 0 1.5rem; padding-left:1.1rem; border-left:2px solid var(--rule); }
.finding.critical { border-color:var(--critical); }
.finding.watch { border-color:var(--watch); }
.finding h3 { margin:0 0 .25rem; font-size:1.05rem; font-weight:600; }
.finding p { margin:0; font-size:.97rem; color:#33363D; }
.finding .tag {
  font-family:var(--sans); font-size:.74rem; font-weight:600;
  color:var(--muted); margin-left:.5rem;
}

table { width:100%; border-collapse:collapse; font-family:var(--sans); font-size:.92rem; }
th, td { padding:.5rem .6rem; text-align:right; border-bottom:1px solid var(--rule); }
th:first-child, td:first-child { text-align:left; padding-left:0; }
th { font-weight:600; color:var(--muted); font-size:.8rem; }
td { font-variant-numeric:tabular-nums; }
td.none { color:var(--muted); font-variant-numeric:normal; font-style:italic; }
tr:last-child td { border-bottom:none; }

.gaps { margin:0; padding:0; list-style:none; }
.gaps li {
  padding:.55rem 0 .55rem 1.4rem; border-bottom:1px solid var(--rule);
  font-size:.97rem; position:relative;
}
.gaps li::before {
  content:"?"; position:absolute; left:0; top:.55rem;
  font-family:var(--sans); font-size:.85rem; color:var(--muted);
}
.gaps li:last-child { border-bottom:none; }

footer {
  margin-top:4rem; padding-top:1.2rem; border-top:1px solid var(--rule);
  font-family:var(--sans); font-size:.8rem; color:var(--muted); line-height:1.55;
}
footer a { color:var(--muted); }

form.search { display:flex; gap:.5rem; margin:1.4rem 0 0; }
input[type=text] {
  flex:1; padding:.7rem .8rem; font-family:var(--sans); font-size:1rem;
  border:1px solid var(--ink); background:#fff; border-radius:2px; color:var(--ink);
}
button {
  padding:.7rem 1.2rem; font-family:var(--sans); font-size:1rem; font-weight:600;
  background:var(--ink); color:var(--paper); border:1px solid var(--ink);
  border-radius:2px; cursor:pointer;
}
.hint { font-family:var(--sans); font-size:.85rem; color:var(--muted); margin:.7rem 0 0; }
.results { list-style:none; padding:0; margin:2rem 0 0; }
.results li { padding:.8rem 0; border-bottom:1px solid var(--rule); }
.sources { list-style:none; padding:0; margin:0; font-family:var(--sans); font-size:.92rem; }
.sources li { padding:.3rem 0; }
.results .where { font-family:var(--sans); font-size:.85rem; color:var(--muted); }
.notice { padding:.9rem 1.1rem; border-left:3px solid var(--watch); margin:1.5rem 0; font-size:.95rem; }

@media print {
  body { background:#fff; font-size:11pt; }
  .wrap { max-width:none; padding:0; }
  form.search, .noprint { display:none; }
  .finding, .verdict { break-inside:avoid; }
}
@media (max-width:34rem) {
  .wrap { padding:2rem 1.1rem 4rem; }
  h1 { font-size:1.3rem; }
  .verdict { font-size:1.15rem; }
  form.search { flex-direction:column; }
}
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title>
<style>{CSS}</style>
</head><body><div class="wrap">
<div class="masthead"><b>GrantSight</b><span>Public-source due diligence</span></div>
{body}
</div></body></html>"""


def search_page(query: str = "", candidates=None, error: str | None = None) -> str:
    body = [
        "<h1>Check an organization before you fund it</h1>",
        '<p class="sub">Enter a name or EIN. Everything here comes from IRS '
        "filings and the IRS exemption files. Nothing you type is stored.</p>",
        f'<form class="search" method="get" action="/">'
        f'<input type="text" name="q" value="{e(query)}" '
        f'placeholder="Feeding America, or 36-3673599" aria-label="Organization name or EIN" autofocus>'
        f"<button type=\"submit\">Look up</button></form>",
        '<p class="hint">Organizations with under $50,000 in receipts file a '
        "postcard instead of a 990, so their briefs will be thin. That is "
        "expected, not a red flag.</p>",
        "<h2>What a brief covers</h2>"
        '<ul class="sources">'
        "<li>Whether exemption is current, revoked, or reinstated, and whether "
        "contributions are deductible</li>"
        "<li>Five years of revenue, expenses, assets, and liabilities, with "
        "reserve months and year-over-year change</li>"
        "<li>Conditions worth asking about, each with the figure and fiscal "
        "year it fired on</li>"
        "<li>An explicit list of what the public record does not answer</li>"
        "</ul>",
    ]
    if error:
        body.append(f'<div class="notice">{e(error)}</div>')
    if candidates:
        body.append("<h2>Several organizations match</h2><ul class=\"results\">")
        for item in candidates:
            ein = str(item.get("ein", "")).zfill(9)
            where = ", ".join(p for p in (item.get("city", "").title(), item.get("state")) if p)
            body.append(
                f'<li><a href="/brief?ein={e(ein)}">{e(item.get("name", "Unnamed"))}</a>'
                f'<div class="where">{e(where)} &middot; EIN {e(ein[:2])}-{e(ein[2:])}</div></li>'
            )
        body.append("</ul>")
    body.append(_footer())
    return _page("GrantSight", "\n".join(body))


def _footer(source: str = "propublica") -> str:
    attribution = (
        ATTRIBUTION if source != "irs-only"
        else ("Data from the IRS Exempt Organizations files: the Auto-Revocation "
              "List, Publication 78 Data, and Form 990-N filings.")
    )
    return (
        f'<footer><p>{e(attribution)}</p>'
        "<p>Exemption status is checked against the IRS Auto-Revocation List "
        "and Publication 78 data. IRS financial extracts lag filings by 12 to "
        "24 months, so figures here are not current-year. This is background "
        "research, not legal, tax, or investment advice, and it is not a "
        "funding recommendation. Organization names appear exactly as "
        "registered with the IRS.</p></footer>"
    )


def brief_page(brief) -> str:
    m, s = brief.metrics, brief.status
    parts: list[str] = []

    parts.append(f"<h1>{e(brief.name)}</h1>")
    if brief.subtitle:
        parts.append(f'<p class="sub">{e(brief.subtitle)}</p>')

    meta = [f"EIN {brief.ein}"]
    if brief.location:
        meta.append(brief.location)
    if brief.subsection:
        meta.append(brief.subsection)
    if brief.ntee:
        meta.append(brief.ntee)
    parts.append(
        '<p class="meta">' + "".join(f"<span>{e(x)}</span>" for x in meta) + "</p>"
    )

    why = s.detail if s and s.detail else ""
    if s and s.index_built_at:
        age = f", {s.index_age_days} days ago" if s.index_age_days is not None else ""
        why += f" IRS exemption files as of {s.index_built_at[:10]}{age}."
    if s and s.deductibility_label:
        why += f" Pub. 78 classification: {s.deductibility_label}."
    parts.append(
        f'<div class="verdict {e(brief.severity)}">{e(brief.verdict)}'
        + (f'<span class="why">{e(why.strip())}</span>' if why.strip() else "")
        + "</div>"
    )

    if getattr(brief, "highlights", None):
        parts.append(_summary_section(brief.highlights))

    if brief.narrative:
        parts.append(f'<p class="narrative">{e(brief.narrative)}</p>')

    if brief.flags:
        parts.append("<h2>Findings</h2>")
        for flag in brief.flags:
            tag = {CRITICAL: "resolve before disbursing", WATCH: "ask about this"}.get(
                flag.severity, "context"
            )
            parts.append(
                f'<div class="finding {e(flag.severity)}"><h3>{e(flag.title)}'
                f'<span class="tag">{e(tag)}</span></h3>'
                f"<p>{e(flag.detail)}</p></div>"
            )

    if brief.filings:
        parts.append(_financials_table(brief))

    if brief.xml:
        parts.append(_people_section(brief))
        parts.append(_grants_section(brief))

    parts.append("<h2>What this brief could not determine</h2>")
    parts.append(
        '<ul class="gaps">'
        + "".join(f"<li>{e(item)}</li>" for item in brief.gaps)
        + "</ul>"
    )

    if brief.peer and brief.peer.usable:
        peer = brief.peer
        parts.append("<h2>Size relative to peers</h2>")
        parts.append(
            f"<p>{e(peer.describe())} Across that population the median is "
            f"{_money(peer.median_revenue)}, with the middle half falling "
            f"between {_money(peer.p25)} and {_money(peer.p75)}.</p>"
            f'<p class="hint">Compared on {e(peer.scope)}. Population built '
            f"from the IRS SOI extract joined to the Business Master File"
            + (f", {e(peer.built_at[:10])}." if peer.built_at else ".")
            + "</p>"
        )

    parts.append(_sources(brief))
    if brief.errors:
        parts.append(
            '<div class="notice noprint">'
            + "<br>".join(e(x) for x in brief.errors)
            + "</div>"
        )
    parts.append(
        '<p class="hint noprint" style="margin-top:2rem">'
        f'<a href="/">Look up another organization</a> &nbsp; '
        f'<a href="/api/brief.json?ein={e(brief.ein)}">JSON</a></p>'
    )
    parts.append(_footer(getattr(brief, "source", "propublica")))
    return _page(f"{brief.name} — GrantSight", "\n".join(parts))


def _cell(value, formatter) -> str:
    if value is None:
        return '<td class="none">n/d</td>'
    return f"<td>{formatter(value)}</td>"


def _financials_table(brief) -> str:
    rows = brief.filings[:5]
    header = "".join(f"<th>FY{f.fiscal_year}</th>" for f in rows)
    lines = [
        f'<h2>Reported financials</h2><table><thead><tr><th>&nbsp;</th>{header}</tr></thead><tbody>'
    ]

    def row(label: str, metric: str, formatter=_money) -> str:
        cells = "".join(_cell(f.get(metric), formatter) for f in rows)
        return f"<tr><td>{e(label)}</td>{cells}</tr>"

    lines.append(row("Total revenue", "total_revenue"))
    lines.append(row("Total expenses", "total_expenses"))
    lines.append(row("Total assets", "total_assets"))
    lines.append(row("Total liabilities", "total_liabilities"))
    lines.append(row("Contributions", "contributions"))
    lines.append(row("Program service revenue", "program_revenue"))
    lines.append(row("Net assets", "net_assets"))
    lines.append(row("  without donor restrictions", "unrestricted_net_assets"))
    lines.append(row("Grants paid", "grants_paid"))

    surplus = "".join(
        _cell(
            (f.get("total_revenue") - f.get("total_expenses"))
            if f.get("total_revenue") is not None and f.get("total_expenses") is not None
            else None,
            _money,
        )
        for f in rows
    )
    lines.append(f"<tr><td>Surplus / deficit</td>{surplus}</tr>")
    forms = "".join(f"<td>{e(f.form_type)}</td>" for f in rows)
    lines.append(f"<tr><td>Form filed</td>{forms}</tr>")
    lines.append("</tbody></table>")

    m = brief.metrics
    summary = []
    if m.program_expense_ratio is not None:
        summary.append(
            f"{_pct(m.program_expense_ratio)} program expense ratio "
            f"({_pct(m.overhead_ratio)} management and fundraising)"
        )
    if m.months_of_unrestricted_reserve is not None:
        summary.append(
            f"{m.months_of_unrestricted_reserve:.1f} months of unrestricted "
            f"reserve ({m.months_of_reserve:.1f} on total net assets)"
        )
    elif m.months_of_reserve is not None:
        summary.append(f"{m.months_of_reserve:.1f} months of reserve, total net assets")
    if m.contribution_share is not None:
        summary.append(f"{_pct(m.contribution_share)} of revenue from contributions")
    if m.public_support_ratio is not None:
        summary.append(
            f"{_pct(m.public_support_ratio)} public support on the "
            f"{m.public_support_basis} test"
        )
    if m.revenue_trend and m.trend_span:
        summary.append(
            f"revenue {m.revenue_trend} at {_pct(m.revenue_cagr)} a year across "
            f"{m.trend_span} filings"
        )
    if summary:
        lines.append(f'<p class="hint">Most recent year: {e("; ".join(summary))}.</p>')
    note = ('<p class="hint">"n/d" means the element is absent from that '
            "form's IRS extract. It is not a zero.")
    if m.program_expense_ratio is None:
        note += (" The program, management and fundraising expense split is "
                 "not published in this source at all — see below.")
    else:
        note += (" The program, management and fundraising split comes from "
                 "the Form 990 XML, not this extract.")
    lines.append(note + "</p>")
    return "\n".join(lines)


def _sources(brief) -> str:
    links = []
    ein_digits = brief.ein.replace("-", "")
    # Always link the IRS lookup: it is authoritative for current status and is
    # the only useful source for an organization ProPublica does not index.
    links.append(
        '<li><a href="https://apps.irs.gov/app/eos/">IRS Tax Exempt Organization '
        f"Search</a> — authoritative check of current status for EIN {e(brief.ein)}</li>"
    )
    if getattr(brief, "source", "propublica") != "irs-only":
        links.append(
            f'<li><a href="https://projects.propublica.org/nonprofits/organizations/{e(ein_digits)}">'
            "Full profile and filing history on ProPublica Nonprofit Explorer</a></li>"
        )
    for filing in brief.filings[:3]:
        if filing.pdf_url:
            links.append(
                f'<li><a href="{e(filing.pdf_url)}">FY{filing.fiscal_year} '
                f"Form {e(filing.form_type)} (PDF)</a></li>"
            )
    for filing in brief.pdf_filings[:2]:
        if filing.get("pdf_url"):
            links.append(
                f'<li><a href="{e(filing["pdf_url"])}">FY{filing.get("tax_prd_yr")} '
                "filing (PDF, no extracted data)</a></li>"
            )
    return "<h2>Read the source</h2><ul class=\"sources\">" + "".join(links) + "</ul>"


def _people_section(brief) -> str:
    """Part VII Section A, plus Schedule J detail where the filing carries it.

    Reported in organizational capacity only: name, title, hours, pay. The
    IRS requires this to be public; addresses that appear in some filings are
    never extracted.
    """
    xml = brief.xml
    if not xml or not xml.people:
        return ""

    rows = []
    for person in xml.people:
        comp = person.total_comp
        detail = []
        if person.hours:
            detail.append(f"{person.hours:g} hrs/wk")
        if person.roles:
            detail.append(", ".join(person.roles))
        if person.total_comp_schedule_j is not None:
            pieces = [
                (label, value) for label, value in (
                    ("base", person.base_comp), ("bonus", person.bonus_comp),
                    ("deferred", person.deferred_comp), ("benefits", person.benefits),
                ) if value
            ]
            if pieces:
                detail.append(
                    "Schedule J: " + ", ".join(f"{l} {_money(v)}" for l, v in pieces)
                )
        rows.append(
            f"<tr><td>{e(person.name)}"
            + (f'<div class="where">{e("; ".join(detail))}</div>' if detail else "")
            + f"</td><td>{e(person.title or '')}</td>"
            + (f"<td>{_money(comp)}</td>" if comp is not None
               else '<td class="nd">n/d</td>')
            + "</tr>"
        )

    note = (
        f'<p class="hint">From Part VII Section A of the FY{xml.tax_year} filing'
        + (", with Schedule J detail where reported" if xml.has_schedule_j else "")
        + ". Compensation is reportable pay from the organization plus related "
        "organizations plus other compensation.</p>"
    )
    return (
        "<h2>Officers, directors and key employees</h2>"
        '<table><thead><tr><th>Name</th><th>Title</th><th>Compensation</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{note}"
    )


def _grants_section(brief) -> str:
    """Schedule I Part II: who this organization funded."""
    xml = brief.xml
    if not xml or not xml.grants:
        return ""

    rows = []
    for grant in xml.grants:
        label = e(grant.recipient)
        if grant.ein:
            link = f"/brief?ein={e(grant.ein)}"
            label = f'<a href="{link}">{label}</a>'
        detail = " &middot; ".join(
            e(x) for x in (grant.purpose, grant.irc_section) if x
        )
        rows.append(
            f"<tr><td>{label}"
            + (f'<div class="where">{detail}</div>' if detail else "")
            + "</td>"
            + (f"<td>{_money(grant.total)}</td>" if grant.total is not None
               else '<td class="nd">n/d</td>')
            + "</tr>"
        )

    total = sum(g.total for g in xml.grants if g.total)
    note = (
        f'<p class="hint">From Schedule I of the FY{xml.tax_year} filing; '
        f"{len(xml.grants)} recipients totalling {_money(total)}"
        + (", largest first, list truncated" if xml.grants_truncated else ", largest first")
        + ". Recipient names link through to their own brief where an EIN was "
        "reported.</p>"
    )
    return (
        "<h2>Grants made</h2>"
        '<table><thead><tr><th>Recipient</th><th>Amount</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{note}"
    )


def _summary_section(h) -> str:
    """What the organization is, above the findings.

    Everything except the press block is derived from the filings, so it needs
    no hedging. The press block is separated and labelled precisely because it
    is the one part that came from outside the filings.
    """
    if not h.has_content:
        return ""

    parts = ["<h2>Summary</h2>"]
    lead = h.headline
    if h.mission:
        lead += f" {e(h.mission)}"
    parts.append(f"<p>{e(h.headline)}{' ' + e(h.mission) if h.mission else ''}</p>")

    if h.facts:
        parts.append(
            '<ul class="sources">'
            + "".join(f"<li>{e(fact)}</li>" for fact in h.facts)
            + "</ul>"
        )

    if h.website:
        status = ""
        if h.website_status and h.website_status != "reachable":
            status = f" — {e(h.website_status)} when checked"
        parts.append(
            f'<p class="hint"><a href="{e(h.website)}" target="_blank" '
            f'rel="noopener">{e(h.website)}</a>{status}, as reported on the '
            f"organization's own filing.</p>"
        )

    if h.press:
        rows = []
        for item in h.press:
            meta = " &middot; ".join(
                e(x) for x in (item.source, item.published) if x
            )
            rows.append(
                f'<li><a href="{e(item.url)}" target="_blank" rel="noopener">'
                f"{e(item.title)}</a>"
                + (f'<div class="where">{meta}</div>' if meta else "")
                + "</li>"
            )
        parts.append("<h2>Press mentions</h2>")
        parts.append(f'<ul class="results">{"".join(rows)}</ul>')
        parts.append(
            '<p class="hint">Matched to this organization by name plus at '
            "least one of city, state, EIN, or the domain on its filing. "
            "Headlines are linked, not summarized, and nothing here is treated "
            "as a finding — confirm anything material against the source.</p>"
        )
    del lead
    return "\n".join(parts)
