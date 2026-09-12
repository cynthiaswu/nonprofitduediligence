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

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from diligence import brief as brief_mod, irs_status, render
from diligence.http import FetchError
from diligence.propublica import NotFound, looks_like_ein, resolve

app = FastAPI(title="GrantSight", docs_url="/api/docs")

PEERS_DEFAULT = os.environ.get("GRANTSIGHT_PEERS", "1") == "1"
NARRATIVE_DEFAULT = os.environ.get("GRANTSIGHT_NARRATIVE", "1") == "1"


@app.get("/", response_class=HTMLResponse)
def home(q: str = Query("", description="Organization name or EIN")):
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


@app.get("/healthz")
def healthz():
    """Reports whether the IRS index exists, since the app runs without it."""
    status = irs_status.check("000000000")
    return {
        "ok": True,
        "irs_index_built": status.is_known,
        "irs_index_at": status.index_built_at,
    }
