# Publishing GrantSight

Two separate things, often confused:

1. **Putting the code on GitHub** so others can read and fork it.
2. **Running the app somewhere public** so people can use it without installing
   anything.

GitHub does the first. It cannot do the second on its own — GitHub Pages serves
static files only, and this is a Python server that needs to run. Step 4 covers
hosting.

---

## 1. Check what you are about to commit

Do this before `git init`, not after. Things that must never enter the history:

| Never commit | Why |
| --- | --- |
| `data/` | The IRS indexes. Hundreds of MB, rebuildable in ten minutes, and GitHub rejects files over 100 MB. |
| `.cache/` | Cached HTTP responses. |
| `ANTHROPIC_API_KEY`, `GRANTSIGHT_NEWS_KEY` | Keys belong in the host's environment settings. |
| `*.sqlite3`, `*.zip` | Build artifacts. |

The included `.gitignore` already covers these. Verify it did:

```bash
cd grantsight
git init
git add -A
git status --short | grep -E "data/|\.cache/|\.sqlite3|\.zip"   # should print nothing
```

If that prints anything, stop and fix `.gitignore` before committing. Removing a
large file or a key from git history afterwards is genuinely painful.

A key that has already been committed is compromised even after you delete it.
Rotate it rather than just removing the file.

---

## 2. Create the repository

**With the GitHub CLI** (`brew install gh`, then `gh auth login`):

```bash
git commit -m "GrantSight: public-source nonprofit due diligence"
gh repo create grantsight --public --source=. --push
```

That creates the repo, sets the remote, and pushes in one step.

**Without the CLI:** create an empty repository at
<https://github.com/new> — public, and do *not* let it add a README, .gitignore
or licence, since you already have them. Then:

```bash
git commit -m "GrantSight: public-source nonprofit due diligence"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/grantsight.git
git push -u origin main
```

**Already private and want to flip it:** Settings → General → scroll to Danger
Zone → Change visibility → Make public.

---

## 3. Add a licence and the data notices

Two things a public repository of this kind needs.

**A licence for your code.** Without one, nobody may legally reuse it. MIT is the
usual choice for a tool like this: <https://choosealicense.com/licenses/mit/>.
Save the text as `LICENSE` in the root. GitHub will detect and display it.

**A notice about the data, which your licence does not cover.** ProPublica's
Nonprofit Explorer data is offered under non-commercial terms with an
attribution requirement — no charging for access, no selling advertising
against it, and a citation and link if you publish. The app renders the
attribution on every brief, but say it in the README too, and contact
ProPublica before doing anything commercial. The IRS files are US government
works and carry no such restriction.

If you would rather not take on the ProPublica terms at all, load the IRS SOI
extract directly and drop the dependency. The normalizer works either way.

---

## 4. Run it somewhere public

The app is a standard ASGI service plus two locally built SQLite indexes. That
shapes the hosting choice: you need a machine with a disk, not a static host or
a short-lived function.

### Option A — Railway (recommended)

See [RAILWAY.md](RAILWAY.md) for the full walkthrough, including the volume
timing constraint that shapes how the indexes get built.

Render and Fly work the same way — deploy from the repo, attach a persistent
volume, use the included `Dockerfile`:

1. Connect the repo.
2. Add a persistent disk mounted at `/data` (5 GB is comfortable; the IRS
   revocation index alone is a few hundred MB).
3. Set environment variables:
   ```
   GRANTSIGHT_DATA=/data
   GRANTSIGHT_CACHE=/data/cache
   GRANTSIGHT_CONTACT=you@example.org
   ```
   `GRANTSIGHT_CONTACT` goes into the User-Agent. Set it — it is how ProPublica
   reaches you instead of blocking you.
4. Deploy, then build the indexes once, in the host's shell:
   ```bash
   python -m diligence.irs_status --build
   ```
   This takes about ten minutes and exits non-zero if verification fails.
5. Confirm with `GET /healthz`, which reports whether the index exists.

Until step 4 runs, the app still works — every brief simply reports exemption
status as unverified rather than implying good standing.

### Option B — a VPS

Any small instance works. Run behind nginx or Caddy with `uvicorn app:app
--host 127.0.0.1 --port 8000` under systemd, and add a monthly cron entry:

```cron
0 4 1 * * cd /srv/grantsight && python -m diligence.irs_status --build >> /var/log/grantsight.log 2>&1
```

The IRS republishes monthly. `--build` verifies itself and exits non-zero on
failure, so cron will email you if a rebuild goes wrong rather than leaving a
silently stale index. Briefs older than 40 days start carrying a staleness
finding on their own.

### What will not work

- **GitHub Pages** — static files only, no Python.
- **Serverless functions** — the SQLite indexes need a persistent disk, and a
  cold start cannot rebuild them.
- **The free tiers that sleep** — fine for a demo; the first request after a
  sleep will be slow.

---

## 5. Keep the tests running

The suite is 86 tests with no network calls, so it runs anywhere. Add
`.github/workflows/tests.yml`:

```yaml
name: tests
on: [push, pull_request]
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python -m pytest tests/ -q
```

Worth doing here specifically: most of these tests assert *negative* behaviour —
that an unknown exemption status never reads as clean, that an unresolved field
never becomes zero, that a press item about a different organization is
dropped. Those are exactly the failures nobody notices by eye.

---

## 6. Before you point real users at it

- **Verify the field map once.** `python -m diligence.normalize <EIN>` against a
  few real organizations of each form type. A non-empty `unmapped` list means
  the IRS changed the extract.
- **Build the peer index**, or the peer section stays hidden:
  `python -m diligence.peers --build --soi <extract> --bmf <bmf>`.
- **Decide about the XML.** Without it there is no program expense ratio, no
  named officers and no grants-made detail; the briefs say so in the gaps
  section. See the README for `GRANTSIGHT_XML_DIR`.
- **Leave press mentions off unless you have a search provider you trust.** They
  are off by default. The identity-confirmation rules are strict, but no rule
  catches everything, and a confident headline about the wrong organization in
  a diligence brief is the worst failure this tool can produce.
- **Say what it is.** A short line on the landing page — background research
  from public filings, not advice and not a funding recommendation — sets the
  right expectation. The footer says it on every brief; the front page should
  too.
