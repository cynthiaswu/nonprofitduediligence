"""A deliberately boring HTTP layer: on-disk cache, request spacing, one retry.

ProPublica asks that heavy users be gentle. Caching is also what makes the
app usable as a public demo without hammering someone else's free API.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import USER_AGENT

CACHE_DIR = Path(os.environ.get("GRANTSIGHT_CACHE", "./.cache"))
MIN_INTERVAL_S = float(os.environ.get("GRANTSIGHT_MIN_INTERVAL", "0.35"))
DEFAULT_TTL_S = int(os.environ.get("GRANTSIGHT_TTL", str(60 * 60 * 24 * 7)))

_lock = threading.Lock()
_last_request_at = 0.0


def _contact() -> str:
    contact = os.environ.get("GRANTSIGHT_CONTACT")
    return f"{USER_AGENT} contact:{contact}" if contact else USER_AGENT


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return CACHE_DIR / f"{digest}.json"


def _throttle() -> None:
    global _last_request_at
    with _lock:
        wait = MIN_INTERVAL_S - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()


class FetchError(RuntimeError):
    """Raised when a source is unreachable. Callers degrade, they don't crash."""


def get_json(url: str, ttl_s: int = DEFAULT_TTL_S) -> Any:
    """GET a JSON document, using the on-disk cache when it is still fresh."""
    path = _cache_path(url)
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl_s:
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)

    _throttle()
    headers = {"User-Agent": _contact(), "Accept": "application/json"}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = httpx.get(
                url, headers=headers, timeout=20.0, follow_redirects=True
            )
            if response.status_code == 404:
                raise FetchError(f"not found: {url}")
            response.raise_for_status()
            payload = response.json()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload))
            return payload
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport failure degrades
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise FetchError(f"{url}: {last_error}")


def get_bytes(url: str, dest: Path, ttl_s: int = DEFAULT_TTL_S) -> Path:
    """Download a large file (IRS bulk zips) to `dest`, refreshing on TTL."""
    if dest.exists() and (time.time() - dest.stat().st_mtime) < ttl_s:
        return dest
    _throttle()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": _contact()},
            timeout=180.0,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    handle.write(chunk)
        tmp.replace(dest)
        return dest
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise FetchError(f"{url}: {exc}") from exc
