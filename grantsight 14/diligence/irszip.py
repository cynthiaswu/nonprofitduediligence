"""Read one Form 990 out of an IRS monthly zip, without downloading the zip.

WHY THIS EXISTS
---------------
For a week this codebase assumed a per-object URL existed somewhere: some
host that would serve `{object_id}_public.xml` on demand. Every fix swapped
one such URL for another. All of them were measured, and all of them fail for
recent filings:

  s3.amazonaws.com/irs-form-990          NoSuchKey. The IRS announced on
                                         16 Dec 2021 it would stop updating
                                         this bucket. It is dead, as advertised.
  gt990datalake-rawdata (GivingTuesday)  NoSuchKey for 2026 objects. The mirror
                                         carries older filings only.
  projects.propublica.org/download-xml   HTTP 403, "Security Check". Their XML
                                         link works in a browser and is refused
                                         to servers. Not a usable source.

The IRS itself distributes e-file XML one way only: monthly zips at
apps.irs.gov/pub/epostcard/990/xml/{YYYY}/{YYYY}_TEOS_XML_{MM}{A..D}.zip.
There is no per-object endpoint and there never was one. That assumption was
the root of the failure.

A monthly zip is large, and a web request cannot wait for it. But a zip is
random-access by design: the central directory sits at the end, and every
member records its own offset. With HTTP range requests we read the directory
once (cached per zip), then pull just the few hundred kilobytes of the one
filing we want. Two ranged requests instead of a gigabyte.

If the server refuses ranges, this says so plainly rather than falling back to
something that silently returns nothing. A missing document must never again
be indistinguishable from an unreachable one.
"""

from __future__ import annotations

import io
import os
import re
import struct
import zlib
from pathlib import Path

import httpx

from . import USER_AGENT

ZIP_BASE = os.environ.get(
    "GRANTSIGHT_IRS_ZIP_BASE",
    "https://apps.irs.gov/pub/epostcard/990/xml",
)
# Suffixes the IRS uses when a month is split across files.
MONTH_SUFFIXES = ("A", "B", "C", "D", "E")
TIMEOUT = httpx.Timeout(60.0, connect=20.0)


class ZipSourceError(RuntimeError):
    """The zip could not be read. The message says why, and it is shown."""


def _headers() -> dict:
    contact = os.environ.get("GRANTSIGHT_CONTACT")
    ua = f"{USER_AGENT} contact:{contact}" if contact else USER_AGENT
    return {"User-Agent": ua}


def archives_for_batch(batch_id: str | None) -> list[str]:
    """Every archive that could hold a filing, from its XML_BATCH_ID.

    XML_BATCH_ID looks like "2026_TEOS_XML_05A". The year and the two-digit
    month in it are reliable. **The trailing letter is not.**

    Measured on 20 Sep 2026. The IRS index gives Planned Parenthood's FY2025
    return (object 202641349349313769) a batch of `2026_TEOS_XML_05A`, and the
    file is not in that archive. When a month is split, the archives partition
    that month's filings by object id, in order:

        05A  202601149339300000 .. 202621329349203217   (84,172 members)
        05B  202621329349203222 .. 202641419349301759   (76,704 members)

    202641349349313769 falls inside 05B. The index points at 05A regardless,
    so the letter names the month's batch, not the file within it.

    So: trust the month, enumerate the letters, take the archive that actually
    contains the member. A miss costs one cached central-directory read.
    """
    if not batch_id:
        return []
    name = batch_id.strip()
    # The index is not consistent about case: it writes "2024_TEOS_XML_04a"
    # with a lowercase suffix in places. A case-sensitive match rejected those
    # outright and silently gave up on the filing.
    match = re.fullmatch(r"(\d{4})_TEOS_XML_(\d{2})[A-Za-z]", name)
    if not match:
        return []
    year, month = match.group(1), match.group(2)

    # The index's own spelling first, in case the published file really is
    # lowercase, then the published uppercase forms for the whole month.
    names = [name]
    names += [
        candidate for suffix in MONTH_SUFFIXES
        if (candidate := f"{year}_TEOS_XML_{month}{suffix}") not in names
    ]
    return [f"{ZIP_BASE}/{year}/{n}.zip" for n in names]


# ---------------------------------------------------------------------------
# minimal ranged zip reader


def _range(client: httpx.Client, url: str, start: int, end: int) -> bytes:
    """Bytes [start, end] inclusive. Raises when the server ignores the range."""
    response = client.get(url, headers={**_headers(), "Range": f"bytes={start}-{end}"})
    if response.status_code == 200:
        # 200 means the range was ignored and the whole file is coming.
        raise ZipSourceError(
            f"{url}: server ignored Range (returned 200, not 206). "
            "Ranged reads are not supported here."
        )
    if response.status_code != 206:
        raise ZipSourceError(f"{url}: HTTP {response.status_code} on ranged read")
    return response.content


def _tail_range(client: httpx.Client, url: str, length: int) -> bytes:
    response = client.get(url, headers={**_headers(), "Range": f"bytes=-{length}"})
    if response.status_code == 200:
        raise ZipSourceError(
            f"{url}: server ignored Range (returned 200, not 206). "
            "Ranged reads are not supported here."
        )
    if response.status_code != 206:
        raise ZipSourceError(f"{url}: HTTP {response.status_code} on ranged read")
    return response.content


def _total_size(response_headers, url: str) -> int:
    cr = response_headers.get("content-range", "")
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    raise ZipSourceError(f"{url}: no Content-Range on a 206 response")


def _directory_cache_path(url: str) -> Path:
    import hashlib

    digest = hashlib.sha256(url.encode()).hexdigest()[:20]
    root = Path(os.environ.get("GRANTSIGHT_DATA", "./data")) / "zip-directories"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.cd"


def _central_directory(client: httpx.Client, url: str) -> tuple[bytes, int]:
    """The raw central directory, and the offset it starts at.

    Cached on disk per zip: a portfolio sweep asks for many filings from the
    same month, and re-reading a multi-megabyte directory for each one would
    turn a useful feature into a timeout. A 404 is cached as an empty marker
    for the same reason -- months split across A/B/C files mean most
    candidates legitimately do not exist.
    """
    cached = _directory_cache_path(url)
    if cached.exists():
        blob = cached.read_bytes()
        if not blob:
            raise ZipSourceError(f"{url}: 404 (cached)")
        return blob, 0
    probe = client.get(url, headers={**_headers(), "Range": "bytes=-65557"})
    if probe.status_code == 200:
        raise ZipSourceError(
            f"{url}: server ignored Range (returned 200, not 206). "
            "Ranged reads are not supported here."
        )
    if probe.status_code == 404:
        cached.write_bytes(b"")
        raise ZipSourceError(f"{url}: 404 (this monthly zip does not exist)")
    if probe.status_code != 206:
        raise ZipSourceError(f"{url}: HTTP {probe.status_code} reading the zip tail")
    tail = probe.content
    size = _total_size(probe.headers, url)

    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ZipSourceError(f"{url}: no end-of-central-directory record found")
    cd_size, cd_offset = struct.unpack("<II", tail[eocd + 12:eocd + 20])

    # Zip64: these files routinely exceed 4 GB, so this branch is the normal
    # one, not an edge case.
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc = tail.rfind(b"PK\x06\x07")
        if loc < 0:
            raise ZipSourceError(f"{url}: zip64 locator missing")
        z64_offset = struct.unpack("<Q", tail[loc + 8:loc + 16])[0]
        head = _range(client, url, z64_offset, z64_offset + 55)
        if head[:4] != b"PK\x06\x06":
            raise ZipSourceError(f"{url}: zip64 end-of-central-directory missing")
        cd_size, cd_offset = struct.unpack("<QQ", head[40:56])

    if cd_size <= 0 or cd_offset + cd_size > size:
        raise ZipSourceError(f"{url}: central directory bounds look wrong")
    directory = _range(client, url, cd_offset, cd_offset + cd_size - 1)
    try:
        cached.write_bytes(directory)
    except OSError:
        pass  # a full disk costs speed here, never correctness
    return directory, cd_offset


def _find_member(directory: bytes, name: str) -> tuple[int, int, int, int] | None:
    """(local_header_offset, compressed_size, method, uncompressed_size)."""
    target = name.encode()
    pos = 0
    end = len(directory)
    while pos + 46 <= end:
        if directory[pos:pos + 4] != b"PK\x01\x02":
            break
        method = struct.unpack("<H", directory[pos + 10:pos + 12])[0]
        comp_size, _uncomp = struct.unpack("<II", directory[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack(
            "<HHH", directory[pos + 28:pos + 34]
        )
        local_offset = struct.unpack("<I", directory[pos + 42:pos + 46])[0]
        entry_name = directory[pos + 46:pos + 46 + name_len]
        extra = directory[pos + 46 + name_len:pos + 46 + name_len + extra_len]

        if entry_name.endswith(target) or entry_name == target:
            # Zip64 extra field carries the real sizes and offset.
            if comp_size == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
                comp_size, local_offset = _zip64_extra(
                    extra, _uncomp, comp_size, local_offset
                )
            return local_offset, comp_size, method, _uncomp
        pos += 46 + name_len + extra_len + comment_len
    return None


def _zip64_extra(extra: bytes, uncomp: int, comp: int, offset: int):
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack("<HH", extra[pos:pos + 4])
        body = extra[pos + 4:pos + 4 + size]
        if tag == 0x0001:
            cursor = 0
            if uncomp == 0xFFFFFFFF and cursor + 8 <= len(body):
                cursor += 8
            if comp == 0xFFFFFFFF and cursor + 8 <= len(body):
                comp = struct.unpack("<Q", body[cursor:cursor + 8])[0]
                cursor += 8
            if offset == 0xFFFFFFFF and cursor + 8 <= len(body):
                offset = struct.unpack("<Q", body[cursor:cursor + 8])[0]
            break
        pos += 4 + size
    return comp, offset


def _verify(data: bytes, expected: int, url: str) -> bytes:
    """Refuse output that does not match the size the archive recorded.

    A decompressor that silently returns a short or corrupt buffer would hand
    the app a half-parsed return, and a half-parsed return is worse than no
    return: it looks like data. The central directory records the exact
    uncompressed length, so check it. 0 means the archive did not say, and
    then there is nothing to check against.
    """
    if expected and len(data) != expected:
        raise ZipSourceError(
            f"{url}: decompressed {len(data):,} bytes but the archive records "
            f"{expected:,}. The file was not decoded correctly and is not "
            "being used."
        )
    return data


def _read_member(client: httpx.Client, url: str, offset: int,
                 comp_size: int, method: int, uncomp_size: int = 0) -> bytes:
    head = _range(client, url, offset, offset + 29)
    if head[:4] != b"PK\x03\x04":
        raise ZipSourceError(f"{url}: local header missing at {offset}")
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    start = offset + 30 + name_len + extra_len
    raw = _range(client, url, start, start + comp_size - 1)
    if method == 0:
        return _verify(raw, uncomp_size, url)
    if method == 8:
        return _verify(zlib.decompress(raw, -zlib.MAX_WBITS), uncomp_size, url)
    if method == 9:
        # Deflate64. The IRS uses it for members in these archives -- Planned
        # Parenthood's FY2025 return is method 9 -- and neither zlib nor the
        # standard library's zipfile can inflate it. Without this the file is
        # located correctly and then fails at the last step, which is the
        # exact failure mode this whole module exists to stop repeating.
        try:
            import inflate64
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise ZipSourceError(
                "this filing is Deflate64-compressed and the inflate64 package "
                "is not installed (add inflate64 to requirements.txt)"
            ) from exc
        return _verify(inflate64.Inflater().inflate(raw), uncomp_size, url)
    raise ZipSourceError(f"{url}: unsupported compression method {method}")


def fetch_object(object_id: str, batch_id: str | None,
                 dest: Path | None = None) -> Path:
    """Pull one filing's XML out of its IRS archive. Raises on failure.

    `batch_id` is XML_BATCH_ID from the IRS index, which names the archive
    outright. Indexes from 2024 onward carry it; 2023 and earlier do not, and
    those filings are old enough that the per-object mirrors still serve them.
    """
    urls = archives_for_batch(batch_id)
    if not urls:
        raise ZipSourceError(
            f"no usable XML_BATCH_ID for this filing (got {batch_id!r}), so "
            "the IRS archive holding it is not named. Indexes before 2024 lack "
            "this column; those filings come from the mirrors instead."
        )
    member = f"{object_id}_public.xml"
    problems = []
    data = None

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for url in urls:
            try:
                directory, _ = _central_directory(client, url)
            except ZipSourceError as exc:
                problems.append(str(exc))
                continue
            except httpx.HTTPError as exc:
                problems.append(f"{url}: {exc}")
                continue
            found = _find_member(directory, member)
            if found is None:
                problems.append(f"{url}: {member} not in this archive")
                continue
            offset, comp_size, method, uncomp = found
            data = _read_member(client, url, offset, comp_size, method, uncomp)
            break

    if data is None:
        raise ZipSourceError("; ".join(problems) or "no archive for that batch")

    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
    return _spool(data, object_id)


def _spool(data: bytes, object_id: str) -> Path:
    target = Path(os.environ.get("GRANTSIGHT_DATA", "./data")) / "xml-cache"
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{object_id}.xml"
    path.write_bytes(data)
    return path


def probe(object_id: str, batch_id: str | None) -> dict:
    """What the zip route would do, reported rather than guessed at."""
    urls = archives_for_batch(batch_id)
    report: dict = {"batch_id": batch_id, "archives_tried": urls}
    if not urls:
        report["usable"] = False
        report["detail"] = (
            f"no usable XML_BATCH_ID in the index for this filing ({batch_id!r})"
        )
        return report
    try:
        path = fetch_object(object_id, batch_id)
        head = path.read_bytes()[:120].decode("utf-8", "replace")
        report.update(usable=True, bytes=path.stat().st_size, head=head)
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        report.update(usable=False, detail=str(exc)[:600])
    return report
