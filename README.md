# gh-fleet-analytics

### A private dashboard for every repo you own, because GitHub throws the numbers away

> **AI-assisted project.** This codebase was created with [Claude](https://claude.com/claude-code)
> (Anthropic), directed and reviewed by a human author. The collector and renderer run daily
> against a ~100-repo account and the outputs are the ones in use there. CI renders the
> bundled synthetic dataset on every push, which proves the renderer works; it does not
> exercise the GitHub API. The Cloudflare Worker is deployed and serving, but the deploy
> path is not covered by tests.

GitHub's own Insights tab shows you one repo at a time, and it forgets. Traffic views and
clones are kept for **fourteen days**. Stars, forks and release download counts are worse:
the API only ever reports the *current* total, so a star history does not exist unless
something wrote the number down each day.

This writes the number down each day.

```
collect.py  ──►  data/           ──►  render.py  ──►  portal/page.html  ──►  Worker
   nightly       one JSON per repo                    one self-contained    Basic auth
   GitHub API    committed to git                     HTML file            over TLS
                                          └────────►  summary.json
                                                      public repos only
```

Everything is Python standard library and one small Cloudflare Worker. No database, no
dependencies to install, no service to sign up for.

![The portal, rendered from the bundled synthetic dataset](docs/screenshots/portal.png)

*The portal, rendered from the synthetic dataset that ships with the repo — run
`python3 render.py --config examples/config.json` to produce exactly this.*

---

## What you get

A single page, gated behind a password, covering **every repo you own — public and
private, in one table**:

- Daily views and clones across the whole account, as far back as you have collected
- Where the traffic came from, aggregated across repos
- Release download counts, ranked
- Per-repo table with 30-day sparklines, sortable by any column
- An honest note at the top saying exactly how much history exists

And optionally a second output: a JSON summary of your **public** repos — stars, forks,
downloads, releases — to feed a "what we've shipped" page on your own site.

Traffic is deliberately excluded from that public file. Views and clones say how many
people looked and did not stay, which is nobody else's business, and a public number
invites gaming.

---

## Why the history matters more than the dashboard

The dashboard is the easy part. The reason this exists at all is the collection, and the
collection has one property worth understanding before you start:

**A day you do not collect is a day gone forever.** Not delayed — gone. GitHub will not
serve you a traffic day older than 14 days, and there is no historical endpoint for stars
or downloads at any age. If the nightly job fails for three weeks, those three weeks are
a permanent hole, and running the collector twice afterwards will not fill it.

So: start collecting before you think you need it, and treat a red workflow run as data
loss rather than as a flaky build. Everything else here is recoverable.

Two consequences show up in the code and are worth knowing about:

- **Each run re-fetches the whole 14-day window and merges it over what is on disk**,
  newest value winning, rather than appending only unseen days. GitHub revises the last
  day or two after the fact — a partial day gets completed, deduplicated uniques get
  corrected — so "append only new dates" would freeze a half-counted day forever. Merging
  is also what makes the collector safe to run twice in a day, or to run after a week of
  downtime and still recover the days still inside the window.
- **Traffic and snapshots are stored separately and must not be confused.** Traffic is a
  per-day measurement GitHub attributes to a date: authoritative, mergeable, the only
  series that can be backfilled at all. Snapshots are point-in-time totals read on the day
  of the run — stars, forks, download counts. A gap in snapshots cannot be interpolated
  honestly, so the portal differences consecutive snapshots and shows a gap as a gap.

---

## Setup

### 1. Put it somewhere with your data

Your collected data belongs in **your** repo, not in a clone of this one — it is your
history and it needs to be committed. Two shapes work:

**Copy the scripts in** (simplest). Drop `collect.py`, `render.py`, `config.json` and
`portal/` into a repo you own, ideally a private one.

**Or keep this as a submodule** and hold only `config.json` and `data/` in your repo.

Either way, `data/` must be in a repo you push to, because that is the history.

### 2. Configure

```bash
cp config.example.json config.json
```

Set `owner` to your GitHub username or org. Everything else has a working default; the
file documents each key inline.

### 3. Get a token

Traffic endpoints require **push** access — a read-only token returns 403 on every single
one of them, which is the most common way this appears broken.

At <https://github.com/settings/tokens>:

- **Classic PAT** with the `repo` scope, or
- **Fine-grained PAT** with *Administration: read* + *Metadata: read*, applied to **all
  repositories**

No expiry, or diary the renewal — an expired token fails silently every night, and every
silent night is a permanent hole.

### 4. Run it

```bash
python3 collect.py --check      # fetch and report, write nothing
python3 collect.py              # fetch and merge into data/
python3 render.py               # render the portal page
```

`collect.py` falls back to `gh auth token` when `GITHUB_TOKEN` is unset, so locally you
need nothing beyond a logged-in `gh`.

Open `portal/page.html` in a browser. It is entirely self-contained — no CDN, no fonts, no
fetch — so it works from a `file://` URL, and you can stop here if a local file is all you
want.

### 5. Collect nightly

Copy `examples/collect.yml` into your repo as `.github/workflows/collect.yml`, then add
the token as a repository secret:

```bash
gh secret set ANALYTICS_TOKEN
```

Note it is **not** `secrets.GITHUB_TOKEN` — Actions' built-in token is scoped to the one
repository and returns 404 for every other repo you own, which looks exactly like a
deleted repo rather than a permissions problem.

### 6. Serve it, if you want it off your laptop

```bash
cd portal
cp wrangler.example.jsonc wrangler.jsonc     # edit name and route
npx wrangler secret put PORTAL_PASSWORD
npx wrangler deploy
```

The rendered page is bundled into the Worker as a string at deploy time, so there is no
origin to secure separately and no bucket to misconfigure. Re-render and redeploy to
update it. Any username is accepted at the browser prompt; only the password is checked.

**It fails closed.** With no `PORTAL_PASSWORD` set the Worker serves 503 rather than the
data — the one failure mode worth being loud about, since this page names your private
repos.

---

## Try it before you collect anything

A synthetic dataset ships with the repo, so you can see the output immediately:

```bash
python3 render.py --config examples/config.json
```

That writes `portal/page.html` from made-up numbers for a made-up account. CI runs the
same command on every push.

Regenerate it with `examples/make-example-data.py` — it is deterministic, so it produces
no diff unless the shape actually changed.

---

## Cost and rate limits

Roughly **4 minutes of Actions time a day per hundred repos**, most of it spent pacing
rather than working: the search API allows 30 requests a *minute* against the core budget's
5,000 an hour, and four search calls run per repo, so the loop deliberately sleeps 2
seconds between them. On a private repo that is about 120 minutes a month against the
2,000-minute free allowance. On a public repo, nothing.

A Cloudflare cron Worker was the obvious alternative and does not work: the
50-subrequest-per-invocation limit is reached long before a hundred repos are done.

---

## Things that will bite you

- **A read-only token 403s on every traffic endpoint.** Traffic needs push access. This is
  not documented anywhere obvious.
- **`public_repo` is not `repo`.** They are nested in GitHub's token UI and trivially
  confused, and picking the narrow one does not produce an error — the API answers every
  request cheerfully, just about a smaller world. A run that quietly collects 78 of your
  103 repos and reports success is worse than one that crashes, because the missing
  repos' series files are left untouched and simply stop updating behind a dashboard that
  still looks plausible. `collect.py` compares each run against the previous index and
  **refuses to write a materially smaller one**; losing the private repos entirely is
  called out by name. `--allow-shrink` overrides it when repos really were deleted.
- **`/orgs/{owner}` returns 404 for a personal account** and always will. `collect.py`
  tries the authenticated-user endpoint first for exactly this reason, then the org
  endpoint, then the public one — and prints which it used, because the public fallback
  silently drops every private repo rather than erroring.
- **Traffic endpoints answer 202** while the day's aggregation is in flight. The collector
  retries; the scheduled run is at 04:10 UTC rather than on the hour partly for this
  reason, and partly because GitHub delays on-the-hour cron under load.
- **Two overlapping runs lose data.** Both read the series files, both merge, and the
  second push wins. The workflow sets a concurrency group.
- **A push during collection makes the checkout stale.** Collection takes about fifteen
  minutes; anything landing on your default branch in that window makes `git push` fail
  with "fetch first" and throws the whole run away. The workflow rebases and retries three
  times before giving up loudly.

---

## Licence

MIT. See [LICENSE](LICENSE).
