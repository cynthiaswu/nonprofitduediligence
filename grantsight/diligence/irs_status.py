"""Exemption status: is this organization actually still a 501(c)(3) today?

This is the question ProPublica's 990 data cannot answer and the one that most
matters before disbursing. Three IRS bulk files, pipe-delimited ASCII inside
zips, refreshed monthly:

  Auto-Revocation List   every org that lost exemption for three consecutive
                         years of non-filing, plus reinstatement dates
  Pub. 78 Data           orgs eligible to receive deductible contributions
  Form 990-N             e-Postcard filers, the small orgs ProPublica omits

Field order for the revocation file, per the IRS data dictionary:

  EIN | Legal Name | DBA Name | Address | City | State | ZIP | Country |
  Exemption Type | Revocation Date | Revocation Posting Date |
  Exemption Reinstatement Date

The files are loaded into a local SQLite index. That index is a static public
reference refreshed on a schedule. It holds no user data, and nothing anyone
looks up is ever written to it.

    python -m diligence.irs_status --build      # download and index
    python -m diligence.irs_status --verify     # sanity-check the result
    python -m diligence.irs_status --check EIN  # look one up

Design rule running through the whole module: every failure path resolves to
UNKNOWN, never to CLEAR. A missing file, an unparseable date, a stale index,
and a schema change all produce "could not verify" rather than a brief that
implies an organization is in good standing.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .http import FetchError, get_bytes
from .propublica import clean_ein

DATA_DIR = Path(os.environ.get("GRANTSIGHT_DATA", "./data"))
DB_PATH = DATA_DIR / "irs.sqlite3"
SCHEMA_VERSION = 2

# Verified against the IRS TEOS bulk-download page and long-standing public use.
# If the IRS moves them, --build falls back to scraping the landing page.
SOURCES = {
    "revocation": os.environ.get(
        "IRS_REVOCATION_URL",
        "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip",
    ),
    "pub78": os.environ.get(
        "IRS_PUB78_URL",
        "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip",
    ),
    "epostcard": os.environ.get(
        "IRS_EPOSTCARD_URL",
        "https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip",
    ),
}

LANDING_PAGE = (
    "https://www.irs.gov/charities-non-profits/"
    "tax-exempt-organization-search-bulk-data-downloads"
)

# Refreshed monthly by the IRS. Past this, warn rather than trust silently.
STALE_AFTER_DAYS = int(os.environ.get("GRANTSIGHT_STALE_DAYS", "40"))

UNKNOWN = "unknown"
CLEAR = "clear"
REVOKED = "revoked"
REINSTATED = "reinstated"

# Deductibility codes from Pub. 78. PC is the common case for public charities.
DEDUCTIBILITY = {
    "PC": "public charity",
    "POF": "private operating foundation",
    "PF": "private foundation",
    "SO": "supporting organization",
    "SOUNK": "supporting organization, type undetermined",
    "LODGE": "domestic fraternal society",
    "EO": "exempt organization",
    "FORGN": "foreign organization",
    "GROUP": "subordinate in a group ruling",
}


# ---------------------------------------------------------------------------
# date parsing
# ---------------------------------------------------------------------------
# Sources disagree on the revocation date format: the IRS data dictionary
# documents MM/DD/YYYY, while a widely used community loader extracts the year
# with substr(8, 11), which only works on an 11-character date like
# "15-May-2024". Both have probably shipped at different times. Rather than
# pick one and risk every date silently failing to parse -- which would make
# revoked organizations read as clean -- accept all plausible layouts and
# report at build time which one the file actually used.

_DATE_FORMATS = (
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%d-%b-%Y", "DD-Mon-YYYY"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%m/%d/%y", "MM/DD/YY"),
    ("%d-%B-%Y", "DD-Month-YYYY"),
    ("%b %d, %Y", "Mon DD, YYYY"),
    ("%Y%m%d", "YYYYMMDD"),
)


def parse_date(value: str | None) -> date | None:
    """Parse an IRS date in any layout the files have been seen to use."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt, _label in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def date_layout(value: str | None) -> str | None:
    """Which named layout matched, for the build report."""
    if not value or not str(value).strip():
        return None
    text = str(value).strip()
    for fmt, label in _DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return label
        except ValueError:
            continue
    return "UNRECOGNIZED"


# ---------------------------------------------------------------------------
# lookup
# ---------------------------------------------------------------------------
@dataclass
class ExemptStatus:
    state: str = UNKNOWN
    revocation_date: str | None = None
    posting_date: str | None = None
    reinstatement_date: str | None = None
    pub78_deductibility: str | None = None
    in_pub78: bool | None = None
    epostcard_years: list[int] = field(default_factory=list)
    index_built_at: str | None = None
    index_age_days: int | None = None
    stale: bool = False
    detail: str = (
        "IRS status index not built. Run `python -m diligence.irs_status --build`."
    )

    @property
    def is_known(self) -> bool:
        return self.state != UNKNOWN

    @property
    def deductibility_label(self) -> str | None:
        if not self.pub78_deductibility:
            return None
        code = self.pub78_deductibility.strip().upper()
        return DEDUCTIBILITY.get(code, code)


def lookup_name(ein: str) -> str | None:
    """Find an organization's legal name in the IRS files alone.

    Needed for 990-N filers: ProPublica does not index them, so this is the
    only name source when the API returns nothing.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        ein = clean_ein(ein)
        for table in ("pub78", "revocation"):
            row = conn.execute(
                f"SELECT name FROM {table} WHERE ein = ?", (ein,)  # noqa: S608
            ).fetchone()
            if row and row["name"]:
                return row["name"].strip()
        return None
    finally:
        conn.close()


def known_to_irs(ein: str) -> bool:
    """Does any IRS file mention this EIN at all?"""
    status = check(ein)
    return bool(
        status.epostcard_years
        or status.in_pub78
        or status.state in (REVOKED, REINSTATED)
    )


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        version = conn.execute("SELECT schema_version FROM meta").fetchone()
        if not version or version["schema_version"] != SCHEMA_VERSION:
            conn.close()
            return None
        return conn
    except sqlite3.Error:
        return None


def check(ein: str) -> ExemptStatus:
    """Look one EIN up across all three files. Never guesses CLEAR."""
    ein = clean_ein(ein)
    conn = _connect()
    if conn is None:
        return ExemptStatus()

    try:
        meta = conn.execute("SELECT built_at, revocation_rows FROM meta").fetchone()

        # An empty revocation table means the download failed during --build.
        # Reporting CLEAR here would be the single worst bug in this codebase.
        if not meta or not meta["revocation_rows"]:
            return ExemptStatus(
                detail=(
                    "The Auto-Revocation List is empty in the local index, so "
                    "exemption status could not be verified. Rebuild the index."
                )
            )

        built_at = meta["built_at"]
        age = None
        stale = False
        built = parse_date(built_at[:10]) if built_at else None
        if built:
            age = (date.today() - built).days
            stale = age > STALE_AFTER_DAYS

        status = ExemptStatus(
            state=CLEAR,
            index_built_at=built_at,
            index_age_days=age,
            stale=stale,
            detail="Not on the IRS Auto-Revocation List.",
        )

        row = conn.execute("SELECT * FROM revocation WHERE ein = ?", (ein,)).fetchone()
        if row:
            status.revocation_date = row["revocation_date"] or None
            status.posting_date = row["posting_date"] or None
            status.reinstatement_date = row["reinstatement_date"] or None
            revoked_on = parse_date(row["revocation_date"])
            reinstated_on = parse_date(row["reinstatement_date"])

            if row["revocation_date"] and revoked_on is None:
                # Present but unparseable: schema drift. Say so; do not clear.
                status.state = UNKNOWN
                status.detail = (
                    f"This EIN appears on the Auto-Revocation List, but the "
                    f"date {row['revocation_date']!r} could not be parsed. "
                    f"Treat status as unverified and check the IRS lookup."
                )
            elif reinstated_on and (not revoked_on or reinstated_on >= revoked_on):
                status.state = REINSTATED
                status.detail = (
                    f"Exemption was auto-revoked effective "
                    f"{status.revocation_date} and reinstated "
                    f"{status.reinstatement_date}."
                )
            else:
                status.state = REVOKED
                status.detail = (
                    f"Exemption auto-revoked effective {status.revocation_date} "
                    f"with no reinstatement date on file."
                )

        pub78 = conn.execute(
            "SELECT deductibility FROM pub78 WHERE ein = ?", (ein,)
        ).fetchone()
        status.in_pub78 = pub78 is not None
        if pub78:
            status.pub78_deductibility = pub78["deductibility"]

        years = conn.execute(
            "SELECT DISTINCT tax_year FROM epostcard WHERE ein = ? "
            "AND tax_year IS NOT NULL ORDER BY tax_year DESC",
            (ein,),
        ).fetchall()
        status.epostcard_years = [int(r["tax_year"]) for r in years]

        if stale and status.state != UNKNOWN:
            status.detail += (
                f" Index is {age} days old; the IRS refreshes these files "
                f"monthly, so a recent change may not be reflected."
            )
        return status
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def discover_urls() -> dict[str, str]:
    """Scrape the IRS landing page for zip links, if the direct URLs moved."""
    import httpx

    from . import USER_AGENT

    found: dict[str, str] = {}
    try:
        response = httpx.get(
            LANDING_PAGE,
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! could not read {LANDING_PAGE}: {exc}", file=sys.stderr)
        return found

    for href in re.findall(r'href="([^"]+\.zip)"', response.text, re.I):
        url = href if href.startswith("http") else "https://www.irs.gov" + href
        low = url.lower()
        if "revocation" in low:
            found.setdefault("revocation", url)
        elif "pub78" in low or "pub-78" in low:
            found.setdefault("pub78", url)
        elif "epostcard" in low or "990n" in low:
            found.setdefault("epostcard", url)
    return found


def _members(path: Path):
    """Yield (name, text-stream) for each delimited member of a zip or file."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".txt", ".csv", ".dat")):
                    with archive.open(name) as handle:
                        yield name, io.TextIOWrapper(
                            handle, encoding="latin-1", errors="replace"
                        )
    else:
        with path.open(encoding="latin-1", errors="replace") as handle:
            yield path.name, handle


def _rows(path: Path):
    # IRS pipe files are unquoted; a `"` inside a name is literal, and the
    # default quoting would swallow every following row into one field.
    for _name, stream in _members(path):
        for row in csv.reader(stream, delimiter="|", quoting=csv.QUOTE_NONE):
            if row:
                yield row


def _norm_ein(value: str) -> str | None:
    digits = "".join(c for c in (value or "") if c.isdigit())
    return digits.zfill(9) if 7 <= len(digits) <= 9 else None


def _fetch(key: str, url: str, local: Path | None) -> Path | None:
    if local:
        return local
    try:
        return get_bytes(url, DATA_DIR / f"{key}.zip", ttl_s=60 * 60 * 24 * 25)
    except FetchError as exc:
        print(f"  ! {key}: {exc}", file=sys.stderr)
        return None


def build_index(local: dict[str, Path] | None = None) -> dict:
    """Download and index all three files. Returns a report for --verify."""
    local = local or {}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_db = DB_PATH.with_suffix(".building")
    tmp_db.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE revocation (
            ein TEXT PRIMARY KEY, name TEXT, subsection TEXT,
            revocation_date TEXT, posting_date TEXT, reinstatement_date TEXT
        );
        CREATE TABLE pub78 (ein TEXT PRIMARY KEY, name TEXT, deductibility TEXT);
        CREATE TABLE epostcard (ein TEXT, tax_year INTEGER);
        CREATE TABLE meta (
            schema_version INTEGER, built_at TEXT, revocation_rows INTEGER,
            pub78_rows INTEGER, epostcard_rows INTEGER, date_layout TEXT,
            notes TEXT
        );
        """
    )

    report: dict = {"counts": {}, "layouts": {}, "samples": {}, "problems": []}
    discovered: dict[str, str] | None = None

    for key, url in SOURCES.items():
        path = _fetch(key, url, local.get(key))
        if path is None and key not in local:
            if discovered is None:
                print("  direct URL failed; checking the IRS landing page...")
                discovered = discover_urls()
            if key in discovered:
                print(f"  found {key} at {discovered[key]}")
                path = _fetch(key, discovered[key], None)
        if path is None:
            report["counts"][key] = 0
            report["problems"].append(f"{key}: download failed")
            continue
        report["counts"][key] = _load(conn, key, path, report)
        print(f"  {key}: {report['counts'][key]:,} rows")

    conn.execute("CREATE INDEX epostcard_ein ON epostcard (ein)")
    conn.execute(
        "INSERT INTO meta VALUES (?,?,?,?,?,?,?)",
        (
            SCHEMA_VERSION,
            datetime.now().isoformat(timespec="seconds"),
            report["counts"].get("revocation", 0),
            report["counts"].get("pub78", 0),
            report["counts"].get("epostcard", 0),
            report["layouts"].get("revocation_date", ""),
            "; ".join(report["problems"]),
        ),
    )
    conn.commit()
    conn.close()

    if report["counts"].get("revocation", 0) == 0:
        report["problems"].append(
            "revocation table is empty; every status check will report UNKNOWN"
        )
    tmp_db.replace(DB_PATH)
    report["db"] = str(DB_PATH)
    return report


def _load(conn: sqlite3.Connection, key: str, path: Path, report: dict) -> int:
    loaded = 0
    batch: list[tuple] = []
    layouts: Counter = Counter()
    samples: list[str] = []

    if key == "revocation":
        sql = "INSERT OR REPLACE INTO revocation VALUES (?,?,?,?,?,?)"
        for row in _rows(path):
            ein = _norm_ein(row[0]) if row else None
            if not ein or len(row) < 11:
                continue
            revocation_date = row[9].strip()
            posting_date = row[10].strip()
            reinstatement = row[11].strip() if len(row) > 11 else ""
            if loaded < 200_000:  # layout is uniform; no need to scan every row
                layouts[date_layout(revocation_date) or "BLANK"] += 1
            if len(samples) < 5 and revocation_date:
                samples.append(revocation_date)
            batch.append((
                ein, row[1].strip(), row[8].strip(),
                revocation_date, posting_date, reinstatement,
            ))
            loaded += 1
            if len(batch) >= 10_000:
                conn.executemany(sql, batch)
                batch.clear()
        report["layouts"]["revocation_date"] = (
            layouts.most_common(1)[0][0] if layouts else "NONE"
        )
        report["layouts"]["revocation_date_mix"] = dict(layouts)
        report["samples"]["revocation_date"] = samples

    elif key == "pub78":
        # EIN | Legal Name | City | State | Country | Deductibility Code
        sql = "INSERT OR REPLACE INTO pub78 VALUES (?,?,?)"
        for row in _rows(path):
            ein = _norm_ein(row[0]) if row else None
            if not ein or len(row) < 6:
                continue
            batch.append((ein, row[1].strip(), row[5].strip()))
            loaded += 1
            if len(batch) >= 10_000:
                conn.executemany(sql, batch)
                batch.clear()

    else:
        # e-Postcard layout has varied across releases; rely only on the EIN
        # and the first plausible four-digit year in the row.
        sql = "INSERT INTO epostcard VALUES (?,?)"
        for row in _rows(path):
            ein = _norm_ein(row[0]) if row else None
            if not ein or len(row) < 2:
                continue
            year = None
            for cell in row[1:6]:
                text = (cell or "").strip()[:4]
                if text.isdigit() and 1990 <= int(text) <= 2100:
                    year = int(text)
                    break
            batch.append((ein, year))
            loaded += 1
            if len(batch) >= 10_000:
                conn.executemany(sql, batch)
                batch.clear()

    if batch:
        conn.executemany(sql, batch)
    conn.commit()
    return loaded


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def verify() -> tuple[bool, list[str]]:
    """Sanity-check a built index. Returns (ok, messages)."""
    messages: list[str] = []
    conn = _connect()
    if conn is None:
        return False, [f"No usable index at {DB_PATH}. Run --build."]

    ok = True
    try:
        meta = conn.execute("SELECT * FROM meta").fetchone()
        rev_rows = meta["revocation_rows"]
        messages.append(f"built {meta['built_at']}")
        messages.append(f"revocation rows: {rev_rows:,}")
        messages.append(f"pub78 rows: {meta['pub78_rows']:,}")
        messages.append(f"e-Postcard rows: {meta['epostcard_rows']:,}")
        messages.append(f"revocation date layout: {meta['date_layout']}")

        # The list has held roughly a million cumulative revocations since 2010.
        if rev_rows < 500_000:
            ok = False
            messages.append(
                f"FAIL: only {rev_rows:,} revocation rows. The full list has "
                f"held on the order of a million records since 2010, so this "
                f"file is truncated or partly unparsed."
            )

        if meta["date_layout"] in ("UNRECOGNIZED", "NONE", "BLANK", ""):
            ok = False
            messages.append(
                "FAIL: revocation dates did not match any known layout. Every "
                "lookup would report UNKNOWN. Add the real format to "
                "_DATE_FORMATS."
            )

        # Dates must parse and land in a plausible range: the programme began
        # in 2010 and nothing should be dated in the future.
        sample = conn.execute(
            "SELECT revocation_date FROM revocation WHERE revocation_date != '' "
            "LIMIT 2000"
        ).fetchall()
        parsed = [parse_date(r["revocation_date"]) for r in sample]
        good = [d for d in parsed if d]
        if sample and len(good) / len(sample) < 0.95:
            ok = False
            messages.append(
                f"FAIL: only {len(good)}/{len(sample)} sampled dates parsed."
            )
        elif good:
            lo, hi = min(good), max(good)
            messages.append(
                f"revocation dates span {lo.isoformat()} to {hi.isoformat()}"
            )
            if lo.year < 2009 or hi > date.today():
                ok = False
                messages.append("FAIL: date range is implausible.")

        reinstated = conn.execute(
            "SELECT COUNT(*) c FROM revocation WHERE reinstatement_date != ''"
        ).fetchone()["c"]
        messages.append(f"reinstated: {reinstated:,}")
        if rev_rows and reinstated == 0:
            ok = False
            messages.append(
                "FAIL: no reinstatement dates at all, which suggests the "
                "trailing column is not being read."
            )

        # A live round trip: pick a real revoked EIN out of the index and make
        # sure check() actually returns REVOKED for it.
        probe = conn.execute(
            "SELECT ein FROM revocation WHERE reinstatement_date = '' "
            "AND revocation_date != '' LIMIT 1"
        ).fetchone()
        if probe:
            result = check(probe["ein"])
            messages.append(f"probe {probe['ein']}: {result.state}")
            if result.state != REVOKED:
                ok = False
                messages.append(f"FAIL: a known revoked EIN returned {result.state}.")

        probe2 = conn.execute(
            "SELECT ein FROM revocation WHERE reinstatement_date != '' LIMIT 1"
        ).fetchone()
        if probe2:
            result = check(probe2["ein"])
            messages.append(f"probe {probe2['ein']}: {result.state}")
            if result.state != REINSTATED:
                ok = False
                messages.append(
                    f"FAIL: a known reinstated EIN returned {result.state}."
                )

        # An EIN that cannot exist must come back clean, not error.
        control = check("000000000")
        if control.state != CLEAR:
            ok = False
            messages.append(f"FAIL: control EIN returned {control.state}.")

        built = parse_date(meta["built_at"][:10])
        if built:
            age = (date.today() - built).days
            if age > STALE_AFTER_DAYS:
                messages.append(
                    f"WARN: index is {age} days old. The IRS refreshes monthly."
                )
        if meta["notes"]:
            messages.append(f"build notes: {meta['notes']}")
    finally:
        conn.close()
    return ok, messages


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(
        description="Build and verify the IRS exemption status index."
    )
    parser.add_argument("--build", action="store_true", help="download and index")
    parser.add_argument("--verify", action="store_true", help="sanity-check the index")
    parser.add_argument("--check", metavar="EIN", help="look up one EIN")
    parser.add_argument("--revocation", type=Path, help="use a local file")
    parser.add_argument("--pub78", type=Path)
    parser.add_argument("--epostcard", type=Path)
    args = parser.parse_args()

    if args.check:
        status = check(args.check)
        print(f"{clean_ein(args.check)}: {status.state.upper()}")
        print(f"  {status.detail}")
        if status.in_pub78 is not None:
            print(f"  Pub. 78: {status.deductibility_label or 'not listed'}")
        if status.epostcard_years:
            print(f"  990-N filings: {status.epostcard_years[:6]}")
        raise SystemExit(0 if status.is_known else 1)

    if args.build:
        local = {
            k: v
            for k, v in (
                ("revocation", args.revocation),
                ("pub78", args.pub78),
                ("epostcard", args.epostcard),
            )
            if v
        }
        print("Building IRS status index...")
        report = build_index(local)
        if report["samples"].get("revocation_date"):
            print(f"  sample dates: {report['samples']['revocation_date']}")
            print(f"  parsed as: {report['layouts'].get('revocation_date')}")
        for problem in report["problems"]:
            print(f"  ! {problem}", file=sys.stderr)

    if args.build or args.verify:
        ok, messages = verify()
        print("\nVerification:")
        for line in messages:
            print(f"  {line}")
        print("\n" + ("PASS — status checks are live." if ok else "FAIL — see above."))
        raise SystemExit(0 if ok else 1)

    parser.print_help()


if __name__ == "__main__":  # pragma: no cover
    main()
