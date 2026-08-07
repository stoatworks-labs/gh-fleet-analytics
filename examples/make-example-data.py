#!/usr/bin/env python3
"""
Generate the synthetic dataset in examples/data/.

Entirely made up — no real account is involved. It exists so `render.py` produces a
real page before you have collected anything, and so CI has something to render on
every push. Deterministic (fixed seed, dates counted back from a fixed day), so
regenerating it produces no diff unless the shape actually changed.

    ./examples/make-example-data.py
"""

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# Fixed, so the committed files are stable.
LAST_DAY = date(2026, 8, 6)
DAYS = 45
SEED = 20260806

REPOS = [
    # name,                private, lang,          stars, base views, base clones, releases
    ("aurora-mixer",       False, "Rust",          412, 38, 210, 6),
    ("beacon-cli",         False, "Go",            188, 21, 340, 4),
    ("cinder-ui",          False, "TypeScript",     97, 15,  62, 3),
    ("driftwood",          False, "Python",         54,  9,  48, 2),
    ("ember-probe",        False, "C++",            31,  6,  27, 1),
    ("fathom-docs",        False, "MDX",             12,  4,  11, 0),
    ("glasshouse",         True,  "TypeScript",      0,  2,   9, 0),
    ("harbourmaster",      True,  "Python",          0,  1,   6, 0),
]

REFERRERS = ["Google", "github.com", "news.ycombinator.com", "reddit.com",
             "example.com", "duckduckgo.com", "lobste.rs"]
PATHS = ["/{r}", "/{r}/releases", "/{r}/blob/main/README.md", "/{r}/issues"]


def main():
    rng = random.Random(SEED)
    dates = [(LAST_DAY - timedelta(days=n)).isoformat() for n in range(DAYS - 1, -1, -1)]
    today = LAST_DAY.isoformat()
    generated = datetime(2026, 8, 6, 4, 14, 0, tzinfo=timezone.utc).isoformat(timespec="seconds")

    (DATA / "series").mkdir(parents=True, exist_ok=True)
    index = {"generated": generated, "date": today, "owner": "example-org", "repos": []}

    for name, private, lang, stars, bv, bc, nrel in REPOS:
        traffic, snapshots = {}, {}
        downloads = 0
        for i, d in enumerate(dates):
            # A gentle upward drift with weekday seasonality, so the charts look like
            # traffic rather than noise.
            weekend = date.fromisoformat(d).weekday() >= 5
            trend = 1 + i / (DAYS * 2)
            dip = 0.55 if weekend else 1.0
            views = max(0, int(bv * trend * dip * rng.uniform(0.6, 1.45)))
            clones = max(0, int(bc * trend * rng.uniform(0.5, 1.6)))
            traffic[d] = {
                "views": views,
                "view_uniques": max(1, int(views * rng.uniform(0.45, 0.8))) if views else 0,
                "clones": clones,
                "clone_uniques": max(1, int(clones * rng.uniform(0.2, 0.5))) if clones else 0,
            }
            downloads += int(nrel * rng.uniform(0, 9))
            snapshots[d] = {
                "stars": max(0, stars - int((DAYS - 1 - i) * rng.uniform(0, 0.5))),
                "forks": max(0, stars // 9),
                "watchers": max(0, stars // 14),
                "open_issues": rng.randint(0, 7),
                "size_kb": rng.randint(400, 24000),
                "downloads": downloads,
                "releases": nrel,
                "issues_open": rng.randint(0, 7),
                "issues_closed": rng.randint(0, 60),
                "prs_open": rng.randint(0, 3),
                "prs_merged": rng.randint(0, 40),
            }

        meta = {
            "name": name, "private": private, "archived": False,
            "description": f"An entirely fictional project called {name}.",
            "language": lang, "license": "MIT" if not private else None,
            "topics": [], "created_at": "2025-11-02T09:00:00Z",
            "pushed_at": f"{today}T08:12:00Z",
            "homepage": None, "default_branch": "main",
        }
        if nrel:
            meta["latest_release"] = f"v0.{nrel}.0"
            meta["latest_release_at"] = f"{dates[-8]}T12:00:00Z"

        refs = rng.sample(REFERRERS, k=min(5, len(REFERRERS)))
        series = {
            "repo": name,
            "traffic": traffic,
            "snapshots": snapshots,
            "referrers": {today: [
                {"source": s, "count": rng.randint(4, 240), "uniques": rng.randint(2, 90)}
                for s in refs
            ]},
            "paths": {today: [
                {"path": p.format(r=name), "count": rng.randint(5, 300),
                 "uniques": rng.randint(3, 120)} for p in PATHS
            ]},
            "releases": {today: {f"v0.{nrel}.0": {f"{name}-v0.{nrel}.0.zip": downloads}}} if nrel else {},
            "meta": meta,
        }
        (DATA / "series" / f"{name}.json").write_text(
            json.dumps(series, indent=1) + "\n", encoding="utf-8")

        last = snapshots[today]
        index["repos"].append({
            "name": name, "private": private, "archived": False,
            "description": meta["description"], "language": lang,
            "license": meta["license"], "topics": [],
            "created_at": meta["created_at"], "pushed_at": meta["pushed_at"],
            "latest_release": meta.get("latest_release"),
            "latest_release_at": meta.get("latest_release_at"),
            "days_of_history": len(traffic),
            **last,
        })

    (DATA / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(REPOS)} series and index.json to {DATA}")


if __name__ == "__main__":
    main()
