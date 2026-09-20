# GrantSight — handoff brief

## What this is

A due-diligence brief generator for grantmakers. Input is a US nonprofit's name
or EIN; output is a one-page brief answering "is this organization in good
standing and financially sound enough to fund." Python 3.12, FastAPI,
two SQLite indexes, no user accounts and no stored user data.

Repo layout:

```
app.py                    three routes: /, /brief, /api/brief.json, /healthz
entrypoint.sh             binds $PORT first, builds indexes in background
Dockerfile, railway.json  container + Railway config
diligence/
  propublica.py           ProPublica Nonprofit Explorer API v2 client
  normalize.py            per-form IRS SOI field map (990 / 990-EZ / 990-PF)
  irs_status.py           Auto-Revocation List, Pub. 78, 990-N index + verify
  xml990.py               Form 990 XML: Part IX split, Part VII, Sch J, Sch I
  metrics.py              derived ratios, flags, thresholds, gap statements
  peers.py                SOI-to-BMF join, population percentiles
  profile.py              summary, highlights, website check, press matching
  narrative.py            optional LLM paragraph, constrained to computed facts
  brief.py                assembly, degrading source by source
  render.py               HTML
tests/test_pipeline.py    96 tests, fully offline
```

Docs in repo: `README.md`, `RAILWAY.md`, `PUBLISHING.md`.

## Current state

Deployed on Railway from a GitHub repo. Working: IRS status index, briefs,
findings, financials, gaps. Not yet built: the peer percentile index.

**Immediate task.** The peer build fails. Two causes found and fixed in the
latest code; the fix may not be deployed yet. Verify by running, inside the
container:

```bash
python -m diligence.peers --build \
  --soi https://www.irs.gov/pub/irs-soi/24eoextract990.zip \
  --bmf https://www.irs.gov/pub/irs-soi/eo1.csv \
  --bmf https://www.irs.gov/pub/irs-soi/eo2.csv \
  --bmf https://www.irs.gov/pub/irs-soi/eo3.csv \
  --bmf https://www.irs.gov/pub/irs-soi/eo4.csv
```

Expect `24eoextract990.zip: N columns, comma-delimited`, then a few hundred
thousand SOI rows, ~1.39M BMF rows, a joined population, and `PASS`. If SOI
loads 0 rows, print the real header and compare against `REVENUE_KEYS` in
`peers.py`.

## Verified data sources

These URLs cost several failed attempts to pin down. Do not guess variants.

| Source | URL |
| --- | --- |
| Auto-Revocation List | `https://apps.irs.gov/pub/epostcard/data-download-revocation.zip` |
| Pub. 78 | `https://apps.irs.gov/pub/epostcard/data-download-pub78.zip` |
| 990-N e-Postcard | `https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip` |
| 990 XML index (per year) | `https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv` |
| SOI extract, 2018+ | `https://www.irs.gov/pub/irs-soi/{YY}eoextract990.zip` |
| SOI extract, 2017 and earlier | `https://www.irs.gov/pub/irs-soi/{YY}eofinextract990.zip` |
| EO BMF | `https://www.irs.gov/pub/irs-soi/eo1.csv` … `eo4.csv` |

Note the SOI rename: `eofinextract` → `eoextract` from the 2018 file onward.
`{YY}eoextract990EZ.zip` and `{YY}eoextract990pf.zip` are the short-form and
foundation equivalents — capitalization is inconsistent and is as shown.

Landing pages, if a filename changes again: search "SOI Tax Stats Annual
Extract of Tax-Exempt Organization Financial Data" and "Form 990 series
downloads".

## Design invariants — do not break these

These are the point of the project. A change that violates one is a regression
even if tests pass.

1. **Unknown status never reads as clean.** Missing index, failed download,
   unparseable date, schema drift — every path resolves to `UNKNOWN`, never
   `CLEAR`. A brief implying an organization is in good standing when that was
   not verified is the worst failure this codebase can produce.
2. **An unresolved field is `None`, never `0.0`, never estimated.** A real
   zero in a filing is a zero; an absent element is `None` and renders `n/d`.
3. **"Not published in this source" and "did not resolve" are worded
   differently.** The first is permanent, the second is a fixable bug. See
   `normalize.NOT_IN_EXTRACT`.
4. **The gaps section is first-class output**, rendered at the same weight as
   findings. A brief that hides what it could not establish is worse than no
   brief.
5. **No scoring, no ranking, no funding recommendation.** Every threshold lives
   named in `metrics.THRESHOLDS` for the operator to argue with. Findings
   describe; they do not decide.
6. **Press mentions must corroborate identity** — name match plus at least one
   of city, state, EIN, or the domain on the filing; two corroborators for
   generic names. Failures are dropped silently. Press items can never raise
   severity or become findings.
7. **Every build verifies itself and exits non-zero on failure**, so a bad
   index never publishes silently.

## What is genuinely impossible (don't burn time)

- **Program/management/fundraising split from the SOI extract.** Every Part IX
  element there is column (A), the total. There is no `progsrvcexpns` field. It
  comes from the Form 990 XML only.
- **Donor identities.** Schedule B is not public. The Schedule A public support
  test (`pubsupplesspct170` / `totsupp170`) and `exceeds2pct170` are the
  available proxies for concentration and are already wired up.
- **Paper filers.** No XML exists. Those briefs fall back to the extract and
  say which sections are missing. OCR not attempted.
- **Netlify hosting of the app.** JS/TS/Go function runtimes only, no
  persistent disk. Was explored and removed from the repo.

## The filing-recency rule

One bug class cost several rounds and is pinned by tests from four directions.
Do not undo it.

**ProPublica decides whether a return was filed. The XML decides the numbers.
Neither may lower what the other established.**

ProPublica's API splits filings into `filings_with_data` (the IRS summary
extract, 12–24 months behind) and `filings_without_data` (returns it holds with
no extracted figures). Reading only the first reports a current filer as years
delinquent. `propublica.filing_years()` reads both and returns `latest_filed`
alongside `latest_with_figures`, so a brief can say "filed through FY2025,
published figures are FY2023" instead of picking one and being wrong.

The XML may raise the filing year, never lower it. A dead XML source leaves
stale figures, which is acceptable and is stated; it must not turn a FY2025
filer into a FY2023 delinquent.

Index years are **processing years**, not tax years: a FY2025 return filed in
April 2026 appears in `index_2026.csv`. Indexing the tax years you care about
misses precisely the newest filings.

## Diagnosing before changing anything

Three wrong diagnoses were made about this pipeline from the outside. Each of
these answers one layer definitively; run them before editing:

```bash
python -m diligence.propublica 52-1693387        # which years, which list
python -m diligence.xml990 --diagnose 52-1693387 # index -> object id -> fetch
python -m diligence.xml990 --diagnose-index 52-1693387 \
    --url https://apps.irs.gov/pub/epostcard/990/xml/2026/index_2026.csv
```

The third downloads the real index and prints its actual header and any rows
matching the EIN. Nobody had looked at the file.

## Bugs already found and fixed — do not reintroduce

- Fabricated SOI element names (`progsrvcexpns` etc). All mappings now come
  from the published IRS SOI data dictionaries, per form type.
- 990-N filers were unreachable: `fetch_organization()` raised `NotFound`
  before IRS data was consulted. There is now an IRS-only brief path.
- `reserve_basis` silently blanked by a variable-name collision with the public
  support test's `basis`.
- `.capitalize()` lowercasing "Baltimore, MD" in the summary headline.
- Generic-name press matching bailed before counting corroborators, so
  "Hope House" could never confirm even with an exact EIN match.
- Whitespace delimiter represented as `None`, colliding with "no delimiter
  found" — a correctly parsed header reported as failure.
- Peer builds reported "population too small" without naming which source
  failed. Now names the cause and detects HTML error pages served as 200.
- `xml990.build_index` accepted only local paths, so passing the documented
  index URLs indexed nothing at all.
- The per-object XML source defaulted to the `irs-form-990` S3 bucket, which
  the IRS stopped updating at the end of 2021 and AWS marks deprecated. Every
  fetch 404'd, and `load_for` swallowed the error, so a dead source was
  indistinguishable from an organization with no filing.
- `/healthz` reported the XML capability live on row count alone, hiding the
  above. It now probes an actual fetch.

## Open work, roughly in priority order

1. **Empty/missing `--soi` argument crashes** with `IsADirectoryError` instead
   of a clear message. `peers.build_index` should validate sources first.
2. **Field map verification against live data.** Run
   `python -m diligence.normalize <EIN>` against real EINs of each form type.
   A non-empty `unmapped` list means the IRS changed the extract.
3. **Rebuilds require deleting the SQLite file first** — `entrypoint.sh` skips
   when the file exists. A `--force` flag would be better.
4. **Press provider is unwired.** `profile._provider_from_env()` builds one
   from `GRANTSIGHT_NEWS_URL`; no concrete adapter ships. Off by default.
5. **XML corpus is fetched per object** and cached. Fine on a 5 GB volume; a
   local corpus path exists via `GRANTSIGHT_XML_DIR` if that changes.

## Testing conventions

`python -m pytest tests/ -q` — 96 tests, no network calls, runs anywhere.

The assertions that matter are the negative ones: unknown never reads as clean,
unresolved stays `None`, an empty revocation table reports `UNKNOWN`, a 990-N
filer is context rather than a finding, a press item about a same-named
organization in another city is dropped. When adding a feature, add the
negative test too.

Large-scale paths were exercised against synthetic fixtures: 1,050,000
revocation rows in both date layouts, plus truncated, empty, and corrupt-date
variants. Fixtures exist for all three form types and for modern and pre-2013
XML schemas.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `GRANTSIGHT_CONTACT` | Appended to User-Agent. Set this. |
| `GRANTSIGHT_DATA` | Volume path, `/data` on Railway |
| `GRANTSIGHT_XML_INDEX_URLS` | Space-separated IRS index CSVs; enables XML sections |
| `GRANTSIGHT_PEERS_SOI` / `_BMF` | Space-separated URLs; enables peer percentiles |
| `GRANTSIGHT_AUTO_BUILD=0` | Disable background index builds |
| `ANTHROPIC_API_KEY` | Optional narrative paragraph |
| `GRANTSIGHT_NEWS=1` + `GRANTSIGHT_NEWS_URL` | Press mentions, off by default |

Do not set `PORT`; Railway injects it and `entrypoint.sh` reads it.

## Licensing note

ProPublica's data is non-commercial with attribution: no charging for access,
no advertising against it, citation and link required. The footer renders the
attribution on every brief. A free tool from a funder is within those terms;
contact ProPublica before any paywall or ads. IRS files carry no such
restriction — loading the SOI extract directly would remove the dependency, and
the normalizer works either way.

---

## The FY2025 bug, and why it took so long (20 Sep 2026)

**Symptom.** EINs 13-1644147 and 52-1693387 reported "last filing FY2023"
while ProPublica's website showed FY2025.

**Two independent causes. Only the second was ever looked at.**

### 1. A failed download was being read as "never filed"

`load_years()` returns the filings whose *documents* could be downloaded.
`portfolio.evaluate_one` and `brief.build` derived filing recency from that
list. So when the document fetch 404'd, the app discarded its own index's
knowledge that a FY2025 return existed and fell back to ProPublica's extract,
which stops at FY2023 — then flagged the organization as years late.

Whether a return was filed and whether we can read it are different questions.
Only the first decides lateness. Recency now comes from
`xml990.latest_indexed_year()`, which reads the IRS e-file index and touches no
network. This holds even when every document source is dead, and is pinned by
`test_filing_recency_survives_a_dead_document_source`.

### 2. There is no per-object XML URL, and there never was

Every fix before this one swapped one per-object URL for another. All were
measured on 20 Sep 2026 against object id `202641349349313769` (PPFA FY2025,
filed 14 May 2026):

| Source | Result |
|---|---|
| `s3.amazonaws.com/irs-form-990` | `NoSuchKey` — IRS stopped updating it 31 Dec 2021 |
| `gt990datalake-rawdata` (GivingTuesday) | `NoSuchKey` — mirrors older filings only |
| `projects.propublica.org/download-xml` | **HTTP 403 "Security Check"** — works in a browser, refused to servers |

The IRS distributes e-file XML exactly one way: monthly zips at
`apps.irs.gov/pub/epostcard/990/xml/{YYYY}/{YYYY}_TEOS_XML_{MM}{A..E}.zip`.

`irszip.py` reads a single filing out of one using HTTP range requests — zip
central directory first (cached per archive), then just that member's bytes.
Two ranged requests instead of a gigabyte. Handles zip64, since these archives
routinely exceed 4 GB. If a server ignores `Range`, it says so instead of
returning nothing.

The submission date is what names the archive, so `build_index` now stores
`SUB_DATE`. **An index built before this change cannot locate current-year
documents.** `entrypoint.sh` detects the old schema and rebuilds; it also
refreshes monthly, which it previously never did — a stale index reports
organizations as having stopped filing.

### Also established

ProPublica's **API** and **website** are different pipelines. For 13-1644147 the
API returns FY2023 as the newest `filings_with_data` and FY2024 only under
`filings_without_data`; the website shows full FY2025 financials. Do not use
the API to conclude a filing does not exist. That mistake was made here, out
loud, against a user who was correct.

### Verifying without a terminal

`/healthz` reports `current_financials.filing_recency` separately from document
availability. `/debug?ein=` now carries `latest_indexed_year` and a
`source_probe` block that actually GETs each source and reports status and
first bytes — measured, not inferred.
