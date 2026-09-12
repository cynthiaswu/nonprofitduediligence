# Deploying on Railway

Railway runs the full application, Form 990 XML included. Three files do the
work: `Dockerfile`, `entrypoint.sh`, and `railway.json`.

One platform detail shapes the whole design, so it is worth stating first.

## Volumes mount at start, not at build

Railway attaches a volume when the container **starts**. It is not mounted
during the image build, and not during a pre-deploy command. Two consequences:

- Building the IRS index in the Dockerfile does not work. It would be written
  into an image layer and then hidden the moment the volume mounts over it.
- Building it in the start command before launching the server does not work
  either. It takes about ten minutes, and the health check would kill the
  container long before the port opened.

`entrypoint.sh` therefore binds the port immediately and builds in the
background. The app is built for that state: until the index exists, every
brief reports exemption status as unverified rather than implying good
standing, and `/healthz` returns `irs_index_built: false`. Nothing reads as
clean while the data is missing.

---

## 1. Create the service

```bash
npm i -g @railway/cli
railway login
railway init          # or: railway link, to attach to an existing project
railway up
```

Or connect the GitHub repo in the dashboard. `railway.json` pins the builder to
the Dockerfile — do not let it fall back to Nixpacks, which is deprecated and
will not honour the entrypoint.

## 2. Attach a volume

Command Palette (`⌘K`) → *New Volume*, attach it to the service, and set the
mount path to:

```
/data
```

An absolute path, matching `GRANTSIGHT_DATA` in the Dockerfile. Do not use a
relative path like `./data`; Railway puts application files in `/app`, so a
relative mount would land at `/app/data` and the env var would have to match.

Hobby-plan volumes default to 5 GB, which is comfortable. Rough sizes:

| What | Size |
| --- | --- |
| IRS bulk downloads (revocation, Pub. 78, 990-N) | ~500 MB |
| `irs.sqlite3` | ~250 MB |
| `xml_index.sqlite3` (per year indexed) | ~50 MB |
| `peers.sqlite3` | ~100 MB |
| XML response cache | grows; capped by the TTL, prune if it matters |

One volume per service. Growing past the plan default is a Pro dashboard
action.

## 3. Set variables

```
GRANTSIGHT_CONTACT=you@example.org
```

That is the only one you must set. It goes into the User-Agent and is how
ProPublica and the IRS reach you rather than blocking you. The Dockerfile
already sets `GRANTSIGHT_DATA`, `GRANTSIGHT_CACHE` and `GRANTSIGHT_XML_DIR`.

Optional:

| Variable | Effect |
| --- | --- |
| `ANTHROPIC_API_KEY` | Model-written summary paragraph; a deterministic template runs without it |
| `GRANTSIGHT_XML_INDEX_URLS` | Space-separated IRS index CSVs; enables the Form 990 XML sections |
| `GRANTSIGHT_PEERS_SOI` | Space-separated SOI extract URLs; with the next one, enables peer percentiles |
| `GRANTSIGHT_PEERS_BMF` | Space-separated EO BMF CSV URLs |
| `GRANTSIGHT_REFRESH_DAYS` | Rebuild window, default 30 |
| `GRANTSIGHT_AUTO_BUILD=0` | Disable the background build and run it by hand |
| `WEB_CONCURRENCY` | Uvicorn workers, default 2 |
| `GRANTSIGHT_NEWS=1` + `GNEWS_API_KEY` | Press mentions via GNews; off by default |
| `GRANTSIGHT_NEWS=1` + `GRANTSIGHT_NEWS_URL` | Press mentions via a generic search API template |

Do not set `PORT`. Railway injects it and the entrypoint reads it.

## 4. Watch the first boot

Deploy logs should show, in this order:

```
[grantsight] serving on :8080
[grantsight] building IRS index in background (about 10 minutes)...
[grantsight] IRS index ready.
```

The service answers requests from the first line. Confirm with `/healthz`:

```json
{"ok": true, "irs_index_built": true, "irs_index_at": "2026-09-11T04:12:07"}
```

If `irs_index_built` stays false after fifteen minutes, read the logs for a
line beginning `!` — a failed download names the URL it could not reach. The
index verifies itself and refuses to publish a partial build, which is why a
failure shows as unverified rather than as a clean bill of health.

---

## 5. Turn on the Form 990 XML

This is the reason to be on Railway rather than a static host, so it is worth
doing properly. It needs two pieces.

**The EIN index.** The IRS publishes a per-year index CSV listing every
electronic filing with its object id and batch zip, at
`apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv`. The directory
year must match the filename year (`.../2024/index_2025.csv` is a 404). Years
are *filing* years: an FY2024 return filed in 2025 is in `index_2025.csv`. Set:

```
GRANTSIGHT_XML_INDEX_URLS=https://apps.irs.gov/pub/epostcard/990/xml/2026/index_2026.csv https://apps.irs.gov/pub/epostcard/990/xml/2025/index_2025.csv https://apps.irs.gov/pub/epostcard/990/xml/2024/index_2024.csv
```

The entrypoint builds `xml_index.sqlite3` from these on first boot. Roughly
50-100 MB per year. A URL that fails is reported and skipped; the build only
fails if nothing loads.

**The filings themselves.** Two options:

*Batch-zip fetch (default; fine on a 5 GB volume).* The IRS ships filings in
yearly batch zips of 70-500 MB. Once the index exists, each brief locates its
filing's zip, reads the zip directory with HTTP range requests, then fetches
and decompresses just that one member (~100-300 KB). The directory is cached
in `xml_batches.sqlite3` and the XML in `xml-cache/`, so nothing is fetched
twice. First view of an organization is a few seconds slower; afterwards it
is cached. Set `GRANTSIGHT_XML_OBJECT_URL` (a `{object_id}` template) instead
if you mirror the corpus somewhere yourself.

*Local corpus.* If you want no per-request fetch, download the yearly zips,
extract them, and upload to the volume:

```bash
railway volume files upload ./xml-2024.tar /xml-2024.tar
railway ssh
tar -xf /xml-2024.tar -C /data/xml
```

The full corpus is far larger than a hobby volume, so this only makes sense
for a subset you care about. `GRANTSIGHT_XML_DIR` is checked before the
network, so a partial corpus just means some organizations fetch and others do
not.

With the XML working, briefs gain the program expense ratio, named officers
with titles and compensation, Schedule J detail, and Schedule I grants with
recipient EINs that link through to their own briefs.

## 6. Peer percentiles

Also variable-driven, so no terminal required. Add two more variables and
redeploy:

```
GRANTSIGHT_PEERS_SOI=https://www.irs.gov/pub/irs-soi/23eofinextract990.zip
GRANTSIGHT_PEERS_BMF=https://www.irs.gov/pub/irs-soi/eo1.csv https://www.irs.gov/pub/irs-soi/eo2.csv https://www.irs.gov/pub/irs-soi/eo3.csv https://www.irs.gov/pub/irs-soi/eo4.csv
```

Both accept several space-separated URLs. The two files each hold half of what
a percentile needs and are joined on EIN: the SOI extract has financials but no
sector or state, and the Business Master File has sector and state but no
financials. The four `eo*.csv` files are the IRS regional splits of the BMF —
load all four for national coverage, or just the regions you fund in.

Check the logs for:

```
[grantsight] building peer percentile index...
[grantsight] peer index ready.
```

Until this runs the peer section simply does not render. It never estimates
from a partial population, so a failed build shows as an absent section rather
than a wrong number.

To rebuild later — a newer SOI year, or more BMF regions — delete
`peers.sqlite3` from the volume first, since the entrypoint skips the build
when the file already exists.

---

## Keeping it current

The IRS republishes monthly. The entrypoint rebuilds automatically when
`irs.sqlite3` is older than `GRANTSIGHT_REFRESH_DAYS`, so a redeploy after that
window refreshes it. If the service runs for months without a redeploy, either
restart it monthly or run the build over SSH.

Either way it is visible rather than silent: past 40 days, every brief carries
a finding that the IRS data is stale and states its age.

## Things that will bite you

**Nixpacks instead of the Dockerfile.** `railway.json` pins
`"builder": "DOCKERFILE"`. If the build log mentions Nixpacks, the entrypoint
is not running and neither is the background build.

**A relative volume mount path.** `./data` resolves to `/app/data`, not
`/data`, and `GRANTSIGHT_DATA` points at `/data`. The index would build into
the container's ephemeral filesystem and vanish on the next deploy — and the
symptom is subtle, because the app works fine until it restarts.

**Redeploying during a build.** The build is not transactional across restarts;
it writes to a `.building` file and renames on success, so an interrupted build
leaves the previous index intact. But it will start over.

**Assuming the health check means ready.** `/healthz` returns 200 as soon as
the port is open, which is deliberate — it is a liveness check, not a readiness
one. Read `irs_index_built` to know whether status checks are live.

## Before pointing real users at it

- Look up a known-revoked EIN. The verdict should be red and say revoked.
- Look up a small 990-N filer. It should explain why there are no financials
  rather than failing.
- Check a brief shows the officers table. If not, the XML index did not build.
- Confirm the ProPublica attribution is in the footer. Their terms are
  non-commercial with attribution; a free tool from a funder is within that,
  but contact them before adding a paywall or advertising.
