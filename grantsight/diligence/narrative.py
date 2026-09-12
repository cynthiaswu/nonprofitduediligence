"""Optional one-paragraph summary.

Design constraint worth stating plainly: the model never sees the raw 990 data.
It receives only the findings this codebase already computed and verified, and
it is instructed to introduce no numbers that are not in that list. A model
that cannot see raw data cannot invent a figure from it. If the API key is
absent, a deterministic template runs instead and the brief looks the same.
"""

from __future__ import annotations

import json
import os

import httpx

MODEL = os.environ.get("GRANTSIGHT_MODEL", "claude-sonnet-4-6")

SYSTEM = """You write one paragraph of a nonprofit due-diligence brief for a \
grantmaker's program officer.

Rules, in order of importance:
1. Use ONLY the findings given to you. Introduce no number, date, name, or fact \
that is not in them.
2. Never recommend funding or declining. Describe; do not decide.
3. Missing data is not a negative finding. Say what is unknown neutrally.
4. Four sentences maximum. Plain prose, no headers, no bullets, no hedging \
filler like "it is important to note".
5. Lead with the exemption status finding if one exists."""


def _fallback(brief) -> str:
    """Deterministic summary. Never repeats the verdict rendered above it."""
    parts: list[str] = []
    critical = [f for f in brief.flags if f.severity == "critical"]
    watch = [f for f in brief.flags if f.severity == "watch"]
    if critical:
        parts.append(critical[0].detail.split(".")[0] + ".")
    if watch:
        titles = ", ".join(f.title.lower() for f in watch[:3])
        parts.append(f"Raise with the organization: {titles}.")
    elif not critical:
        parts.append("Nothing in the public record was flagged for follow-up.")
    if brief.gaps:
        parts.append(
            f"{len(brief.gaps)} things this brief could not determine are "
            f"listed at the end."
        )
    return " ".join(parts)


def summarize(brief) -> str:
    """Return a short paragraph. Falls back to a template without an API key."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _fallback(brief)

    findings = {
        "organization": brief.name,
        "location": brief.location,
        "type": brief.subsection,
        "sector": brief.ntee,
        "exempt_status": brief.status.state if brief.status else "unknown",
        "exempt_status_detail": brief.status.detail if brief.status else "",
        "latest_fiscal_year": brief.metrics.latest_fiscal_year if brief.metrics else None,
        "flags": [
            {"severity": f.severity, "title": f.title, "detail": f.detail}
            for f in brief.flags
        ],
        "not_determined": brief.gaps,
    }

    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 400,
            "system": SYSTEM,
            "messages": [{
                "role": "user",
                "content": "Findings:\n" + json.dumps(findings, indent=2),
            }],
        },
        timeout=45.0,
    )
    response.raise_for_status()
    blocks = response.json().get("content", [])
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return text.strip() or _fallback(brief)
