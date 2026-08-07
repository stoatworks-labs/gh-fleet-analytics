# AGENTS.md — gh-fleet-analytics

A GitHub analytics collector and dashboard renderer. Two Python files with no
dependencies, one Cloudflare Worker, one synthetic dataset for testing.

## The rule that matters

**The data is not reproducible.** GitHub keeps traffic for 14 days and reports stars,
forks and downloads only as a current total. Anything that drops, truncates or
overwrites a collected day destroys history that cannot be re-fetched at any price.
Before changing `merge()` or `prune()` in `collect.py`, be certain the change cannot
lose a date.

Corollaries that already caught someone:

- **Merge, never append.** GitHub revises the last day or two after the fact. Appending
  only unseen dates freezes a half-counted day forever.
- **`prune()` applies to the rolling trails only** — referrers, paths, releases. Traffic
  and snapshots are never pruned.

## Layout

- `collect.py` — fetch and merge. Config-driven; no account name appears anywhere in it.
- `render.py` — portal HTML and the optional public summary, from the same dataset.
- `config.example.json` — the schema, documented inline with `_`-prefixed keys.
- `portal/` — the Worker. `page.html` is a build output and is gitignored.
- `examples/` — synthetic dataset, its generator, a config, and the workflow users copy.

Paths in a config resolve **against the config file**, not the working directory, so
the same command works from anywhere. Keep it that way.

## Testing

There is no test suite. CI renders `examples/config.json` on every push, which catches
renderer breakage but never touches the GitHub API. The collector's API handling is
exercised only by running it for real.

```bash
python3 render.py --config examples/config.json     # what CI runs
python3 collect.py --check                          # fetch, report, write nothing
python3 collect.py --only some-repo                 # one repo, for quick API checks
```

## Things previously got wrong, so check them

- **A read-only token 403s on every traffic endpoint.** Traffic needs push access. A 403
  with `X-RateLimit-Remaining` still healthy is a permissions answer, not a rate limit,
  and retrying cannot fix it — `get()` distinguishes these and must keep doing so.
- **`Retry-After` is checked before `X-RateLimit-Reset`.** The secondary (burst) limit
  reports a healthy remaining budget while refusing the request. Check it first or a
  burst-limited call looks like a permissions failure and gets silently dropped from the
  day's data.
- **`/orgs/{owner}` is a 404 for a personal account.** `list_repos()` tries the
  authenticated-user endpoint first, and prints which endpoint it used — the public
  fallback drops every private repo without erroring, so the print is the only signal.
- **The search API allows 30 requests per minute**, a twentieth of the core budget, and
  four run per repo. The 2-second sleep is load-bearing; removing it makes a hundred-repo
  run spend most of its life in backoff.
- **Views and clones must not share a y-axis.** Clones run an order of magnitude higher —
  mostly CI, not people — so a shared axis buries the views line and implies a comparison
  that is not meaningful. Small multiples, one series per chart, no legend.
- **The portal fails closed.** No `PORTAL_PASSWORD`, no data — 503. This page names
  private repos; do not add a fallback that serves it unauthenticated.

## Publishing changes

`portal/page.html` is bundled into the Worker at deploy time, not fetched. Re-render
before deploying or you ship the previous page:

```bash
python3 render.py && cd portal && npx wrangler deploy
```
