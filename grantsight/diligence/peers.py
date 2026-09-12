"""Peer comparison from a local population, not a sample.

The previous version fetched a dozen organizations from one page of search
results and called the result a percentile. It wasn't one, and it had no tests.

This computes a true percentile against every organization in the sector and
state, by joining two public files that each hold half of what is needed:

  SOI annual extract   EIN and financials, no name, no sector, no state
  EO Business Master   EIN, NTEE code, state, no financials
  File (BMF)

Joined on EIN into a local table, the percentile is then a COUNT over the whole
population rather than an estimate.

    python -m diligence.peers --build --soi 23eofinextract990.dat --bmf eo1.csv

Both arguments accept a local path or a URL, and repeat, so several years or
all four BMF regions can be loaded in one pass. Without a built table, peer
comparison is reported as unavailable rather than approximated.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .http import FetchError, get_bytes

DATA_DIR = Path(os.environ.get("GRANTSIGHT_DATA", "./data"))
DB_PATH = DATA_DIR / "peers.sqlite3"
MIN_POPULATION = int(os.environ.get("GRANTSIGHT_MIN_PEERS", "30"))
# A real join of one SOI year against the full BMF lands in the hundreds of
# thousands. Anything under this means the files did not overlap properly.
MIN_POPULATION_FOR_VERIFY = int(os.environ.get("GRANTSIGHT_MIN_VERIFY", "1000"))

NTEE_GROUP_NAMES = {
    "A": "arts and culture", "B": "education", "C": "environment",
    "D": "animal welfare", "E": "health care", "F": "mental health",
    "G": "disease and disorders", "H": "medical research",
    "I": "crime and legal", "J": "employment", "K": "food and agriculture",
    "L": "housing and shelter", "M": "public safety and disaster",
    "N": "recreation and sports", "O": "youth development",
    "P": "human services", "Q": "international affairs",
    "R": "civil rights", "S": "community improvement",
    "T": "philanthropy and grantmaking", "U": "science and technology",
    "V": "social science", "W": "public and societal benefit",
    "X": "religion", "Y": "mutual benefit", "Z": "unclassified",
}

# Revenue element names differ by form; see normalize.FIELD_MAP for the source.
REVENUE_KEYS = ("totrevenue", "totrevnue", "totrcptperbks")


@dataclass
class PeerContext:
    sector: str
    sector_name: str
    state: str | None
    population: int
    percentile: int
    median_revenue: float | None
    p25: float | None
    p75: float | None
    scope: str
    built_at: str | None

    @property
    def usable(self) -> bool:
        return self.population >= MIN_POPULATION

    def describe(self) -> str:
        where = f"in {self.state}" if self.state else "nationally"
        return (
            f"Larger than {self.percentile}% of the {self.population:,} "
            f"{self.sector_name} organizations {where} with financial data on "
            f"record."
        )


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM peer_orgs LIMIT 1")
        return conn
    except sqlite3.Error:
        return None


def _quantile(conn, where: str, params: tuple, fraction: float) -> float | None:
    row = conn.execute(
        f"SELECT revenue FROM peer_orgs WHERE {where} ORDER BY revenue "  # noqa: S608
        f"LIMIT 1 OFFSET CAST((SELECT COUNT(*) FROM peer_orgs WHERE {where}) * ? AS INT)",
        params + params + (fraction,),
    ).fetchone()
    return row["revenue"] if row else None


def compare(organization: dict, revenue: float | None) -> PeerContext | None:
    """Place `revenue` against the full sector-and-state population.

    Falls back to a national comparison when the state population is too thin
    to be meaningful, and returns None when even that is too thin.
    """
    ntee = (organization.get("ntee_code") or "").strip()
    state = (organization.get("state") or "").strip().upper()
    if not ntee or revenue is None:
        return None

    conn = _connect()
    if conn is None:
        return None

    try:
        group = ntee[0].upper()
        built = conn.execute("SELECT built_at FROM peer_meta LIMIT 1").fetchone()
        built_at = built["built_at"] if built else None

        for where, params, scope, shown_state in (
            ("ntee_major = ? AND state = ?", (group, state), "sector and state", state),
            ("ntee_major = ?", (group,), "sector, nationally", None),
        ):
            total = conn.execute(
                f"SELECT COUNT(*) c FROM peer_orgs WHERE {where}", params  # noqa: S608
            ).fetchone()["c"]
            if total < MIN_POPULATION:
                continue

            below = conn.execute(
                f"SELECT COUNT(*) c FROM peer_orgs WHERE {where} AND revenue < ?",  # noqa: S608
                params + (revenue,),
            ).fetchone()["c"]

            return PeerContext(
                sector=group,
                sector_name=NTEE_GROUP_NAMES.get(group, "comparable"),
                state=shown_state,
                population=total,
                percentile=round(100 * below / total),
                median_revenue=_quantile(conn, where, params, 0.5),
                p25=_quantile(conn, where, params, 0.25),
                p75=_quantile(conn, where, params, 0.75),
                scope=scope,
                built_at=built_at,
            )
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def _text_members(path: Path):
    """Yield text streams for a zip's data members, or the file itself.

    IRS file naming is inconsistent -- the SOI extracts have shipped as .dat,
    .dat.dat and with no extension at all -- so an extension filter that finds
    nothing falls back to every member rather than silently loading zero rows.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            wanted = [n for n in names if n.lower().endswith((".dat", ".csv", ".txt"))]
            if not wanted and names:
                print(f"  note: no .dat/.csv/.txt member in {path.name}; "
                      f"reading {names}", file=sys.stderr)
                wanted = names
            for name in wanted:
                with archive.open(name) as handle:
                    yield io.TextIOWrapper(handle, encoding="latin-1", errors="replace")
    else:
        with path.open(encoding="latin-1", errors="replace") as handle:
            yield handle


def _resolve_source(source: str, label: str) -> Path | None:
    if source.startswith("http"):
        try:
            path = get_bytes(source, DATA_DIR / f"{label}-{abs(hash(source)) % 99999}.bin")
        except FetchError as exc:
            print(f"\n  !! DOWNLOAD FAILED: {source}\n     {exc}\n"
                  f"     Check the URL opens in a browser. IRS filenames change "
                  f"between years.\n", file=sys.stderr)
            return None
        # A 404 page served as HTML is still a successful HTTP fetch.
        head = path.open("rb").read(200).lstrip().lower()
        if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            print(f"\n  !! {source}\n     returned an HTML page, not data. The URL "
                  f"is probably wrong.\n", file=sys.stderr)
            return None
        return path
    if not source.strip():
        print(f"  ! empty --{label} argument", file=sys.stderr)
        return None
    path = Path(source)
    if not path.is_file():
        what = "is a directory, not a file" if path.is_dir() else "does not exist"
        print(f"  ! {path} {what}", file=sys.stderr)
        return None
    return path


# Whitespace is a real choice, so it needs its own value: returning None for
# "split on whitespace" collided with None meaning "no delimiter found", and
# the space-delimited path silently reported failure on a header it had
# actually parsed correctly.
WHITESPACE = " "


def _split(line: str, delimiter: str) -> list[str]:
    if delimiter == WHITESPACE:
        return line.split()
    return [cell.strip().strip('"') for cell in line.split(delimiter)]


def _sniff_delimiter(header: str) -> str | None:
    """SOI extracts changed format: pre-2018 files are space-delimited ASCII,
    later ones are CSV. Pick whichever splitter yields a usable header rather
    than assuming, because guessing wrong parses every row to nothing.

    Returns the delimiter, or None when no candidate produces a usable header.
    """
    for delimiter in (",", "\t", WHITESPACE):
        names = [cell.lower() for cell in _split(header, delimiter)]
        if "ein" in names and any(key in names for key in REVENUE_KEYS):
            return delimiter
    return None


DELIMITER_NAMES = {",": "comma", "\t": "tab", WHITESPACE: "space"}


def _load_soi(conn: sqlite3.Connection, path: Path) -> int:
    """Load EIN and total revenue, whatever delimiter the year happens to use."""
    loaded = 0
    for stream in _text_members(path):
        header = stream.readline().strip()
        delimiter = _sniff_delimiter(header)
        if delimiter is None:
            preview = header[:200]
            print(f"\n  !! {path.name}: could not find EIN and a revenue column "
                  f"in the header.\n     Header begins: {preview}\n"
                  f"     Expected one of {REVENUE_KEYS}.\n", file=sys.stderr)
            continue

        names = _split(header, delimiter)
        lower = {n.lower(): i for i, n in enumerate(names)}
        ein_idx = lower.get("ein")
        rev_idx = next((lower[k] for k in REVENUE_KEYS if k in lower), None)
        year_idx = lower.get("tax_pd") or lower.get("tax_prd")
        print(f"  {path.name}: {len(names)} columns, "
              f"{DELIMITER_NAMES[delimiter]}-delimited")

        batch = []
        for line in stream:
            cells = _split(line, delimiter)
            if len(cells) <= max(ein_idx, rev_idx):
                continue
            ein = "".join(c for c in cells[ein_idx] if c.isdigit()).zfill(9)
            if len(ein) != 9:
                continue
            try:
                revenue = float(cells[rev_idx])
            except ValueError:
                continue
            year = None
            if year_idx is not None and len(cells) > year_idx:
                raw = "".join(c for c in cells[year_idx] if c.isdigit())
                if len(raw) >= 4:
                    year = int(raw[:4])
            batch.append((ein, revenue, year))
            loaded += 1
            if len(batch) >= 10_000:
                conn.executemany("INSERT OR REPLACE INTO soi VALUES (?,?,?)", batch)
                batch.clear()
        if batch:
            conn.executemany("INSERT OR REPLACE INTO soi VALUES (?,?,?)", batch)
    conn.commit()
    return loaded


def _load_bmf(conn: sqlite3.Connection, path: Path) -> int:
    """BMF regional extracts are CSV with a header naming EIN, STATE, NTEE_CD."""
    loaded = 0
    for stream in _text_members(path):
        reader = csv.reader(stream)
        try:
            header = [h.strip().upper() for h in next(reader)]
        except StopIteration:
            continue
        try:
            ein_idx = header.index("EIN")
        except ValueError:
            print(f"  ! no EIN column in {path.name}", file=sys.stderr)
            continue
        state_idx = header.index("STATE") if "STATE" in header else None
        ntee_idx = next(
            (header.index(k) for k in ("NTEE_CD", "NTEECD", "NTEE") if k in header), None
        )
        if ntee_idx is None:
            print(f"  ! no NTEE column in {path.name}", file=sys.stderr)
            continue

        batch = []
        for row in reader:
            if len(row) <= max(ein_idx, ntee_idx):
                continue
            ein = "".join(c for c in row[ein_idx] if c.isdigit()).zfill(9)
            ntee = (row[ntee_idx] or "").strip().upper()
            if len(ein) != 9 or not ntee:
                continue
            state = (row[state_idx].strip().upper() if state_idx is not None
                     and len(row) > state_idx else "")
            batch.append((ein, ntee[0], state))
            loaded += 1
            if len(batch) >= 10_000:
                conn.executemany("INSERT OR REPLACE INTO bmf VALUES (?,?,?)", batch)
                batch.clear()
        if batch:
            conn.executemany("INSERT OR REPLACE INTO bmf VALUES (?,?,?)", batch)
    conn.commit()
    return loaded


def build_index(soi_sources: list[str], bmf_sources: list[str]) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".building")
    tmp.unlink(missing_ok=True)

    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(
        """
        CREATE TABLE soi (ein TEXT PRIMARY KEY, revenue REAL, fiscal_year INTEGER);
        CREATE TABLE bmf (ein TEXT PRIMARY KEY, ntee_major TEXT, state TEXT);
        CREATE TABLE peer_orgs (
            ein TEXT PRIMARY KEY, ntee_major TEXT, state TEXT, revenue REAL
        );
        CREATE TABLE peer_meta (built_at TEXT, population INTEGER, notes TEXT);
        """
    )

    report = {"soi": 0, "bmf": 0, "joined": 0, "problems": []}
    for source in soi_sources:
        path = _resolve_source(source, "soi")
        if path:
            count = _load_soi(conn, path)
            report["soi"] += count
            print(f"  SOI {Path(source).name}: {count:,} rows")
            if count == 0:
                report["problems"].append(f"SOI source loaded 0 rows: {source}")
    for source in bmf_sources:
        path = _resolve_source(source, "bmf")
        if path:
            count = _load_bmf(conn, path)
            report["bmf"] += count
            print(f"  BMF {Path(source).name}: {count:,} rows")
            if count == 0:
                report["problems"].append(f"BMF source loaded 0 rows: {source}")

    conn.execute(
        """
        INSERT INTO peer_orgs
        SELECT soi.ein, bmf.ntee_major, bmf.state, soi.revenue
        FROM soi JOIN bmf ON soi.ein = bmf.ein
        WHERE soi.revenue > 0
        """
    )
    conn.execute("CREATE INDEX peer_lookup ON peer_orgs (ntee_major, state, revenue)")
    conn.execute("CREATE INDEX peer_national ON peer_orgs (ntee_major, revenue)")
    report["joined"] = conn.execute("SELECT COUNT(*) c FROM peer_orgs").fetchone()[0]

    if report["joined"] == 0:
        if report["soi"] == 0 and report["bmf"] == 0:
            cause = "neither source loaded. Both URLs failed or were unreadable."
        elif report["soi"] == 0:
            cause = ("the SOI extract loaded 0 rows, so there are no financials "
                     "to join. Check the --soi URL.")
        elif report["bmf"] == 0:
            cause = ("the BMF loaded 0 rows, so there is no sector or state to "
                     "join to. Check the --bmf URLs.")
        else:
            cause = (f"both sources loaded ({report['soi']:,} SOI, "
                     f"{report['bmf']:,} BMF) but no EINs matched between them.")
        report["problems"].append(f"join produced no rows: {cause}")
        print(f"\n  !! JOIN PRODUCED NO ROWS: {cause}", file=sys.stderr)
    conn.execute(
        "INSERT INTO peer_meta VALUES (?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), report["joined"],
         "; ".join(report["problems"])),
    )
    conn.commit()
    conn.close()
    tmp.replace(DB_PATH)
    print(f"  joined population: {report['joined']:,}")
    return report


def verify() -> tuple[bool, list[str]]:
    conn = _connect()
    if conn is None:
        return False, [f"No peer index at {DB_PATH}. Run --build."]
    try:
        messages, ok = [], True
        total = conn.execute("SELECT COUNT(*) c FROM peer_orgs").fetchone()["c"]
        messages.append(f"population: {total:,}")
        if total < MIN_POPULATION_FOR_VERIFY:
            ok = False
            messages.append("FAIL: population too small to produce percentiles.")
        groups = conn.execute(
            "SELECT ntee_major, COUNT(*) c FROM peer_orgs GROUP BY ntee_major "
            "ORDER BY c DESC LIMIT 5"
        ).fetchall()
        messages.append(
            "largest sectors: "
            + ", ".join(f"{r['ntee_major']}={r['c']:,}" for r in groups)
        )
        usable = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT ntee_major, state FROM peer_orgs "
            "GROUP BY ntee_major, state HAVING COUNT(*) >= ?)", (MIN_POPULATION,)
        ).fetchone()["c"]
        messages.append(f"sector-state cells above the {MIN_POPULATION} threshold: {usable:,}")
        if usable == 0:
            ok = False
            messages.append("FAIL: no cell is large enough; every lookup would fall back.")
        return ok, messages
    finally:
        conn.close()


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="Build the peer percentile index.")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--soi", action="append", default=[],
                        help="SOI extract path or URL (repeatable)")
    parser.add_argument("--bmf", action="append", default=[],
                        help="EO BMF path or URL (repeatable)")
    args = parser.parse_args()

    if args.build:
        if not args.soi or not args.bmf:
            parser.error("--build needs at least one --soi and one --bmf source")
        print("Building peer index...")
        build_index(args.soi, args.bmf)
    if args.build or args.verify:
        ok, messages = verify()
        print("\nVerification:")
        for line in messages:
            print(f"  {line}")
        print("\n" + ("PASS — peer percentiles are live." if ok else "FAIL — see above."))
        raise SystemExit(0 if ok else 1)
    parser.print_help()


if __name__ == "__main__":  # pragma: no cover
    main()
