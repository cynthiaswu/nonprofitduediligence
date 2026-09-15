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
        '<p class="hint">Checking a whole grantee list? '
        '<a href="/portfolio">Upload it for a portfolio sweep</a>.</p>',
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

    if not h.press and h.press_status != "not_configured":
        # Say what was actually done. "No coverage exists" and "the search was
        # never run" look identical otherwise.
        explanations = {
            "searched_no_match": (
                f"Searched {h.press_searched} result"
                f"{'s' if h.press_searched != 1 else ''}; none could be "
                f"confirmed as this organization rather than a similarly "
                f"named one."
            ),
            "searched_nothing_returned": (
                "Searched; the provider returned no articles. Most "
                "organizations have never been covered by the press, so this "
                "is the usual result and is not a finding."
            ),
            "error": "The press search did not complete.",
        }
        parts.append("<h2>Press mentions</h2>")
        parts.append(
            f'<p class="hint">{e(explanations.get(h.press_status, ""))}'
            + (f" Provider: {e(h.press_provider)}." if h.press_provider else "")
            + "</p>"
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
                + ("&#8224;" if item.provider_asserted else "")
                + (f'<div class="where">{meta}</div>' if meta else "")
                + "</li>"
            )
        parts.append("<h2>Press mentions</h2>")
        parts.append(f'<ul class="results">{"".join(rows)}</ul>')
        note = ('<p class="hint">Matched to this organization by name plus at '
                "least one of city, state, EIN, or the domain on its filing. ")
        if any(item.provider_asserted for item in h.press):
            note += ("Items marked with a dagger were corroborated by the news "
                     "provider's own full-text search rather than by text "
                     "returned here, which is weaker evidence. ")
        note += ("Headlines are linked, not summarized, and nothing here is "
                 "treated as a finding — confirm anything material against the "
                 "source.</p>")
        parts.append(note)
    del lead
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# portfolio
# ---------------------------------------------------------------------------
PORTFOLIO_CSS = """
.counts{display:flex;flex-wrap:wrap;gap:0 2.2rem;margin:1.6rem 0 0;
  font-family:var(--sans);font-size:.9rem}
.counts div{padding:.4rem 0}
.counts b{display:block;font-size:1.6rem;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.1}
.counts .critical b{color:var(--critical)}
.counts .watch b{color:var(--watch)}
.org{padding:1.1rem 0;border-bottom:1px solid var(--rule)}
.org:last-child{border-bottom:none}
.org h3{margin:0 0 .15rem;font-size:1.05rem;font-weight:600}
.org h3 a{color:var(--ink);text-decoration:none}
.org h3 a:hover{text-decoration:underline}
.org .where{font-family:var(--sans);font-size:.84rem;color:var(--muted);margin-bottom:.5rem}
.sig{padding:.3rem 0 .3rem .9rem;border-left:2px solid var(--rule);margin:.35rem 0;
  font-size:.94rem}
.sig.critical{border-color:var(--critical)}
.sig.watch{border-color:var(--watch)}
.sig b{font-weight:600}
.sig span{color:var(--muted);font-family:var(--sans);font-size:.86rem;display:block}
.bar{display:flex;height:.6rem;border-radius:2px;overflow:hidden;margin:.8rem 0 .4rem}
.bar i{display:block;background:var(--accent);opacity:.85}
.bar i:nth-child(even){opacity:.55}
.bandkey{font-family:var(--sans);font-size:.84rem;color:var(--muted);
  display:flex;flex-wrap:wrap;gap:.2rem 1.2rem}
"""


def portfolio_upload_page(error: str | None = None) -> str:
    body = f"""
    <h1 class="lede">Check a whole grantee list at once</h1>
    <p class="sub">Upload a CSV, Excel file or PDF containing your grantees.
    Any nine-digit EIN in it will be found, whatever the column layout.
    Nothing you upload is stored.</p>

    {f'<div class="notice bad">{error}</div>' if error else ""}

    <form method="post" action="/portfolio" enctype="multipart/form-data">
      <h2 style="margin-top:2rem">One organization</h2>
      <div class="search" style="margin-top:0">
        <input type="text" name="single" id="single"
               placeholder="Feeding America, or 36-3673599"
               aria-label="Organization name or EIN">
      </div>
      <p class="hint">A name or an EIN. For a name, an exact match is used;
      anything ambiguous comes back with the candidates rather than a guess.</p>

      <h2>Or a whole list</h2>
      <input type="file" name="upload" accept=".csv,.tsv,.txt,.xlsx,.xlsm,.pdf"
             style="font-family:var(--sans);font-size:.95rem">
      <p class="hint" style="margin:.9rem 0 .3rem">Or paste one per line —
      EINs, names, or a mix:</p>
      <textarea name="pasted" rows="5"
        placeholder="52-1693387&#10;Friends of the Urban Forest&#10;94-2699528"
        style="width:100%;padding:.6rem;font-family:var(--sans);font-size:.95rem;
               border:1px solid var(--ink);border-radius:2px"></textarea>
      <button class="go" type="submit" style="margin-top:1.1rem">Run the sweep</button>
    </form>

    <h2>What comes back</h2>
    <ul class="sources">
      <li>Anything auto-revoked, or revoked and since reinstated</li>
      <li>Organizations that have not filed recently enough</li>
      <li>Sharp revenue drops and multi-year deficits</li>
      <li>Funder concentration, measured by the Schedule A public support test</li>
      <li>Leadership departures and changes at the top, from Part VII</li>
      <li>How the portfolio's sizes sit against each organization's own sector</li>
    </ul>
    <p class="hint">Financials come from the Form 990 XML where available, which
    runs a year or more ahead of the IRS summary extract. A sweep takes a few
    seconds per organization.</p>
    {_footer()}"""
    return _page("Portfolio sweep — GrantSight", body).replace(
        "</style>", PORTFOLIO_CSS + "</style>"
    )


def _org_block(org) -> str:
    label = e(org.name or org.listed_as or org.ein)
    ein_digits = org.ein.replace("-", "")
    meta = [org.ein]
    if org.city or org.state:
        meta.append(", ".join(x for x in (org.city, org.state) if x))
    if org.latest_fiscal_year:
        meta.append(f"FY{org.latest_fiscal_year}"
                    + (" (from XML)" if org.source == "xml" else ""))
    if org.revenue is not None:
        meta.append(_money(org.revenue) + " revenue")
    if org.percentile is not None:
        meta.append(f"{org.percentile}th percentile in sector")

    signals = "".join(
        f'<div class="sig {e(s.severity)}"><b>{e(s.headline)}</b>'
        f"<span>{e(s.detail)}</span></div>"
        for s in org.signals
    )
    return (
        f'<div class="org"><h3><a href="/brief?ein={e(ein_digits)}">{label}</a></h3>'
        f'<div class="where">{e(" · ".join(meta))}</div>{signals}</div>'
    )


def _distribution_block(distribution: dict, total: int) -> str:
    bands = distribution.get("bands") or {}
    if not bands:
        return ""
    order = [label for _, label in
             __import__("diligence.portfolio", fromlist=["BANDS"]).BANDS] + ["unknown"]
    present = [(label, bands[label]) for label in order if bands.get(label)]
    widest = sum(count for _, count in present) or 1

    segments = "".join(
        f'<i style="width:{count / widest * 100:.1f}%" title="{e(label)}"></i>'
        for label, count in present
    )
    key = "".join(
        f"<span>{e(label)}: {count}</span>" for label, count in present
    )

    lines = ["<h2>Size distribution</h2>", f'<div class="bar">{segments}</div>',
             f'<div class="bandkey">{key}</div>']

    if distribution.get("benchmarked"):
        median = distribution.get("median_percentile")
        lines.append(
            f"<p>Against their own sectors, the median organization in this "
            f"portfolio sits at the <b>{median}th percentile</b> by revenue, "
            f"across the {distribution.get('placed', 0)} of {total} that could "
            f"be benchmarked. Comparing within each sector rather than against "
            f"a national median matters, because sector mix otherwise drives "
            f"the result.</p>"
        )
    else:
        lines.append(
            '<p class="hint">No sector benchmark: the peer index has not been '
            "built, so sizes are shown in bands only.</p>"
        )
    sectors = distribution.get("sectors") or {}
    if sectors:
        lines.append(
            '<p class="hint">Sectors represented: '
            + e(", ".join(f"{k} ({v})" for k, v in sectors.items())) + ".</p>"
        )
    return "\n".join(lines)


def _unresolved_block(unresolved) -> str:
    """Names that matched several organizations, or none.

    Shown with candidates so a person can pick, never resolved by guessing:
    the wrong organization in a portfolio report is worse than a gap in one.
    """
    if not unresolved:
        return ""
    rows = []
    for match in unresolved:
        candidates = ""
        if match.candidates:
            links = "".join(
                f'<li><a href="/brief?ein={e(str(c.get("ein", "")).zfill(9))}">'
                f'{e(c.get("name", "Unnamed"))}</a>'
                f'<div class="where">'
                f'{e(", ".join(x for x in ((c.get("city") or "").title(), c.get("state")) if x))}'
                f' &middot; EIN {e(str(c.get("ein", "")).zfill(9))}</div></li>'
                for c in match.candidates
            )
            candidates = f'<ul class="results" style="margin:.5rem 0 0">{links}</ul>'
        rows.append(
            f'<div class="org"><h3>{e(match.query)}</h3>'
            f'<div class="where">{e(match.reason)}</div>{candidates}</div>'
        )
    return (
        f"<h2>Names that could not be matched ({len(unresolved)})</h2>"
        '<p class="hint">These were not swept. Pick the right organization '
        "below, or supply the EIN.</p>" + "".join(rows)
    )


def portfolio_report_page(report, intake=None, unresolved=None) -> str:
    counts = report.summary_counts
    total = len(report.organizations)
    attention = report.attention

    labels = {
        "revoked": "status issues", "late": "filing late",
        "revenue_drop": "revenue drops", "deficit": "running deficits",
        "funder_dependence": "funder concentration", "leadership": "leadership change",
    }
    tiles = "".join(
        f'<div class="{"critical" if k == "revoked" and v else "watch" if v else ""}">'
        f"<b>{v}</b>{e(labels[k])}</div>"
        for k, v in counts.items()
    )

    parts = ["<h1>Portfolio sweep</h1>"]

    if total:
        parts.append(
            f'<p class="meta"><span>{total} organizations</span>'
            f"<span>{_money(report.total_revenue)} combined revenue</span>"
            f"<span>{e(report.generated_at)}</span></p>"
        )
        parts.append(
            f'<div class="counts"><div><b>{len(attention)}</b>need attention</div>'
            f"{tiles}</div>"
        )
        parts.append("<h2>Needs attention</h2>")
        if attention:
            parts.extend(_org_block(org) for org in attention)
        else:
            parts.append("<p>Nothing flagged in this list.</p>")
    else:
        # Nothing resolved. A row of zeroes would read as "all clear", which
        # is the opposite of what happened.
        parts.append(
            f'<p class="meta"><span>{e(report.generated_at)}</span></p>'
            "<p>No organization in this list could be identified, so nothing "
            "was checked. See below.</p>"
        )

    parts.append(_distribution_block(report.size_distribution, total))

    clear = report.clear
    if clear:
        parts.append(f"<h2>No signals ({len(clear)})</h2>")
        parts.append('<ul class="sources">' + "".join(
            f'<li><a href="/brief?ein={e(o.ein.replace("-", ""))}">'
            f'{e(o.name or o.listed_as or o.ein)}</a> — {e(o.ein)}'
            + (f", {_money(o.revenue)}" if o.revenue is not None else "")
            + "</li>" for o in clear
        ) + "</ul>")

    if report.failed:
        parts.append(f"<h2>Could not be evaluated ({len(report.failed)})</h2>")
        parts.append('<ul class="gaps">' + "".join(
            f"<li>{e(o.listed_as or o.ein)} ({e(o.ein)}) — {e(o.error)}</li>"
            for o in report.failed
        ) + "</ul>")

    parts.append(_unresolved_block(unresolved or []))

    if intake is not None and intake.unmatched:
        parts.append(f"<h2>Rows with no EIN ({len(intake.unmatched)})</h2>")
        parts.append(
            '<p class="hint">These were skipped. They are shown so a shorter '
            "portfolio than expected is visible rather than silent.</p>"
        )
        parts.append('<ul class="gaps">' + "".join(
            f"<li>Row {row.source_row}: {e(row.raw or row.name or '')}</li>"
            for row in intake.unmatched[:25]
        ) + "</ul>")

    if report.notes:
        # Warn only about things that affect the result; how many PDF pages
        # were read is a log line, not a caution.
        warnings = [n for n in report.notes
                    if any(w in n.lower() for w in
                           ("not built", "could not", "only the first", "stopped at"))]
        informational = [n for n in report.notes if n not in warnings]
        if warnings:
            parts.append('<div class="notice">'
                         + "<br>".join(e(n) for n in warnings) + "</div>")
        if informational:
            parts.append('<p class="hint">How this list was read: '
                         + e(" ".join(informational)) + "</p>")

    parts.append(
        '<p class="hint noprint" style="margin-top:2rem">'
        '<a href="/portfolio">Run another sweep</a> &nbsp; '
        '<a href="#" onclick="window.print();return false">Print or save as PDF</a></p>'
    )
    parts.append(_footer())
    return _page("Portfolio sweep — GrantSight", "\n".join(parts)).replace(
        "</style>", PORTFOLIO_CSS + "</style>"
    )
