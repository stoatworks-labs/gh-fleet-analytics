#!/usr/bin/env python3
"""
Snapshot every repo an account owns into a directory of per-repo JSON files.

    ./collect.py --check              # fetch, report, write nothing
    ./collect.py                      # fetch and merge into the data directory
    ./collect.py --commit             # ... and commit + push the data repo
    ./collect.py --only some-repo
    ./collect.py --config path/to/config.json

**This exists because GitHub throws the data away.** Traffic views and clones are
retained for FOURTEEN DAYS and there is no API that returns a day older than that.
Stars, forks and release download counts are worse: the API only ever reports the
CURRENT total, so a star history simply does not exist unless something wrote the
number down each day. Everything this file does is in service of that one fact — the
history is only as long as the run history, and a gap in the runs is a permanent hole
in the record, not something a later run can backfill.

That is also why the merge is written the way it is. Each run re-fetches the whole
14-day window and merges it over what is already on disk, newest value winning, rather
than appending only the days it has not seen. GitHub revises the last day or two after
the fact (a partial day gets completed, deduplicated uniques get corrected), so
"append only new dates" would freeze a half-counted day forever. Merging is also what
makes the collector safe to run twice in one day, or to run after a week of downtime
and still recover the days inside the window.

Storage is one JSON file per repo, keyed by ISO date. Not a database: this is a few
hundred KB a year, it belongs in git where it is versioned and diffable and cannot be
lost with a hosting account, and a diff of a data file is a genuinely useful review
artefact. The portal reads these files; nothing reads GitHub live.

Two kinds of series live side by side in each file and they must not be confused:

  traffic    per-DAY measurements GitHub attributes to a date. Authoritative for that
             date, mergeable, and the only series that can be backfilled at all.
  snapshots  point-in-time totals read on the day of the run, filed under the run
             date. Cumulative counters — stars, forks, download counts. A gap here
             cannot be interpolated honestly, so the portal differences consecutive
             snapshots and shows a gap as a gap.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com"
UA = "gh-fleet-analytics"

DEFAULTS = {
    "owner": None,
    "data": "data",
    "include_private": True,
    "exclude": [],
    "include_forks": False,
    # Top-10 referrer and path lists are snapshots of a rolling 14-day window, so they
    # are only worth keeping as a coarse trail. A year of them is still under 100 KB
    # a repo.
    "trail_days": 400,
}


def load_config(path):
    """Read config.json and fill in the defaults. Unknown keys are left alone so the
    renderer can share the same file without this script having to know its schema."""
    if not path.exists():
        sys.exit(
            f"No config at {path}.\n"
            "Copy config.example.json to config.json and set at least `owner`."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    if not cfg["owner"]:
        sys.exit(f"`owner` is not set in {path}. It is the GitHub user or org to collect.")
    return cfg


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def token():
    """Prefer the environment (CI), fall back to the gh CLI (a human at a terminal)."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(
            "No GitHub token. Set GITHUB_TOKEN, or run `gh auth login` locally.\n"
            "The token needs `repo` scope — traffic data requires PUSH access, so a\n"
            "read-only token returns 403 on every traffic endpoint."
        )


TOKEN = None


def get(path, retries=4):
    """GET an API path. Returns (status, parsed-body-or-None).

    Handles the three failure modes that actually happen here rather than raising:
      202  traffic stats are still being computed — retry, then give up for today
      403  rate limited (or no push access) — back off on the reset header
      404  repo renamed or deleted since the index was built
    """
    url = f"{API}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 202:
                    time.sleep(2 + attempt * 3)
                    continue
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            if e.code in (403, 429):
                # Retry-After is the SECONDARY limit (too fast a burst), which the
                # search endpoints hit long before the primary budget runs out and
                # which reports a still-healthy X-RateLimit-Remaining while doing so.
                # Check it first or a burst-limited request looks like a permissions
                # failure and gets silently dropped from the day's data.
                retry_after = e.headers.get("Retry-After")
                if retry_after:
                    wait = min(int(retry_after) + 1, 900)
                    print(f"    secondary limit, sleeping {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                reset = e.headers.get("X-RateLimit-Reset")
                remaining = e.headers.get("X-RateLimit-Remaining")
                # Remaining == 0 is a real rate limit; a 403 with budget left is a
                # permissions answer (no push access) and retrying cannot fix it.
                if remaining == "0" and reset:
                    wait = max(0, int(reset) - int(time.time())) + 2
                    print(f"    rate limited, sleeping {wait}s", flush=True)
                    time.sleep(min(wait, 900))
                    continue
                return 403, None
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return e.code, None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return 0, None
    return 202, None


def paged(path, cap=10):
    """Follow pagination up to `cap` pages, returning the concatenated list."""
    out = []
    for page in range(1, cap + 1):
        sep = "&" if "?" in path else "?"
        status, body = get(f"{path}{sep}per_page=100&page={page}")
        if status != 200 or not body:
            break
        out.extend(body)
        if len(body) < 100:
            break
    return out


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def list_repos(cfg):
    """Every repo owned by `owner` that the token can see.

    **Which endpoint you use decides whether private repos exist at all**, and getting
    it wrong fails silently — you get a smaller list, not an error:

      /user/repos          the authenticated user's own repos, private included. The
                           only endpoint that works when `owner` is a personal account,
                           because /orgs/{user} returns 404 for a user and always will.
      /orgs/{owner}/repos  an organisation's repos, private included if the token has
                           org access.
      /users/{owner}/repos public repos only — the fallback, and the one that quietly
                           drops the private half if you reach it by accident.

    So: try the authenticated-user endpoint when the token belongs to `owner`, then the
    org endpoint, then the public one, and say which was used.
    """
    owner = cfg["owner"]
    repos, via = [], None

    status, me = get("/user")
    if status == 200 and me and me.get("login", "").lower() == owner.lower():
        repos = [r for r in paged("/user/repos?affiliation=owner&sort=full_name")
                 if (r.get("owner") or {}).get("login", "").lower() == owner.lower()]
        via = "/user/repos"

    if not repos:
        repos = paged(f"/orgs/{owner}/repos?type=all&sort=full_name")
        via = f"/orgs/{owner}/repos" if repos else via

    if not repos:
        repos = paged(f"/users/{owner}/repos?type=owner&sort=full_name")
        via = f"/users/{owner}/repos (PUBLIC ONLY)" if repos else via

    if not repos:
        sys.exit(f"No repos found for {owner}. Check the name and the token's scope.")

    if not cfg["include_forks"]:
        repos = [r for r in repos if not r.get("fork")]
    if not cfg["include_private"]:
        repos = [r for r in repos if not r.get("private")]
    excluded = {n.lower() for n in cfg["exclude"]}
    repos = [r for r in repos if r["name"].lower() not in excluded]

    print(f"listed via {via}")
    return repos


def day(ts):
    """GitHub timestamps traffic buckets at midnight UTC; we key by the date alone."""
    return ts.split("T")[0]


def fetch_repo(owner, name):
    """Everything worth having about one repo, in one dict. Missing pieces are None
    rather than absent, so the caller can tell 'not collected' from 'zero'."""
    out = {"traffic": {}, "referrers": None, "paths": None, "snapshot": {}, "releases": {}}

    status, meta = get(f"/repos/{owner}/{name}")
    if status != 200 or not meta:
        return None, f"metadata {status}"
    out["meta"] = {
        "name": meta["name"],
        "private": meta["private"],
        "archived": meta.get("archived", False),
        "description": meta.get("description"),
        "language": meta.get("language"),
        "license": (meta.get("license") or {}).get("spdx_id"),
        "topics": meta.get("topics", []),
        "created_at": meta.get("created_at"),
        "pushed_at": meta.get("pushed_at"),
        "homepage": meta.get("homepage"),
        "default_branch": meta.get("default_branch"),
    }
    out["snapshot"] = {
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "watchers": meta.get("subscribers_count", 0),
        "open_issues": meta.get("open_issues_count", 0),
        "size_kb": meta.get("size", 0),
    }

    notes = []

    for kind, key in (("views", "views"), ("clones", "clones")):
        status, body = get(f"/repos/{owner}/{name}/traffic/{kind}")
        if status != 200 or not body:
            notes.append(f"{kind} {status}")
            continue
        for row in body.get(key, []):
            d = out["traffic"].setdefault(day(row["timestamp"]), {})
            if kind == "views":
                d["views"] = row["count"]
                d["view_uniques"] = row["uniques"]
            else:
                d["clones"] = row["count"]
                d["clone_uniques"] = row["uniques"]

    status, body = get(f"/repos/{owner}/{name}/traffic/popular/referrers")
    if status == 200 and body is not None:
        out["referrers"] = [
            {"source": r["referrer"], "count": r["count"], "uniques": r["uniques"]}
            for r in body
        ]

    status, body = get(f"/repos/{owner}/{name}/traffic/popular/paths")
    if status == 200 and body is not None:
        out["paths"] = [
            {"path": r["path"], "count": r["count"], "uniques": r["uniques"]}
            for r in body
        ]

    # Release asset download counts are cumulative per asset and never reset, so the
    # portal differences them. Store per tag so a re-uploaded asset is visible.
    rels = paged(f"/repos/{owner}/{name}/releases", cap=3)
    total = 0
    for rel in rels:
        assets = {a["name"]: a.get("download_count", 0) for a in rel.get("assets", [])}
        if assets:
            out["releases"][rel["tag_name"]] = assets
            total += sum(assets.values())
    out["snapshot"]["downloads"] = total
    out["snapshot"]["releases"] = len(rels)
    if rels:
        out["meta"]["latest_release"] = rels[0]["tag_name"]
        out["meta"]["latest_release_at"] = rels[0].get("published_at")

    # Open/closed issue and PR counts, via search so closed ones are counted too.
    for label, q in (
        ("issues_open", "type:issue state:open"),
        ("issues_closed", "type:issue state:closed"),
        ("prs_open", "type:pr state:open"),
        ("prs_merged", "type:pr is:merged"),
    ):
        status, body = get(f"/search/issues?q=repo:{owner}/{name}+{q.replace(' ', '+')}")
        if status == 200 and body:
            out["snapshot"][label] = body.get("total_count", 0)
        # The search API allows 30 requests/MINUTE — a twentieth of the core budget —
        # and four of these run per repo, so across a hundred repos an unpaced loop
        # spends most of its life in backoff. 2s keeps it just under the limit.
        time.sleep(2.0)

    return out, ", ".join(notes)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def load_series(series_dir, name):
    path = series_dir / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"repo": name, "traffic": {}, "snapshots": {}, "referrers": {}, "paths": {},
            "releases": {}, "meta": {}}


def prune(trail, today, trail_days):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=trail_days)).date().isoformat()
    return {k: v for k, v in trail.items() if k >= cutoff}


def merge(existing, fresh, today, trail_days):
    """Fold one fetch into the stored series. Returns (series, added, revised).

    New values win on overlapping traffic dates — see the module docstring: GitHub
    completes a partial day after the fact, so the newer read is the better one.
    """
    added = revised = 0
    for date, vals in fresh["traffic"].items():
        prior = existing["traffic"].get(date)
        if prior is None:
            existing["traffic"][date] = vals
            added += 1
        elif prior != {**prior, **vals}:
            existing["traffic"][date] = {**prior, **vals}
            revised += 1

    existing["meta"] = fresh["meta"]
    existing["snapshots"][today] = fresh["snapshot"]
    if fresh["referrers"] is not None:
        existing["referrers"][today] = fresh["referrers"]
        existing["referrers"] = prune(existing["referrers"], today, trail_days)
    if fresh["paths"] is not None:
        existing["paths"][today] = fresh["paths"]
        existing["paths"] = prune(existing["paths"], today, trail_days)
    if fresh["releases"]:
        existing["releases"][today] = fresh["releases"]
        existing["releases"] = prune(existing["releases"], today, trail_days)

    existing["traffic"] = dict(sorted(existing["traffic"].items()))
    existing["snapshots"] = dict(sorted(existing["snapshots"].items()))
    return existing, added, revised


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def shrink_problem(prev_repos, now_repos):
    """Is this collection materially smaller than the last one? -> (problem, missing).

    A scheduled run once went GREEN having collected 78 repos instead of 103, silently
    dropping every private one, because the token carried `public_repo` rather than the
    full `repo` scope. Nothing errored: the API answered every request cheerfully, just
    about a smaller world. A run that quietly loses a quarter of the fleet and reports
    success is worse than one that crashes — the per-repo series files are left
    untouched and simply stop updating, so the data goes stale invisibly behind a
    plausible-looking dashboard of the wrong fleet.

    Repos do occasionally get deleted, so a small shrink is a warning rather than an
    error. Losing the private repos ENTIRELY is never legitimate, and is named
    explicitly because the cause is always the same one.
    """
    prev_n, now_n = len(prev_repos), len(now_repos)
    prev_priv = sum(1 for r in prev_repos if r.get("private"))
    now_priv = sum(1 for r in now_repos if r.get("private"))
    missing = {r["name"] for r in prev_repos} - {r["name"] for r in now_repos}

    if prev_priv > 0 and now_priv == 0:
        return (
            f"saw {prev_priv} private repos last time and none now. The token cannot\n"
            f"see private repositories — a classic PAT needs the full `repo` scope, not\n"
            f"`public_repo` (they are nested in GitHub's UI and easy to confuse).",
            missing,
        )
    if now_n < prev_n * 0.9:
        return (f"collected {now_n} repos, down from {prev_n} — more than a 10% drop.",
                missing)
    return None, missing


def main():
    global TOKEN
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default="config.json", help="path to config.json")
    ap.add_argument("--check", action="store_true", help="fetch and report, write nothing")
    ap.add_argument("--commit", action="store_true", help="commit and push the result")
    ap.add_argument("--only", help="restrict to one repo name")
    ap.add_argument("--allow-shrink", action="store_true",
                    help="write the index even if it lost repos since last time")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)
    # Relative paths in the config resolve against the config file, not the working
    # directory, so the same command works from anywhere.
    data_dir = (cfg_path.parent / cfg["data"]).resolve()
    series_dir = data_dir / "series"
    owner = cfg["owner"]

    TOKEN = token()
    today = datetime.now(timezone.utc).date().isoformat()

    repos = list_repos(cfg)
    if args.only:
        # Repo names are frequently mixed case, so match case-insensitively rather
        # than making the caller guess the capitalisation.
        want = args.only.lower()
        repos = [r for r in repos if r["name"].lower() == want]
        if not repos:
            sys.exit(f"No repo named {args.only} under {owner}")
    print(f"{len(repos)} repos, snapshot date {today}\n")

    index = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "date": today, "owner": owner, "repos": []}
    failures = []

    for i, r in enumerate(sorted(repos, key=lambda x: x["name"]), 1):
        name = r["name"]
        fresh, note = fetch_repo(owner, name)
        if fresh is None:
            print(f"[{i:>3}/{len(repos)}] {name:<34} FAILED  {note}")
            failures.append((name, note))
            continue

        series = load_series(series_dir, name)
        series, added, revised = merge(series, fresh, today, cfg["trail_days"])
        if not args.check:
            write(series_dir / f"{name}.json", series)

        index["repos"].append({
            "name": name,
            "private": fresh["meta"]["private"],
            "archived": fresh["meta"]["archived"],
            "description": fresh["meta"]["description"],
            "language": fresh["meta"]["language"],
            "license": fresh["meta"]["license"],
            "topics": fresh["meta"]["topics"],
            "created_at": fresh["meta"]["created_at"],
            "pushed_at": fresh["meta"]["pushed_at"],
            "latest_release": fresh["meta"].get("latest_release"),
            "latest_release_at": fresh["meta"].get("latest_release_at"),
            "days_of_history": len(series["traffic"]),
            **fresh["snapshot"],
        })

        flag = "priv" if fresh["meta"]["private"] else "    "
        print(f"[{i:>3}/{len(repos)}] {name:<34} {flag} "
              f"+{added:>2}d ~{revised:>2}d  {len(series['traffic']):>4}d total"
              + (f"   ({note})" if note else ""))

    # Refuse to write a materially smaller index than last time. See shrink_problem():
    # a run that silently loses a quarter of the fleet and reports success leaves every
    # series file untouched, so the data goes stale invisibly behind a dashboard that
    # looks perfectly plausible — a far worse outcome than a crash.
    prev_path = data_dir / "index.json"
    if prev_path.exists() and not args.only:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        problem, missing = shrink_problem(prev.get("repos", []), index["repos"])
        if problem and not args.allow_shrink:
            print(f"\nREFUSING TO WRITE: {problem}", file=sys.stderr)
            if missing:
                sample = ", ".join(sorted(missing)[:8])
                print(f"Missing: {sample}"
                      + (f" (+{len(missing)-8} more)" if len(missing) > 8 else ""),
                      file=sys.stderr)
            print("\nThe existing per-repo series files are untouched and no data has been\n"
                  "lost. Fix the token and re-run. If this shrink is genuine (repos really\n"
                  "were deleted), re-run with --allow-shrink.", file=sys.stderr)
            sys.exit(1)
        if problem:
            print(f"\nWARNING: {problem} (proceeding, --allow-shrink given)")

    if not args.check:
        write(data_dir / "index.json", index)

    print(f"\n{len(index['repos'])} collected, {len(failures)} failed")
    for name, note in failures:
        print(f"  {name}: {note}")

    if args.commit and not args.check:
        def git(*a):
            return subprocess.run(["git", "-C", str(data_dir), *a],
                                  text=True, capture_output=True)
        git("add", ".")
        if not git("status", "--porcelain", ".").stdout.strip():
            print("nothing changed")
            return
        git("commit", "-m", f"analytics: snapshot {today}")
        push = git("push")
        print("pushed" if push.returncode == 0 else f"push failed: {push.stderr}")


if __name__ == "__main__":
    main()
