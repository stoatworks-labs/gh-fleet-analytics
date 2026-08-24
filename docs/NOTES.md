# Notes

Working notes for this repo: status, decisions, and the traps that have actually bitten.
Migrated out of Claude Code's memory on 2026-08-24, so they are written in the first
person and dated by when each thing was learned — that date is usually the useful part.

Cross-cutting notes that are not specific to this repo live in
[fleet-notes](https://github.com/stoatworks-labs/fleet-notes).

*gh-fleet-analytics — the PUBLIC GitHub analytics collector/dashboard, canonical over the copy vendored into stoatworks-backend*

`~/Projects/gh-fleet-analytics` — **PUBLIC, MIT, v0.1.0** (tagged 2026-08-07).
`github.com/stoatworks-labs/gh-fleet-analytics`. Generalised out of the
[stoatworks backend](https://github.com/stoatworks-labs/stoatworks-backend/blob/main/docs/NOTES.md) (`stoatworks-backend`) analytics system and published so other people
can build the same dashboard for their own repos.

Two dependency-free Python files (`collect.py`, `render.py`), a Cloudflare
Worker in `portal/`, and a **synthetic** dataset in `examples/data/` so the
renderer produces a real page before any collection has happened. Everything
account-specific lives in `config.json`: owner, paths, palette, portal title,
`PORTAL_REALM`. CI renders the example dataset and parses `examples/collect.yml`
on every push; it never touches the GitHub API.

**This repo is CANONICAL.** `stoatworks-backend` vendors it — see
[analytics tool vendoring](https://github.com/stoatworks-labs/fleet-notes/blob/main/notes/reference_analytics_tool_vendoring.md). Never fix a collector or renderer bug in
the backend copy.

The site entry is in `projects.json` under `infra`/`library`, with a thumbnail
rendered from the *synthetic* data, and `/analytics` carries a callout linking
to it. Both live.

**What is deliberately NOT published:** the collected data. `analytics/data/`
names every private repo with its description, so the public repo ships made-up
repos (`aurora-mixer`, `beacon-cli`, …) instead, generated deterministically by
`examples/make-example-data.py`. CI fails if that generator's output drifts from
what is committed.
