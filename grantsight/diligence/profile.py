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
from dataclasses import dataclass, field
from datetime import date
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
    whole = _normalize(name)
    if not whole:
        return False, []
    tokens = _distinctive_tokens(name)

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

    # A distinctive name needs one corroborator; a generic one needs two.
    required = 1 if len(tokens) >= 2 else 2
    return len(corroborators) >= required, corroborators


def build_query(org: dict) -> str:
    parts = [f'"{org.get("name", "").strip()}"']
    if org.get("city"):
        parts.append(org["city"])
    if org.get("state"):
        parts.append(org["state"])
    parts.append("nonprofit")
    return " ".join(p for p in parts if p.strip())


def find_press(
    org: dict,
    search: SearchFn | None = None,
    limit: int = 5,
) -> list[PressItem]:
    """Identity-confirmed press mentions. Returns [] when nothing confirms.

    `search` takes a query string and yields dicts with title, url, and
    optionally snippet, source and published. Supplying it is the caller's
    job; there is no default provider, so this is inert unless wired up.
    """
    if search is None:
        return []

    confirmed: list[PressItem] = []
    try:
        results = list(search(build_query(org)))
    except Exception:  # noqa: BLE001 - a failed search is simply no press
        return []

    for item in results:
        url = (item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not url or not title:
            continue
        ok, corroborators = confirm_identity(item, org)
        if not ok:
            continue
        confirmed.append(PressItem(
            title=title,
            url=url,
            source=(item.get("source") or _domain(url)),
            published=item.get("published"),
            corroborated_by=corroborators,
        ))
        if len(confirmed) >= limit:
            break
    return confirmed


def build(brief, search: SearchFn | None = None, check_site: bool = True) -> Highlights:
    """Assemble the summary block for one brief."""
    h = build_highlights(brief)
    if check_site and h.website:
        h.website_status = check_website(h.website)

    if search is None and os.environ.get("GRANTSIGHT_NEWS") == "1":
        search = _provider_from_env()
    h.press_enabled = search is not None
    if search is not None:
        h.press = find_press(
            {
                "name": brief.name, "city": brief.city, "state": brief.state,
                "ein": brief.ein, "website": h.website,
            },
            search,
        )
    return h


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

        match = re.search(r'"([^"]+)"', query)
        q = f'"{match.group(1)}"' if match else query
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


def _provider_from_env() -> SearchFn | None:
    """Build a provider from env, for operators who have a search API.

    GNEWS_API_KEY         use GNews; takes precedence when set

    GRANTSIGHT_NEWS_URL   a URL template containing {query}
    GRANTSIGHT_NEWS_KEY   optional, sent as Authorization: Bearer
    GRANTSIGHT_NEWS_PATH  dotted path to the result list, default "results"

    Generic results are read leniently: each item's title, url, snippet,
    source and published are taken from the first key that exists.
    """
    gnews_key = os.environ.get("GNEWS_API_KEY")
    if gnews_key:
        return gnews_provider(gnews_key)

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
        if key:
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
                "url": _pick(item, "url", "link"),
                "snippet": _pick(item, "snippet", "description", "summary", "excerpt"),
                "source": _pick(item, "source", "publisher", "site"),
                "published": _pick(item, "published", "date", "published_at"),
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
