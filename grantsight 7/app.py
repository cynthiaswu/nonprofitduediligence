"""GrantSight web app.

Three routes and no database of user activity:

    GET /                      search by name or EIN
    GET /brief?ein=            the diligence brief, as HTML
    GET /api/brief.json?ein=   the same brief as JSON

Nothing a user types is persisted. The only things on disk are an HTTP
response cache and the IRS reference index, both of which are public data.

    uvicorn app:app --reload
"""

from __future__ import annotations

import os

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from diligence import (
    brief as brief_mod,
    intake as intake_mod,
    irs_status,
    portfolio as portfolio_mod,
    render,
)
from diligence.http import FetchError
from diligence.propublica import NotFound, looks_like_ein, resolve

app = FastAPI(title="GrantSight", docs_url="/api/docs")

PEERS_DEFAULT = os.environ.get("GRANTSIGHT_PEERS", "1") == "1"
NARRATIVE_DEFAULT = os.environ.get("GRANTSIGHT_NARRATIVE", "1") == "1"


@app.get("/", response_class=HTMLResponse)
def home():
    """The portfolio is the product; a single organization is the drill-down.

    An earlier version made single-organization lookup the front door, which
    made this a worse copy of ProPublica's own site. Watching a grantee list
    over time is the thing no public database does.
    """
    return HTMLResponse(render.portfolio_upload_page())


@app.get("/lookup", response_class=HTMLResponse)
def lookup(q: str = Query("", description="Organization name or EIN")):
    query = q.strip()
    if not query:
        return HTMLResponse(render.search_page())
    if looks_like_ein(query):
        return RedirectResponse(f"/brief?ein={query}", status_code=303)

    try:
        record, candidates = resolve(query)
    except FetchError as exc:
        return HTMLResponse(
            render.search_page(query, error=f"Could not reach ProPublica: {exc}"),
            status_code=502,
        )

    if record:
        ein = str(record["organization"]["ein"]).zfill(9)
        return RedirectResponse(f"/brief?ein={ein}", status_code=303)
    if candidates:
        return HTMLResponse(render.search_page(query, candidates=candidates))
    return HTMLResponse(
        render.search_page(
            query,
            error=(
                f"No organization matching \"{query}\" is in ProPublica's index. "
                "Organizations that only file the 990-N postcard are not "
                "indexed there; try the EIN directly, or search the IRS Tax "
                "Exempt Organization Search tool."
            ),
        ),
        status_code=404,
    )


def _build(ein: str, peers: bool, narrative: bool):
    return brief_mod.build(ein, include_peers=peers, include_narrative=narrative)


@app.get("/brief", response_class=HTMLResponse)
def brief_html(
    ein: str,
    peers: bool = Query(PEERS_DEFAULT),
    narrative: bool = Query(NARRATIVE_DEFAULT),
):
    try:
        result = _build(ein, peers, narrative)
    except ValueError as exc:
        return HTMLResponse(render.search_page(ein, error=str(exc)), status_code=400)
    except NotFound as exc:
        return HTMLResponse(
            render.search_page(
                ein,
                error=(
                    f"{exc} That usually means the organization files only the "
                    "990-N postcard, is newly registered, or is a church or "
                    "church auxiliary exempt from filing."
                ),
            ),
            status_code=404,
        )
    except FetchError as exc:
        return HTMLResponse(
            render.search_page(ein, error=f"Could not reach ProPublica: {exc}"),
            status_code=502,
        )
    return HTMLResponse(render.brief_page(result))


@app.get("/api/brief.json")
def brief_json(
    ein: str,
    peers: bool = Query(PEERS_DEFAULT),
    narrative: bool = Query(False),
):
    try:
        result = _build(ein, peers, narrative)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except NotFound as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except FetchError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(result.to_dict())


MAX_UPLOAD_BYTES = int(os.environ.get("GRANTSIGHT_MAX_UPLOAD", str(8 * 1024 * 1024)))
MAX_PORTFOLIO = int(os.environ.get("GRANTSIGHT_MAX_PORTFOLIO", "250"))


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_form():
    return RedirectResponse("/", status_code=303)


@app.post("/portfolio", response_class=HTMLResponse)
async def portfolio_run(
    upload: UploadFile | None = File(None),
    pasted: str = Form(""),
    single: str = Form(""),
):
    """Sweep a grantee list. Accepts a CSV, XLSX or PDF upload, or pasted EINs.

    Nothing is written to disk: the upload is parsed in memory and discarded.
    """
    try:
        if single.strip():
            result = intake_mod.from_text(single)
        elif upload is not None and upload.filename:
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                return HTMLResponse(render.portfolio_upload_page(
                    f"That file is {len(data) / 1_048_576:.1f} MB; the limit is "
                    f"{MAX_UPLOAD_BYTES // 1_048_576} MB."
                ), status_code=413)
            result = intake_mod.parse_upload(upload.filename, data)
        elif pasted.strip():
            result = intake_mod.from_text(pasted)
        else:
            return HTMLResponse(render.portfolio_upload_page(
                "Upload a file or paste some EINs."
            ), status_code=400)
    except intake_mod.UnsupportedFile as exc:
        return HTMLResponse(render.portfolio_upload_page(str(exc)), status_code=415)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(render.portfolio_upload_page(
            f"Could not read that file: {exc}"
        ), status_code=400)

    # Lines that named an organization instead of giving an EIN. Resolving
    # them here means a funder can paste the list they actually have.
    names_to_resolve = [
        row.name for row in result.unmatched
        if row.name and len(row.name.strip()) > 2
    ][:25]
    resolved, unresolved = ({}, [])
    if names_to_resolve:
        resolved, unresolved = portfolio_mod.resolve_names(names_to_resolve)
        # Every attempted name is now accounted for in the unresolved block,
        # so drop it from "rows with no EIN" rather than listing it twice.
        attempted = set(names_to_resolve)
        result.unmatched = [
            row for row in result.unmatched if row.name not in attempted
        ]

    eins = result.eins + [e for e in resolved if e not in result.eins]
    if not eins:
        if unresolved:
            report = portfolio_mod.PortfolioReport(
                generated_at=__import__("datetime").date.today().strftime("%B %-d, %Y")
            )
            return HTMLResponse(
                render.portfolio_report_page(report, intake=result,
                                             unresolved=unresolved),
                status_code=200,
            )
        return HTMLResponse(render.portfolio_upload_page(
            "No EINs or recognizable organization names were found. If this is "
            "a scanned PDF there is no text layer to read — export as CSV or "
            "XLSX instead."
        ), status_code=422)

    truncated = False
    if len(eins) > MAX_PORTFOLIO:
        eins, truncated = eins[:MAX_PORTFOLIO], True

    names = {r.ein: r.name for r in result.rows if r.ein and r.name}
    names.update(resolved)
    report = portfolio_mod.evaluate(eins, names=names, awards=result.awards)
    if truncated:
        report.notes.append(
            f"Only the first {MAX_PORTFOLIO} organizations were swept; the "
            f"file contained {len(result.eins)}."
        )
    report.notes.extend(result.notes)
    return HTMLResponse(
        render.portfolio_report_page(report, intake=result, unresolved=unresolved)
    )


@app.post("/api/portfolio.json")
async def portfolio_json(upload: UploadFile | None = File(None), pasted: str = Form("")):
    if upload is not None and upload.filename:
        result = intake_mod.parse_upload(upload.filename, await upload.read())
    else:
        result = intake_mod.from_text(pasted)
    eins = result.eins[:MAX_PORTFOLIO]
    if not eins:
        return JSONResponse({"error": "no EINs found"}, status_code=422)
    report = portfolio_mod.evaluate(eins)
    return JSONResponse({
        "generated_at": report.generated_at,
        "counts": report.summary_counts,
        "size_distribution": report.size_distribution,
        "organizations": [
            {
                "ein": o.ein, "name": o.name, "severity": o.severity,
                "fiscal_year": o.latest_fiscal_year, "revenue": o.revenue,
                "source": o.source, "percentile": o.percentile,
                "signals": [
                    {"kind": s.kind, "severity": s.severity,
                     "headline": s.headline, "detail": s.detail}
                    for s in o.signals
                ],
            }
            for o in report.organizations
        ],
        "failed": [{"ein": o.ein, "error": o.error} for o in report.failed],
        "unmatched_rows": len(result.unmatched),
    })


@app.get("/healthz")
def healthz():
    """What is actually working, per capability.

    An earlier version reported only the revocation index, so a deployment
    with no Form 990 XML and no peer percentiles looked entirely healthy while
    two of the three features were silently off. Each entry says whether the
    capability is live and, when it is not, the command that turns it on.
    """
    import sqlite3
    from datetime import datetime

    from diligence import peers as peers_mod, xml990

    def rows(path, table):
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    status = irs_status.check("000000000")
    age = None
    if status.index_built_at:
        try:
            built = datetime.fromisoformat(status.index_built_at)
            age = (datetime.now() - built).days
        except ValueError:
            age = None

    xml_rows = rows(xml990.XML_DB, "filings")
    peer_rows = rows(peers_mod.DB_PATH, "peer_orgs")

    capabilities = {
        "exemption_status": {
            "live": status.is_known,
            "detail": f"index built {status.index_built_at}, {age} days old"
            if status.is_known else "not built",
            "stale": bool(age is not None and age > irs_status.STALE_AFTER_DAYS),
            "fix": None if status.is_known
            else "python -m diligence.irs_status --build",
        },
        "current_financials": {
            "live": bool(xml_rows),
            "detail": f"{xml_rows:,} filings indexed" if xml_rows
            else "no Form 990 XML index: financials fall back to the IRS "
                 "extract, which runs 12-24 months behind",
            "fix": None if xml_rows
            else "set GRANTSIGHT_XML_INDEX_URLS and redeploy",
        },
        "peer_percentiles": {
            "live": bool(peer_rows),
            "detail": f"{peer_rows:,} organizations in the population" if peer_rows
            else "no peer index: the size-distribution section will not render",
            "fix": None if peer_rows
            else "set GRANTSIGHT_PEERS_SOI and GRANTSIGHT_PEERS_BMF, or run "
                 "python -m diligence.peers --build",
        },
        "press_mentions": {
            "live": os.environ.get("GRANTSIGHT_NEWS") == "1"
            and bool(os.environ.get("NEWSDATA_API_KEY")
                     or os.environ.get("GNEWS_API_KEY")
                     or os.environ.get("GRANTSIGHT_NEWS_URL")),
            "detail": os.environ.get("GRANTSIGHT_NEWS_PROVIDER") or "not configured",
            "fix": None,
        },
    }

    degraded = [k for k, v in capabilities.items()
                if not v["live"] and k != "press_mentions"]
    return {
        "ok": True,
        "degraded": degraded,
        "capabilities": capabilities,
        # Kept for anything already watching these two keys.
        "irs_index_built": status.is_known,
        "irs_index_at": status.index_built_at,
    }
