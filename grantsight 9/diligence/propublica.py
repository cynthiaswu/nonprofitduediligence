"""ProPublica Nonprofit Explorer API v2 client.

Two endpoints, GET only, no API key:
  /search.json          -> organization objects only (no financials since v2)
  /organizations/:ein.json -> organization + filings_with_data + filings_without_data

Terms of use are non-commercial with an attribution requirement, so every brief
this app renders carries the citation. See README before putting it behind a
paywall or selling ads against it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from .http import FetchError, get_json

BASE = "https://projects.propublica.org/nonprofits/api/v2"
ATTRIBUTION = (
    "Data from ProPublica Nonprofit Explorer "
    "(https://projects.propublica.org/nonprofits/), derived from IRS Form 990 "
    "filings and the IRS Exempt Organizations Business Master File."
)

NTEE_MAJOR_GROUPS = {
    "A": (1, "Arts, culture & humanities"),
    "B": (2, "Education"),
    "C": (3, "Environment"),
    "D": (3, "Animals"),
    "E": (4, "Health"),
    "F": (4, "Mental health"),
    "G": (4, "Disease & disorders"),
    "H": (4, "Medical research"),
    "I": (7, "Crime & legal"),
    "J": (7, "Employment"),
    "K": (5, "Food & agriculture"),
    "L": (5, "Housing & shelter"),
    "M": (5, "Public safety & disaster"),
    "N": (5, "Recreation & sports"),
    "O": (5, "Youth development"),
    "P": (5, "Human services"),
    "Q": (6, "International & foreign affairs"),
    "R": (7, "Civil rights & advocacy"),
    "S": (7, "Community improvement"),
    "T": (7, "Philanthropy & grantmaking"),
    "U": (7, "Science & technology"),
    "V": (7, "Social science"),
    "W": (7, "Public & societal benefit"),
    "X": (8, "Religion"),
    "Y": (9, "Mutual benefit"),
    "Z": (10, "Unclassified"),
}


class NotFound(FetchError):
    """The EIN is not in ProPublica's index at all."""


def clean_ein(value: str) -> str:
    """'52-1693387', '52 1693387', ' 521693387 ' -> '521693387'."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 9:
        raise ValueError(
            f"An EIN is nine digits; got {len(digits)} from {value!r}."
        )
    return digits


def looks_like_ein(value: str) -> bool:
    return len(re.sub(r"\D", "", value or "")) == 9


def format_ein(ein: str) -> str:
    ein = clean_ein(ein)
    return f"{ein[:2]}-{ein[2:]}"


def search(
    query: str,
    state: str | None = None,
    ntee_major: int | None = None,
    c_code: int | None = 3,
    page: int = 0,
) -> dict[str, Any]:
    """Keyword search. Returns organization objects with no financial data."""
    params = [f"q={quote(query)}", f"page={page}"]
    if state:
        params.append(f"state%5Bid%5D={quote(state.upper())}")
    if ntee_major:
        params.append(f"ntee%5Bid%5D={ntee_major}")
    if c_code:
        params.append(f"c_code%5Bid%5D={c_code}")
    # Search results churn less than filings; a shorter TTL keeps them current.
    return get_json(f"{BASE}/search.json?" + "&".join(params), ttl_s=60 * 60 * 24)


def fetch_organization(ein: str) -> dict[str, Any]:
    """Full record for one EIN: organization object plus all filings."""
    ein = clean_ein(ein)
    try:
        payload = get_json(f"{BASE}/organizations/{int(ein)}.json")
    except FetchError as exc:
        if "not found" in str(exc):
            raise NotFound(
                f"EIN {format_ein(ein)} is not in ProPublica's index."
            ) from exc
        raise
    if not payload.get("organization"):
        raise NotFound(f"EIN {format_ein(ein)} returned no organization record.")
    return payload


def resolve(query: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Turn user input into one organization, or a shortlist to choose from.

    Returns (record, candidates). Exactly one of them is populated.
    """
    query = (query or "").strip()
    if not query:
        return None, []
    if looks_like_ein(query):
        return fetch_organization(query), []

    results = search(query).get("organizations", [])
    if not results:
        # Retry without the 501(c)(3) filter: the org may be a (c)(4), (c)(6),
        # or a 4947(a)(1) trust, and "no results" would be misleading.
        results = search(query, c_code=None).get("organizations", [])
    if not results:
        return None, []
    if len(results) == 1:
        return fetch_organization(str(results[0]["ein"]).zfill(9)), []
    return None, results[:15]


def filing_years(record: dict) -> dict:
    """Every year ProPublica knows a return was filed, from BOTH lists.

    ProPublica splits filings into those with extracted financial data and
    those without. The second list is not a footnote: it is where recent
    returns live, because the IRS summary extract lags 12-24 months. A tool
    that reads only the first reports a current filer as years delinquent.

    Returns years plus which list each came from, so the caller can say
    "filed through FY2025, figures only through FY2023" rather than choosing
    one number and being wrong either way.
    """
    with_data = sorted(
        {int(f["tax_prd_yr"]) for f in (record.get("filings_with_data") or [])
         if f.get("tax_prd_yr")},
        reverse=True,
    )
    without_data = sorted(
        {int(f["tax_prd_yr"]) for f in (record.get("filings_without_data") or [])
         if f.get("tax_prd_yr")},
        reverse=True,
    )
    every = sorted(set(with_data) | set(without_data), reverse=True)
    return {
        "with_data": with_data,
        "without_data": without_data,
        "all": every,
        "latest_filed": every[0] if every else None,
        "latest_with_figures": with_data[0] if with_data else None,
    }


def ntee_label(code: str | None) -> str | None:
    if not code:
        return None
    entry = NTEE_MAJOR_GROUPS.get(code[0].upper())
    return f"{code} — {entry[1]}" if entry else code


def ntee_major_id(code: str | None) -> int | None:
    if not code:
        return None
    entry = NTEE_MAJOR_GROUPS.get(code[0].upper())
    return entry[0] if entry else None


if __name__ == "__main__":  # pragma: no cover - CLI
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(
        description="Show exactly what ProPublica's API returns for one EIN."
    )
    parser.add_argument("ein")
    args = parser.parse_args()

    record = fetch_organization(args.ein)
    org = record["organization"]
    years = filing_years(record)

    print(f"{org.get('name')}  ({format_ein(str(org.get('ein')).zfill(9))})")
    print(f"  filings_with_data    : {years['with_data']}")
    print(f"  filings_without_data : {years['without_data']}")
    print(f"  latest filed         : FY{years['latest_filed']}")
    print(f"  latest with figures  : FY{years['latest_with_figures']}")
    print()
    if years["latest_filed"] and years["latest_with_figures"] \
            and years["latest_filed"] > years["latest_with_figures"]:
        print("  => A return is on file more recently than the published"
              " figures.\n     The brief should read 'filed through"
              f" FY{years['latest_filed']}, published figures are"
              f" FY{years['latest_with_figures']}', and must NOT report this"
              " as a late filing.")
    else:
        print("  => Filed and published years agree; no lag to report.")
    print()
    print(_json.dumps(record.get("filings_without_data") or [], indent=2)[:800])
