"""What this organization is, in a paragraph, before the findings.

Three parts, in descending order of how much they can be trusted:

  Highlights    Derived entirely from the filings already parsed. No network
                calls, no inference. Mission text, scale, staffing, what the
                organization funds.
  Website       A reachability check against the domain the organization
                itself reported on its 990. No search involved, so no chance
                of landing on a different entity.
  Press         Optional, off by default, and gated behind identity
                confirmation.

ON PRESS MENTIONS
-----------------
Nonprofit names collide constantly: there are hundreds of "Hope House" and
"Community Action" organizations. A news search that returns a confident
result about the wrong one is worse than no result, because a diligence brief
is exactly the context where a reader will believe it.

So every candidate item must corroborate identity before it appears:

  * the organization's name must match after normalization, and
  * at least one independent identifier must also appear -- the city, the
    state, the EIN, or the domain the organization reported on its filing.

Names that are not distinctive (few tokens, all common words) require two
corroborators instead of one. Items that fail are dropped silently rather than
shown with a warning, because a warning next to a plausible headline does not
stop anyone believing it.

Press items are context and never findings. Nothing here can raise the
severity of a brief, and no article is summarized beyond its own headline --
the link is the point.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable

SearchFn = Callable[[str], Iterable[dict]]

# Words too common to identify an organization on their own.
_GENERIC = {
    "the", "of", "and", "for", "a", "an", "inc", "incorporated", "corp",
    "corporation", "co", "company", "llc", "ltd", "trust", "fund", "foundation",
    "association", "society", "center", "centre", "institute", "council",
    "committee", "project", "program", "services", "service", "group",
    "organization", "org", "charity", "charities", "alliance", "network",
    "community", "national", "american", "america", "united", "international",
    "friends", "hope", "new", "first", "st", "saint", "church", "house",
}

STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska",
    "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey",
    "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
    "DC": "district of columbia",
}


# ---------------------------------------------------------------------------
# highlights
# ---------------------------------------------------------------------------
@dataclass
class Highlights:
    headline: str = ""
    mission: str | None = None
    facts: list[str] = field(default_factory=list)
    website: str | None = None
    website_status: str | None = None
    press: list["PressItem"] = field(default_factory=list)
    press_enabled: bool = False
    # "no provider configured", "searched, nothing matched" and "found some"
    # are three different states. Rendering the first two identically -- as an
    # absent section -- makes a misconfiguration look like an organization
    # with no coverage, which is exactly backwards for a diligence tool.
    press_status: str = "not_configured"
    press_searched: int = 0
    press_provider: str | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.headline or self.mission or self.facts)


@dataclass
class PressItem:
    title: str
    url: str
    source: str | None = None
    published: str | None = None
    corroborated_by: list[str] = field(default_factory=list)
    # True when the only corroboration came from the provider's own query
    # semantics rather than text this code inspected. Weaker evidence, and
    # the brief says so.
    provider_asserted: bool = False


def _money(value: float | None) -> str:
    if value is None:
        return "an undisclosed amount"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,.0f}"


def _sector_phrase(ntee: str | None) -> str | None:
    if not ntee or "—" not in ntee:
        return None
    return ntee.split("—", 1)[1].strip().lower()


def build_highlights(brief) -> Highlights:
    """A description of the organization, from the filings alone."""
    h = Highlights()
    m, xml = brief.metrics, brief.xml

    sector = _sector_phrase(brief.ntee)
    where = brief.location or None
    kind = brief.subsection or "exempt organization"
    if xml and xml.form_type == "990-PF" or (
        brief.filings and brief.filings[0].is_private_foundation
    ):
        kind = "private foundation"

    lead = [kind]
    if sector:
        lead.append(f"working in {sector}")
    if where:
        lead.append(f"based in {where}")
    sentence = " ".join(lead)
    # Not .capitalize(): it lowercases the rest, turning "Baltimore, MD" into
    # "baltimore, md" and "501(c)(3)" into "501(c)(3)" only by luck.
    h.headline = (sentence[:1].upper() + sentence[1:] if sentence else "") + "."

    if xml and xml.mission:
        h.mission = xml.mission

    facts: list[str] = []
    if m and m.latest_fiscal_year and brief.filings:
        revenue = brief.filings[0].get("total_revenue")
        facts.append(
            f"{_money(revenue)} revenue in FY{m.latest_fiscal_year}"
            + (f", {m.revenue_trend} over {m.trend_span} filings"
               if m.revenue_trend and m.trend_span else "")
        )
    if xml and xml.employee_count:
        staffing = f"{int(xml.employee_count):,} employees"
        if xml.volunteer_count:
            staffing += f" and {int(xml.volunteer_count):,} volunteers"
        facts.append(staffing)
    if m and m.program_expense_ratio is not None:
        facts.append(f"{m.program_expense_ratio * 100:.0f}% of spending on programs")
    if m and m.months_of_unrestricted_reserve is not None:
        facts.append(
            f"{m.months_of_unrestricted_reserve:.1f} months of unrestricted reserve"
        )
    elif m and m.months_of_reserve is not None:
        facts.append(f"{m.months_of_reserve:.1f} months of reserve")
    if m and m.public_support_ratio is not None:
        facts.append(f"{m.public_support_ratio * 100:.0f}% public support")
    if xml and xml.grants:
        total = sum(g.total for g in xml.grants if g.total)
        facts.append(f"{_money(total)} granted to {len(xml.grants)} organizations")
    if xml and xml.principal_officer:
        facts.append(f"led by {xml.principal_officer}")
    h.facts = facts

    if xml and xml.website:
        h.website = _normalize_url(xml.website)
    return h


def _normalize_url(value: str) -> str | None:
    value = (value or "").strip()
    if not value or value.lower() in ("n/a", "none", "na"):
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def check_website(url: str | None, timeout: float = 6.0) -> str | None:
    """Is the organization's own reported site reachable?

    Uses only the domain the organization published on its own filing, so
    there is no possibility of checking a different entity. A dead site is a
    weak signal, never a finding -- plenty of healthy organizations let a
    domain lapse.
    """
    if not url:
        return None
    try:
        import httpx

        from . import USER_AGENT

        response = httpx.get(
            url, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code < 400:
            return "reachable"
        return f"returned HTTP {response.status_code}"
    except Exception:  # noqa: BLE001 - any failure is just "unreachable"
        return "did not respond"


# ---------------------------------------------------------------------------
# identity confirmation
# ---------------------------------------------------------------------------
def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _distinctive_tokens(name: str) -> list[str]:
    return [t for t in _normalize(name).split() if t not in _GENERIC and len(t) > 2]


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    match = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return match[4:] if match.startswith("www.") else match or None


def confirm_identity(item: dict, org: dict) -> tuple[bool, list[str]]:
    """Does this result actually concern this organization?

    Returns (confirmed, corroborating identifiers found).
    """
    haystack = _normalize(
        " ".join(str(item.get(k) or "") for k in ("title", "snippet", "source", "url"))
    )
    raw_haystack = " ".join(
        str(item.get(k) or "") for k in ("title", "snippet", "url")
    ).lower()

    name = org.get("name") or ""
    whole = _normalize(press_name(name))
    if not whole:
        return False, []
    tokens = _distinctive_tokens(press_name(name))

    # A name with no distinctive tokens at all ("Hope House", "The Education
    # Fund") must appear verbatim, and still needs two corroborators below.
    if tokens:
        name_matches = whole in haystack or all(t in haystack for t in tokens)
    else:
        name_matches = whole in haystack
    if not name_matches:
        return False, []

    corroborators: list[str] = []
    city = org.get("city")
    if city and _normalize(city) in haystack:
        corroborators.append(f"city ({city})")
    state = (org.get("state") or "").upper()
    if state and STATE_NAMES.get(state, "").strip() and \
            STATE_NAMES[state] in haystack:
        corroborators.append(f"state ({state})")
    ein = re.sub(r"\D", "", org.get("ein") or "")
    if ein and (ein in raw_haystack.replace("-", "")):
        corroborators.append("EIN")
    domain = _domain(org.get("website"))
    if domain and domain in raw_haystack:
        corroborators.append(f"domain ({domain})")

    # Some providers corroborate through their query rather than their
    # payload: GDELT can be asked for articles whose FULL TEXT contains the
    # name and the city, then returns only a headline. That is real evidence
    # this code cannot see, so it is accepted -- but it is the provider's
    # word, not something verified here, so it is tracked separately and
    # carries an extra condition.
    asserted = [c for c in (item.get("corroborated_by") or []) if c]
    if asserted:
        # The name must appear in the HEADLINE, not merely somewhere in the
        # url or source domain. Without this, a provider could assert its way
        # past the check on an article that only mentions the organization in
        # passing.
        title_text = _normalize(str(item.get("title") or ""))
        in_title = whole in title_text or (
            bool(tokens) and all(t in title_text for t in tokens)
        )
        if in_title:
            corroborators = corroborators + [c for c in asserted if c not in corroborators]

    # At least one independent identifier is always required, however
    # distinctive the name looks. A tier requiring zero for long names was
    # tried and reverted: it admitted an article about a same-named
    # organization in another city, which is the precise failure this check
    # exists to prevent.
    required = 1 if len(tokens) >= 2 else 2
    return len(corroborators) >= required, corroborators


# Legal-entity suffixes that appear on the 990 and essentially never in a
# headline. Stripping them is the difference between matching local coverage
# and matching nothing.
_LEGAL_SUFFIXES = (
    "inc", "inc.", "incorporated", "corp", "corp.", "corporation", "llc",
    "l.l.c.", "ltd", "ltd.", "co", "co.", "pc", "p.c.", "usa", "u s a",
)


def press_name(name: str) -> str:
    """Turn an IRS legal name into something a newsroom would print.

    "HARBOR STREET YOUTH COALITION, INC." -> "HARBOR STREET YOUTH COALITION"

    Only legal suffixes and trailing punctuation are removed. Case is left
    alone deliberately: an earlier version title-cased registered names, which
    then needed an acronym heuristic to avoid turning ACLU into Aclu -- and
    that heuristic was wrong on NAACP and on "ST JUDE" in opposite directions.
    Case never mattered, because identity matching normalizes it and search
    APIs ignore it, so the whole class of bug is deleted rather than tuned.
    """
    tokens = (name or "").strip().split()
    while tokens and tokens[-1].lower().strip(",.") in _LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[-1].strip(",.") == "":
        tokens.pop()
    if tokens:
        tokens[-1] = tokens[-1].rstrip(",.")
    return " ".join(tokens).strip() or (name or "").strip()


def core_name(name: str, keep: int = 3) -> str:
    """The first few distinctive words -- what an outlet actually prints.

    "PLANNED PARENTHOOD FEDERATION OF AMERICA" -> "PLANNED PARENTHOOD"

    Almost no article uses a national body's full registered name, so an exact
    phrase search on it returns nothing even for organizations covered daily.
    """
    words = press_name(name).split()
    distinctive, taken = [], 0
    for word in words:
        distinctive.append(word)
        if word.lower().strip(",.") not in _GENERIC and len(word) > 2:
            taken += 1
        if taken >= keep:
            break
    while distinctive and distinctive[-1].lower().strip(",.") in _GENERIC:
        distinctive.pop()
    return " ".join(distinctive) or press_name(name)


def build_queries(org: dict) -> list[str]:
    """Progressively looser queries, tried in order until something confirms.

    Exact-phrase on the full registered name is the most precise and the most
    likely to return nothing; the shortened form catches the way the
    organization is actually referred to.
    """
    full = press_name(org.get("name", ""))
    short = core_name(org.get("name", ""))
    city = (org.get("city") or "").strip()
    dba = (org.get("dba") or "").strip()

    queries: list[str] = []
    def add(query: str):
        if query and query not in queries:
            queries.append(query)

    if dba and dba.lower() != full.lower():
        add(f'"{press_name(dba)}" {city}'.strip())
    add(f'"{full}" {city}'.strip())
    if short.lower() != full.lower():
        add(f'"{short}" {city}'.strip())
    return queries


def build_query(org: dict) -> str:
    """Name plus city. No "nonprofit" keyword and no legal suffixes.

    Adding "nonprofit" narrows to articles that use the word, which local
    coverage of a specific organization usually does not. The city is the
    useful discriminator, and it doubles as a corroborator later.
    """
    name = press_name(org.get("name", ""))
    parts = [f'"{name}"'] if name else []
    dba = (org.get("dba") or "").strip()
    if dba and dba.lower() != name.lower():
        parts.append(f'OR "{press_name(dba)}"')
    if org.get("city"):
        parts.append(org["city"])
    return " ".join(p for p in parts if p.strip())


def quoted_term(query: str) -> str:
    """The quoted phrase from a query built by build_queries.

    Adapters need the bare organization name, not the whole query: GNews ANDs
    every term, so sending the city as well drops articles that name the
    organization without it. Extracting it in one place beats each adapter
    carrying its own regex.
    """
    match = re.search(r'"([^"]+)"', query or "")
    return match.group(1) if match else (query or "").strip()


NEWSDATA_ENDPOINT = "https://newsdata.io/api/1"
# /latest only reaches back 48 hours, which is useless for diligence: a
# grantmaker needs the last year or two, not the last two days. /archive is
# the right endpoint, and it costs 5 credits per call instead of 1.
NEWSDATA_LOOKBACK_DAYS = int(os.environ.get("GRANTSIGHT_NEWS_LOOKBACK_DAYS", "540"))


def newsdata_provider(
    api_key: str,
    endpoint: str = NEWSDATA_ENDPOINT,
    lookback_days: int = NEWSDATA_LOOKBACK_DAYS,
) -> SearchFn:
    """Search adapter for NewsData.io (https://newsdata.io/documentation).

    Preferred over GDELT because it returns description and content, so
    corroboration is verified here rather than asserted by the provider.

    Uses /archive, not /latest. /latest covers 48 hours. How far /archive
    actually reaches depends on the plan -- roughly six months on the free
    tier, two years on Professional -- so a lookback longer than the plan
    allows silently returns fewer results rather than erroring.
    """

    def search(query: str):
        import httpx

        from . import USER_AGENT

        start = (date.today() - timedelta(days=lookback_days)).isoformat()
        response = httpx.get(
            f"{endpoint}/archive",
            params={
                "apikey": api_key,
                # Only the quoted name: NewsData ANDs terms like most engines,
                # so adding the city drops articles that omit it. The city is
                # still required as a corroborator downstream.
                "q": f'"{quoted_term(query)}"',
                "language": "en",
                "country": "us",
                "from_date": start,
                "size": 10,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        if response.status_code == 429:
            raise RuntimeError(
                "NewsData rate limit reached (check X-RateLimit-Remaining; the "
                "free tier is 200 credits a day and /archive costs 5 per call)"
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            detail = payload.get("results") or payload.get("message") or payload
            raise RuntimeError(f"NewsData error: {detail}")

        out = []
        for article in payload.get("results") or []:
            if not isinstance(article, dict):
                continue
            snippet = " ".join(
                part for part in (article.get("description"), article.get("content"))
                if part and part != "ONLY AVAILABLE IN PAID PLANS"
            )
            out.append({
                "title": article.get("title"),
                # NewsData calls it "link", not "url".
                "url": article.get("link"),
                "snippet": snippet or None,
                "source": article.get("source_name") or article.get("source_id"),
                "published": (article.get("pubDate") or "")[:10] or None,
            })
        return out

    return search


GNEWS_ENDPOINT = "https://gnews.io/api/v4/search"


def gnews_provider(api_key: str, endpoint: str = GNEWS_ENDPOINT) -> SearchFn:
    """Search adapter for GNews (https://gnews.io/docs/v4).

    Only the quoted organization name is sent; GNews ANDs every term, so the
    city/state/"nonprofit" terms from build_query would drop articles that
    name the organization without them. Identity confirmation in find_press
    still requires a city, state, EIN or domain corroborator in the returned
    title, description or content.
    """

    def search(query: str):
        import httpx

        from . import USER_AGENT

        q = f'"{quoted_term(query)}"' 
        response = httpx.get(
            endpoint,
            params={
                "q": q, "lang": "en", "country": "us", "max": 10,
                "sortby": "publishedAt", "apikey": api_key,
            },
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=15.0,
        )
        response.raise_for_status()
        articles = response.json().get("articles") or []
        out = []
        for a in articles:
            if not isinstance(a, dict):
                continue
            snippet = " ".join(
                s for s in (a.get("description"), a.get("content")) if s
            )
            out.append({
                "title": a.get("title"),
                "url": a.get("url"),
                "snippet": snippet or None,
                "source": _pick(a, "source"),
                "published": (a.get("publishedAt") or "")[:10] or None,
            })
        return out

    return search


GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MIN_INTERVAL_S = 5.0  # GDELT rejects more than one request per 5 s per IP
_gdelt_last_call = 0.0
# FastAPI runs sync handlers in a threadpool, so the throttle needs a lock or
# two concurrent briefs will both sail past it and get rate-limited.
_gdelt_lock = threading.Lock()


def gdelt_provider(endpoint: str = GDELT_ENDPOINT, timespan: str = "24months",
                   org: dict | None = None) -> SearchFn:
    """Search adapter for the GDELT DOC 2.0 API (no key required).

    GDELT returns title, url, domain and date only, no excerpt, so a title
    alone can rarely show the city or state that confirm_identity needs. The
    query therefore asks GDELT for articles whose full text contains the
    quoted name AND the organization's city or state name, and each result
    is marked as corroborated by that full-text match. Articles that name
    the organization without any place term are not returned at all, which
    is the conservative side to err on.
    """

    def search(query: str):
        global _gdelt_last_call
        import time

        import httpx

        from . import USER_AGENT

        name = quoted_term(query)
        places = []
        if org:
            if org.get("city"):
                places.append(f'"{org["city"]}"')
            state_name = STATE_NAMES.get((org.get("state") or "").upper(), "").strip()
            if state_name:
                places.append(f'"{state_name.title()}"')
        q = f'"{name}"'
        if len(places) > 1:
            q += " (" + " OR ".join(places) + ")"
        elif places:
            q += f" {places[0]}"
        q += " sourcelang:english"

        with _gdelt_lock:
            wait = GDELT_MIN_INTERVAL_S - (time.monotonic() - _gdelt_last_call)
            if wait > 0:
                time.sleep(wait)
            _gdelt_last_call = time.monotonic()
        response = httpx.get(
            endpoint,
            params={"query": q, "mode": "artlist", "format": "json",
                    "maxrecords": 25, "sort": "datedesc", "timespan": timespan},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20.0,
        )
        response.raise_for_status()
        if not response.text.strip():
            return []
        articles = response.json().get("articles") or []
        corroborator = ("full-text match (" + " or ".join(p.strip('"') for p in places) + ")"
                        if places else None)
        out = []
        for a in articles:
            if not isinstance(a, dict):
                continue
            seen = a.get("seendate") or ""  # 20250912T083000Z
            published = (f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}"
                         if len(seen) >= 8 and seen[:8].isdigit() else None)
            out.append({
                "title": a.get("title"),
                "url": a.get("url"),
                "snippet": None,
                "source": a.get("domain"),
                "published": published,
                "corroborated_by": [corroborator] if corroborator else [],
            })
        return out

    return search


def find_press(
    org: dict,
    search: SearchFn | None = None,
    limit: int = 5,
    trace: list | None = None,
) -> list[PressItem]:
    """Identity-confirmed press mentions. Returns [] when nothing confirms.

    `search` takes a query string and yields dicts with title, url, and
    optionally snippet, source and published. Supplying it is the caller's
    job; there is no default provider, so this is inert unless wired up.
    """
    if search is None:
        return []

    confirmed: list[PressItem] = []
    seen: set[str] = set()

    for query in build_queries(org):
        try:
            results = list(search(query))
        except Exception as exc:  # noqa: BLE001 - a failed search is no press
            if trace is not None:
                trace.append({"query": query, "error": str(exc)})
            continue

        if trace is not None:
            trace.append({"query": query, "returned": len(results), "items": []})

        for item in results:
            url = (item.get("url") or "").strip()
            title = (item.get("title") or "").strip()
            if not url or not title:
                continue
            ok, corroborators = confirm_identity(item, org)
            if trace is not None:
                trace[-1]["items"].append({
                    "title": title[:90],
                    "confirmed": ok,
                    "corroborated_by": corroborators,
                    "has_snippet": bool(item.get("snippet")),
                    "reason": None if ok else _reject_reason(item, org),
                })
            if not ok or url in seen:
                continue
            seen.add(url)
            asserted = set(item.get("corroborated_by") or [])
            confirmed.append(PressItem(
                title=title, url=url,
                source=(item.get("source") or _domain(url)),
                published=item.get("published"),
                corroborated_by=corroborators,
                provider_asserted=bool(asserted) and set(corroborators) <= asserted,
            ))
            if len(confirmed) >= limit:
                return confirmed
        if confirmed:
            break
    return confirmed


def _reject_reason(item: dict, org: dict) -> str:
    """Why an item did not confirm, in words a human can act on."""
    haystack = _normalize(
        " ".join(str(item.get(k) or "") for k in ("title", "snippet", "source", "url"))
    )
    tokens = _distinctive_tokens(press_name(org.get("name", "")))
    if tokens and not all(t in haystack for t in tokens):
        missing = [t for t in tokens if t not in haystack]
        return f"name not matched (missing: {', '.join(missing[:3])})"
    if not item.get("snippet"):
        return ("name matched but no corroborator found, and this provider "
                "returned no description text to search")
    return "name matched but no city, state, EIN or domain found to corroborate"


def build(brief, search: SearchFn | None = None, check_site: bool = True) -> Highlights:
    """Assemble the summary block for one brief."""
    h = build_highlights(brief)
    if check_site and h.website:
        h.website_status = check_website(h.website)

    org = {
        "name": brief.name,
        # ProPublica's sub_name often carries the name the organization
        # actually operates under, which is what gets printed.
        "dba": getattr(brief, "subtitle", None),
        "city": brief.city, "state": brief.state,
        "ein": brief.ein, "website": h.website,
    }
    if search is None and os.environ.get("GRANTSIGHT_NEWS") == "1":
        search = _provider_from_env(org)
    h.press_enabled = search is not None
    if search is None:
        h.press_status = "not_configured"
        return h

    h.press_provider = (
        os.environ.get("GRANTSIGHT_NEWS_PROVIDER") or "configured provider"
    )
    trace: list = []
    try:
        h.press = find_press(org, search, trace=trace)
    except Exception as exc:  # noqa: BLE001
        h.press_status = "error"
        h.press_provider = f"{h.press_provider} ({exc})"
        return h

    h.press_searched = sum(entry.get("returned", 0) for entry in trace)
    if any("error" in entry for entry in trace):
        h.press_status = "error"
    elif h.press:
        h.press_status = "found"
    elif h.press_searched:
        h.press_status = "searched_no_match"
    else:
        h.press_status = "searched_nothing_returned"
    return h


def _provider_from_env(org: dict | None = None) -> SearchFn | None:
    """Build a provider from env.

    GRANTSIGHT_NEWS_PROVIDER  newsdata | gnews | gdelt | url

    newsdata: NEWSDATA_API_KEY. Description and content, US/local coverage,
              /archive endpoint. Preferred.

    gnews: GNEWS_API_KEY. Returns description and content, so corroboration
           can be verified here. Preferred.
    gdelt: no key. Returns headlines only, so it corroborates through its own
           query instead; see confirm_identity for what that costs.
    url:   GRANTSIGHT_NEWS_URL   a URL template containing {query}
    GRANTSIGHT_NEWS_KEY   optional, sent as Authorization: Bearer
    GRANTSIGHT_NEWS_PATH  dotted path to the result list, default "results"

    Results are read leniently: each item's title, url, snippet, source and
    published are taken from the first key that exists.
    """
    provider = (os.environ.get("GRANTSIGHT_NEWS_PROVIDER") or "").strip().lower()
    if provider == "newsdata" or (not provider and os.environ.get("NEWSDATA_API_KEY")):
        key = os.environ.get("NEWSDATA_API_KEY")
        return newsdata_provider(key) if key else None
    if provider == "gnews" or (not provider and os.environ.get("GNEWS_API_KEY")):
        key = os.environ.get("GNEWS_API_KEY")
        return gnews_provider(key) if key else None
    if provider == "gdelt":
        return gdelt_provider(org=org)

    template = os.environ.get("GRANTSIGHT_NEWS_URL")
    if not template:
        return None

    def search(query: str):
        import json
        from urllib.parse import quote

        import httpx

        from . import USER_AGENT

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        key = os.environ.get("GRANTSIGHT_NEWS_KEY")
        # Most news APIs take the key as a query parameter, not a header; put
        # it in GRANTSIGHT_NEWS_URL for those. The header is for the rest.
        if key and os.environ.get("GRANTSIGHT_NEWS_AUTH", "header") == "header":
            headers["Authorization"] = f"Bearer {key}"
        response = httpx.get(
            template.format(query=quote(query)), headers=headers, timeout=15.0
        )
        response.raise_for_status()
        payload = response.json()
        for step in os.environ.get("GRANTSIGHT_NEWS_PATH", "results").split("."):
            if isinstance(payload, dict):
                payload = payload.get(step, [])
        if not isinstance(payload, list):
            return []
        del json
        return [
            {
                "title": _pick(item, "title", "name", "headline"),
                "url": _pick(item, "url", "link", "documentidentifier"),
                "snippet": _pick(
                    item, "snippet", "description", "summary", "excerpt",
                    "content", "body",
                ),
                "source": _pick(
                    item, "source", "source_name", "source_id", "publisher", "site"
                ),
                "published": _pick(
                    item, "publishedAt", "published_at", "published",
                    "pubDate", "datePublished", "date", "seendate",
                ),
            }
            for item in payload if isinstance(item, dict)
        ]

    return search


def _pick(item: dict, *keys: str):
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("title")
        if value:
            return str(value)
    return None


def freshness_note(items: list[PressItem], today: date | None = None) -> str | None:
    if not items:
        return None
    today = today or date.today()
    return (
        f"{len(items)} press mention{'s' if len(items) != 1 else ''} matched this "
        f"organization's identity. Retrieved {today.isoformat()}."
    )


def diagnose(org: dict, search: SearchFn | None = None) -> dict:
    """Show every query tried, what came back, and why each item was rejected.

    Press matching fails silently by design, which makes it hard to tell a
    coverage problem from a configuration problem. This prints the difference.
    """
    search = search or _provider_from_env(org)
    if search is None:
        return {"error": "No provider configured. Set GRANTSIGHT_NEWS_PROVIDER "
                         "to gnews (with GNEWS_API_KEY), gdelt, or url."}
    trace: list = []
    confirmed = find_press(org, search, trace=trace)
    return {
        "organization": org.get("name"),
        "queries": trace,
        "confirmed": [{"title": p.title, "url": p.url,
                       "corroborated_by": p.corroborated_by} for p in confirmed],
    }


if __name__ == "__main__":  # pragma: no cover - CLI
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="Diagnose press matching for one organization."
    )
    parser.add_argument("--ein", help="look the organization up first")
    parser.add_argument("--name")
    parser.add_argument("--city")
    parser.add_argument("--state")
    args = parser.parse_args()

    target = {"name": args.name, "city": args.city, "state": args.state}
    if args.ein:
        from .propublica import fetch_organization
        record = fetch_organization(args.ein)["organization"]
        target = {
            "name": record.get("name"),
            "dba": (record.get("sub_name") or "").strip() or None,
            "city": (record.get("city") or "").title(),
            "state": record.get("state"),
            "ein": str(record.get("ein")),
            "website": None,
        }

    print(f"Organization: {target['name']} ({target.get('city')}, {target.get('state')})")
    print("Queries that will be tried, in order:")
    for q in build_queries(target):
        print(f"  {q}")
    print()
    print(_json.dumps(diagnose(target), indent=2)[:6000])
