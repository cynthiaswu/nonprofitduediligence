# GrantSight

A public due-diligence brief for any US nonprofit, from an EIN or a name.

No uploads. No CRM. No record of who looked up what. Every input is a string
someone types; every source is a free public dataset.

```
GET /                     search by name or EIN
GET /brief?ein=52-1693387 the brief, as a printable document
GET /api/brief.json?ein=  the same brief as JSON
GET /healthz              whether the IRS index has been built
```

## What this adds over ProPublica Nonprofit Explorer

Nonprofit Explorer already gives you name search, subsection and NTEE codes,
multi-year revenue, expenses, assets and liabilities, and links to filing PDFs.
If all you need is those numbers, use their site directly — it is excellent and
this app calls it.

What it does not do, and this does:

| Gap | How it is closed here |
| --- | --- |
| Auto-revocation status is absent from the 990 data | `irs_status.py` indexes the Auto-Revocation List, Pub. 78, and 990-N, with build-time verification |
| Field names differ across 990 / 990-EZ / 990-PF | `normalize.py` carries a per-form map transcribed from the published IRS SOI data dictionaries |
| No derived metrics | Reserve months, net margin, revenue change, multi-year trend, contribution share, officer comp share, grant share |
| No separation of restricted funds | Part X lines 27–29, so reserve months are reported on net assets *without* donor restrictions |
| No revenue concentration | The Schedule A public support test and the over-2%-of-support figure, both of which are in the extract |
| Nothing about whether the org still exists | The filing's own termination, liquidation, and excess-benefit answers |
| 990-N filers are excluded entirely | Indexed from the IRS e-Postcard file; a brief is still produced when ProPublica has no record at all |
| No synthesis | A summary block, a verdict sentence, ranked findings, and an explicit not-determined list |
| No sense of what the org actually does | Mission, scale, staffing, leadership and grantmaking, pulled from the filings |
| No peer context | True percentiles over the full sector-and-state population, from a local SOI-to-BMF join |
| No functional expense split | `xml990.py` reads Part IX columns B, C and D from the Form 990 XML |
| No named officers or compensation | Part VII Section A and Schedule J, with titles, hours and pay |
| No grants-made detail | Schedule I recipients, with EINs that link through to their own brief |

The part that matters most is the last row of the brief, not the first. A brief
that quietly omits what it could not establish is worse than no brief, so
"What this brief could not determine" is rendered at the same weight as the
findings, and an unresolved field renders as `n/d` rather than `$0`.

## Run it

```bash
pip install -r requirements.txt
python -m diligence.irs_status --build     # ~10 min, a few hundred MB
uvicorn app:app --reload
```

`--build` downloads the three IRS files, indexes them, and then verifies the
result before you trust it. It exits non-zero if anything is wrong, so it is
safe to run from cron:

```
  revocation: 1,050,000 rows
  sample dates: ['13-Mar-2022', '27-Feb-2010', '12-Feb-2025']
  parsed as: DD-Mon-YYYY

Verification:
  revocation dates span 2010-01-01 to 2025-12-27
  reinstated: 150,000
  probe 100000001: revoked
  probe 100000000: reinstated

PASS — status checks are live.
```

Check a single organization without starting the server:

```bash
python -m diligence.irs_status --check 52-1693387
python -m diligence.irs_status --verify
```

Peer percentiles need a second index, built from two files that each hold half
of what is needed — the SOI extract has financials but no sector or state, and
the BMF has sector and state but no financials. On Railway this is driven by
the `GRANTSIGHT_PEERS_SOI` and `GRANTSIGHT_PEERS_BMF` variables; locally:

```bash
python -m diligence.peers --build \
  --soi https://www.irs.gov/pub/irs-soi/23eofinextract990.zip \
  --bmf https://www.irs.gov/pub/irs-soi/eo1.csv --bmf https://www.irs.gov/pub/irs-soi/eo2.csv
```

Both flags repeat and accept paths or URLs. Without this index, peer
comparison reports as unavailable rather than being approximated from a
sample.

The app runs before the index exists. Until it does, every brief says
exemption status was not checked, rather than implying good standing.

Optional environment variables:

```
ANTHROPIC_API_KEY     enables the narrative paragraph; a deterministic
                      template runs without it
GRANTSIGHT_CONTACT    your email, appended to the User-Agent — set this if
                      you put the app in front of real traffic
GRANTSIGHT_PEERS=1    enable peer comparison by default (slow: ~13 API calls)
GRANTSIGHT_CACHE      HTTP cache directory (default ./.cache)
GRANTSIGHT_DATA       IRS index directory (default ./data)
IRS_REVOCATION_URL    override if the IRS moves the bulk files
```

## The field map, and the one ratio you cannot have

`FIELD_MAP` in `normalize.py` is transcribed from the published IRS SOI extract
data dictionaries, per form type. The three forms genuinely differ:

| Metric | Form 990 | Form 990-EZ | Form 990-PF |
| --- | --- | --- | --- |
| Total revenue | `totrevenue` | `totrevnue` | `totrcptperbks` |
| Total expenses | `totfuncexpns` | `totexpns` | `totexpnspbks` |
| Net assets | `totnetassetend` | `totnetassetsend` | `tfundnworth` |
| Contributions | `totcntrbgfts` | `totcntrbs` | `grscontrgifts` |

A candidate may also be a tuple, meaning "sum these": 990-PF investment income
is `intrstrvnue + dividndsamt`, and 990 grants paid is
`grntstogovt + grnsttoindiv + grntstofrgngovt`. Provenance records which
elements were actually used, and it surfaces in the JSON output.

**The program expense ratio is not computable from the extract.** Every Part IX
line in the SOI extract is column (A), the total column. Columns B (program),
C (management) and D (fundraising) are not extracted, and there is no
`progsrvcexpns` element — an earlier draft of this code invented one. The split
comes from the Form 990 XML instead; see below. When the XML is unavailable the
brief says "not published in this source" rather than "did not resolve", which
would wrongly imply a fixable mapping bug.

## The Form 990 XML

`xml990.py` reads what the extract omits: the Part IX functional split, Part VII
Section A officers, Schedule J compensation detail, and Schedule I grants made.
Element names are taken from the IRS annotated form
(`2023form990withfieldnames.pdf`) and the MeF schemas:

| What | Element |
| --- | --- |
| Functional split | `TotalFunctionalExpensesGrp` → `TotalAmt`, `ProgramServicesAmt`, `ManagementAndGeneralAmt`, `FundraisingAmt` |
| People | `Form990PartVIISectionAGrp` → `PersonNm`, `TitleTxt`, `ReportableCompFromOrgAmt`, role indicators |
| Compensation detail | `RltdOrgOfficerTrstKeyEmplGrp` (Schedule J Part II) |
| Grants made | `RecipientTable` → `RecipientBusinessName`, `RecipientEIN`, `CashGrantAmt`, `PurposeOfGrantTxt` |

Nothing uses a rigid xpath. Every lookup is a namespace-agnostic search by
local element name against an ordered candidate list, so pre-2013 filings
(`Form990PartVIISectionA`, `NamePerson`, `TotalFunctionalExpenses`) parse with
the same code, and the tag that matched is recorded in `provenance`.

The corpus lives at `apps.irs.gov/pub/epostcard/990/xml/{year}/` with a
per-year index CSV. Build the EIN lookup, then point at extracted XML:

```bash
python -m diligence.xml990 --build-index index_2024.csv --build-index index_2023.csv
export GRANTSIGHT_XML_DIR=/path/to/extracted/xml
python -m diligence.xml990 --ein 52-1693387
```

`GRANTSIGHT_XML_OBJECT_URL` allows per-object fetching instead of a local
corpus. With neither available, briefs build without the XML sections and say
so in the gaps list.

## The summary block, and press mentions

Every brief opens with what the organization is: a one-line classification, its
mission statement from the filing, and a short fact list — revenue and trend,
staffing, program spending share, reserve, grantmaking, who leads it. All of it
comes from filings already parsed, so none of it needs hedging.

Two additions do reach outside the filings, and both are constrained.

**The website check** fetches only the domain the organization reported on its
own 990. There is no search, so there is no possibility of checking a different
entity. An unreachable site is reported as context and never as a finding —
plenty of healthy organizations let a domain lapse.

**Press mentions are off by default** and inert unless you supply a search
provider. Nonprofit names collide constantly; there are hundreds of
organizations called "Hope House". A confident news result about the wrong one
is worse than no result, because a diligence brief is exactly where a reader
will believe it. So every candidate must corroborate identity: the name has to
match after normalization, *and* at least one independent identifier has to
appear — city, state, EIN, or the domain from the filing. Names with no
distinctive tokens require two. Items that fail are dropped silently rather
than shown with a warning, since a warning beside a plausible headline does not
stop anyone believing it.

Press items are context only. They cannot raise a brief's severity, they never
become findings, and no article is summarized beyond its own headline.

Enable with `GRANTSIGHT_NEWS=1`. The default provider is the
[GDELT DOC 2.0 API](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/):
free, no key, one request per 5 seconds. GDELT returns headlines without
excerpts, so the adapter asks it for articles whose full text contains the
quoted name and the organization's city or state, and records that full-text
match as the corroborator. Set `GRANTSIGHT_NEWS_PROVIDER=gnews` (with
`GNEWS_API_KEY`) or `=url` (with `GRANTSIGHT_NEWS_URL`, a JSON search API
template containing `{query}`) to use another source, or pass a `search`
callable to `brief.build()`.

## Picking this up cold

[HANDOFF.md](HANDOFF.md) is the brief for a new contributor or a coding agent:
verified data-source URLs, the design invariants that must not be broken, bugs
already found and fixed, and what is genuinely impossible so nobody burns time
on it.

## Publishing

- [RAILWAY.md](RAILWAY.md) — deploying the full application, Form 990 XML
  included. Read the first section: Railway mounts volumes at container start,
  not at build, which is why the index builds in the background.
- [PUBLISHING.md](PUBLISHING.md) — GitHub, licensing, and general hosting,
  including what must never enter the git history.

## Reserve, and what an organization can actually spend

Part X splits net assets into line 27 (without donor restrictions) and lines
28–29 (with donor restrictions), and all three are in the SOI extract for Form
990 filers. An earlier version of this code asserted they were not, and printed
that claim into the reserve finding.

Reserve months are therefore computed on unrestricted net assets where the
filing reports them, with `reserve_basis` recording which figure was used, and
the finding states both numbers. A separate finding fires when the total
reserve looks comfortable but the spendable portion does not — the case that
matters most and the one a single total hides.

The IRS never updated Part X for ASU 2016-14, so post-2018 filers put donor
restricted amounts on line 28 or line 29 depending on which guidance they
followed. Both are read and summed, which is correct either way. 990-EZ and
990-PF have no equivalent split; those briefs use total net assets and say so.

### On naming people

An earlier version of this code declined to surface individual compensation,
on the reasoning that assembling information about named people is a different
kind of product. That was wrong. Part VII exists so that funders, regulators
and the public can see who runs an exempt organization and what it pays them;
the IRS requires disclosure and the organization must hand a copy to anyone who
asks. Omitting it protected nobody and made the brief worse at its job.

The limit that does apply: people appear in organizational capacity only —
name, title, hours, compensation. Home addresses present in some filings are
never extracted, and nothing is aggregated across organizations to profile an
individual.

`python -m diligence.normalize <EIN>` still exists, now as a regression check:
a non-empty `unmapped` list on a real filing means the IRS changed the extract.

## How the revocation check fails safe

Exemption status is the one finding that can change a funding decision on its
own, so every failure path in `irs_status.py` resolves to UNKNOWN and never to
CLEAR. Specifically:

| Failure | Result |
| --- | --- |
| No index built | UNKNOWN, with the build command in the message |
| Download failed, revocation table empty | UNKNOWN — *not* "clean", which is the worst bug this code could have |
| Dates present but unparseable | UNKNOWN for that EIN, quoting the raw string |
| Index older than 40 days | Status still reported, plus a finding that the data is stale |
| Schema version changed | Index ignored until rebuilt |

Dates are the fragile part. The IRS data dictionary documents MM/DD/YYYY, but a
widely used community loader extracts the year in a way that only works on an
11-character date like `15-May-2024`. Both layouts (and five others) parse, and
`--build` reports which one the file actually used. If a future file uses
something else, `--verify` fails loudly instead of silently clearing every
revoked organization.

`--verify` checks row count against the known scale of the list, date
parse rate over a 2,000-row sample, the plausibility of the date range, that
reinstatement dates are being read at all, and then round-trips a real revoked
EIN and a real reinstated EIN back through `check()`.

If the IRS moves the files, `--build` falls back to scraping the download
links off the TEOS landing page. Failing that, download them by hand and pass
`--revocation path.zip --pub78 path.zip --epostcard path.zip`.

## Terms, before this goes public

ProPublica's data terms are non-commercial with attribution: no charging people
to look at the data, no selling ads against it, and a citation and link if you
publish. Every brief carries the attribution, and the footer names both sources.
A free tool published by a funder is almost certainly within this; email
ProPublica before adding a paywall or ad slot. To sidestep the terms entirely,
load the IRS SOI extract directly and drop the ProPublica dependency — the
normalizer works the same either way.

## Deliberately not built

- **Auto-submission or writeback anywhere.** Read-only by design.
- **A score.** Every threshold in `metrics.THRESHOLDS` is a convention funders
  disagree about. They are named and in one place so you can argue with them.
  Nothing here ranks organizations or recommends a decision.
- **Adverse-news search.** Wiring a search API here is easy and getting it
  right is not: name collisions on common nonprofit names produce confident,
  wrong results about the wrong organization. Do it deliberately, with a
  human confirming entity identity, or not at all.
- **Anything about individuals.** Officer compensation stays aggregate. The
  per-person figures are public in Schedule J, but a tool that assembles
  dossiers on named people is a different product with different obligations.
- **PDF fallback for paper filers.** A small number of organizations still
  file on paper, so no XML exists. Those briefs fall back to the extract and
  say which sections are missing. OCR of the scanned PDF is possible and is
  not attempted.

## Layout

```
app.py                    routes
diligence/
  propublica.py           API client, EIN parsing, name resolution
  normalize.py            cross-form field mapping + `--introspect` utility
  irs_status.py           auto-revocation / Pub 78 / 990-N index, lookup, verify
  metrics.py              derived ratios, flags, thresholds, gap statements
  peers.py                SOI-to-BMF join, population percentiles, verify
  profile.py              summary, highlights, website check, press matching
Dockerfile                container image; binds $PORT
entrypoint.sh             serves immediately, builds indexes in the background
railway.json              pins the Dockerfile builder and the health check
  xml990.py               Form 990 XML: functional split, people, Schedule I/J
  narrative.py            optional summary, constrained to computed findings
  brief.py                assembly, degrading source by source
  render.py               HTML
tests/test_pipeline.py    13 tests, fully offline
```

The narrative model never sees raw 990 data — only findings this code already
computed, with an instruction to introduce no number that is not in them. A
model that cannot see the raw data cannot invent a figure from it.

## Test

```bash
python -m pytest tests/ -q     # 86 tests, no network
```

The assertions that matter are the negative ones: unresolved stays `None`,
unknown status never reads as clean, an empty revocation table reports UNKNOWN,
an unparseable date reports UNKNOWN, and a 990-N filer is context rather than a
finding against the organization.

Fixtures cover all three form types plus modern and pre-2013 XML, so the
per-form element names and the schema-drift fallbacks are exercised rather
than assumed. The status module was separately exercised
against synthetic files at real scale — 1,050,000 revocation rows in both date
layouts, plus truncated, empty, and corrupt-date variants — to confirm
`--verify` catches each one.
