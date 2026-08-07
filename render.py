#!/usr/bin/env python3
"""
Render collected analytics into a private portal page, and optionally a public summary.

    ./render.py                       # every output the config asks for
    ./render.py --private-only        # skip the public summary
    ./render.py --config path/to/config.json

Two outputs from one dataset:

  portal.out (default portal/page.html)
      Everything — all repos including the private ones, referrers, paths, per-repo
      history. Served by the gated Worker in portal/. Self-contained: no CDN, no
      fonts, no fetch, so it works from a file:// URL when the Worker is down.

  public_summary.out (optional; null to skip)
      Public repos only, and only the figures that are already public knowledge on the
      repo page anyway — stars, forks, release downloads, counts. Deliberately NOT
      traffic: views and clones say how many people looked and did not stay, which is
      nobody else's business, and a public number invites gaming. Feed it to a static
      site generator to publish a "what we've shipped" page.

**Every chart states how much history it has.** The collector cannot backfill (GitHub
keeps 14 days of traffic and no star history at all), so for a while these charts are
mostly empty, and a chart that hides that reads as "nothing happened" rather than "we
weren't looking yet". The depth note is not decoration; it is the difference between
the two.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Default palette. The two data-mark colours were checked against both surfaces below:
# they sit inside the readable lightness band and clear the chroma floor on each, so
# neither series disappears when a reader flips theme. If you replace them, check the
# new pair against BOTH your light and dark surface — a palette that only works on one
# is the usual way a themed chart goes unreadable.
#
# `accent` is used for bar fills only, never as one of the two line series.
DEFAULT_THEME = {
    "light": {
        "paper": "#fcfcfb", "paper2": "#f4f4f1",
        "ink": "#16202b", "ink2": "#5b6773", "ink3": "#8a949e",
        "line": "#e3e3de", "line2": "#d2d2cb",
        "views": "#235f92", "clones": "#a1762a",
    },
    "dark": {
        "paper": "#12161b", "paper2": "#1a2028",
        "ink": "#eef3f8", "ink2": "#a9bccd", "ink3": "#7d93a8",
        "line": "#262f39", "line2": "#36424f",
        "views": "#4a9ae0", "clones": "#bd8a2e",
    },
}

DEFAULTS = {
    "owner": None,
    "data": "data",
    "portal": {},
    "public_summary": {},
    "theme": {},
}

PORTAL_DEFAULTS = {
    "out": "portal/page.html",
    "title": "Fleet analytics",
    "subtitle": "Every repo under {owner}, public and private.",
    "window_days": 90,
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(path):
    if not path.exists():
        sys.exit(
            f"No config at {path}.\n"
            "Copy config.example.json to config.json and set at least `owner`."
        )
    raw = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
           if not k.startswith("_")}
    cfg = {**DEFAULTS, **raw}
    cfg["portal"] = {**PORTAL_DEFAULTS, **(cfg.get("portal") or {})}
    cfg["theme"] = deep_merge(DEFAULT_THEME, cfg.get("theme"))
    if not cfg["owner"]:
        sys.exit(f"`owner` is not set in {path}.")
    return cfg


def load_all(data_dir):
    index_path = data_dir / "index.json"
    if not index_path.exists():
        raise SystemExit(f"No data at {index_path} — run ./collect.py first.")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    series = {}
    series_dir = data_dir / "series"
    for path in sorted(series_dir.glob("*.json")):
        series[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return index, series


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def fleet_traffic(series, days=90):
    """Total views and clones per date across every repo."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    totals = {}
    for s in series.values():
        for date, vals in s.get("traffic", {}).items():
            if date < cutoff:
                continue
            row = totals.setdefault(date, {"views": 0, "clones": 0, "view_uniques": 0})
            row["views"] += vals.get("views", 0)
            row["clones"] += vals.get("clones", 0)
            row["view_uniques"] += vals.get("view_uniques", 0)
    return dict(sorted(totals.items()))


def top_referrers(series, limit=12):
    """Most recent referrer snapshot per repo, summed. Not a time series: GitHub gives
    a rolling 14-day top-10 and summing successive snapshots would double-count."""
    totals = {}
    for s in series.values():
        snaps = s.get("referrers", {})
        if not snaps:
            continue
        latest = snaps[max(snaps)]
        for row in latest:
            t = totals.setdefault(row["source"], {"count": 0, "uniques": 0})
            t["count"] += row["count"]
            t["uniques"] += row["uniques"]
    return sorted(totals.items(), key=lambda kv: -kv[1]["count"])[:limit]


def history_depth(series):
    dates = set()
    snaps = set()
    for s in series.values():
        dates |= set(s.get("traffic", {}))
        snaps |= set(s.get("snapshots", {}))
    return len(dates), len(snaps), (min(dates) if dates else None)


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------

def line_chart(traffic, key, colour_class, width=980, height=190):
    """ONE series per chart, on its own scale.

    Views and clones were originally drawn together and it was unreadable: clones ran
    an order of magnitude higher, so the views line sat flat on the axis and the chart
    said nothing about the series anyone actually cares about. They are also not the
    same measurement — a view is a person, a clone is mostly CI — so a shared axis was
    implying a comparison that isn't meaningful.

    The fix is small multiples, not a second y-axis. Two axes on one frame let the
    reader infer crossings and gaps that are pure artefacts of the scaling.

    One series per chart also means no legend: the heading names it.
    """
    if not traffic:
        return '<p class="empty">No traffic collected yet.</p>'

    dates = list(traffic)
    vmax = max([r.get(key, 0) for r in traffic.values()] + [1])
    pad_l, pad_r, pad_t, pad_b = 44, 76, 16, 28
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b

    def pt(i, val):
        x = pad_l + (w * i / max(1, len(dates) - 1))
        y = pad_t + h - (h * val / vmax)
        return x, y

    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" '
           f'aria-label="Daily {key} across the fleet, peak {vmax}">']

    # Recessive gridlines with labelled ticks.
    for frac in (0, 0.5, 1):
        y = pad_t + h * (1 - frac)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l+w}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" class="tick" text-anchor="end">'
                   f'{int(vmax*frac)}</text>')

    pts = [pt(i, traffic[d].get(key, 0)) for i, d in enumerate(dates)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    out.append(f'<path d="{path}" class="line line--{colour_class}"/>')
    # Direct label at the series end, so the value is readable without a tooltip.
    ex, ey = pts[-1]
    out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" class="dot dot--{colour_class}"/>')
    out.append(f'<text x="{ex+10:.1f}" y="{ey+4:.1f}" class="endlabel">'
               f'{traffic[dates[-1]].get(key, 0):,}</text>')

    for i in (0, len(dates) - 1):
        x, _ = pt(i, 0)
        anchor = "start" if i == 0 else "end"
        out.append(f'<text x="{x:.1f}" y="{height-8}" class="tick" text-anchor="{anchor}">'
                   f'{dates[i][5:]}</text>')

    out.append("</svg>")
    return "".join(out)


def sparkline(traffic, key="views", width=120, height=24):
    """One series, so no legend — the column header names it."""
    if not traffic:
        return '<span class="nodata">—</span>'
    dates = sorted(traffic)[-30:]
    vals = [traffic[d].get(key, 0) for d in dates]
    if not vals or max(vals) == 0:
        return '<span class="nodata">—</span>'
    top = max(vals)
    step = width / max(1, len(vals) - 1)
    pts = " ".join(
        f"{'M' if i == 0 else 'L'}{i*step:.1f},{height - (height-3) * v / top:.1f}"
        for i, v in enumerate(vals)
    )
    return (f'<svg viewBox="0 0 {width} {height}" class="spark" aria-hidden="true">'
            f'<path d="{pts}" class="line line--views"/></svg>')


def bars(rows, label_key, value_key, limit=12):
    """Magnitude against one dimension — a single hue, length carrying the value."""
    if not rows:
        return '<p class="empty">Nothing recorded yet.</p>'
    top = max(r[value_key] for r in rows[:limit]) or 1
    out = ['<div class="bars">']
    for r in rows[:limit]:
        pct = 100 * r[value_key] / top
        out.append(
            f'<div class="bar"><span class="bar__label" title="{esc(r[label_key])}">'
            f'{esc(r[label_key])}</span>'
            f'<span class="bar__track"><span class="bar__fill" style="width:{pct:.1f}%"></span></span>'
            f'<span class="bar__value">{r[value_key]:,}</span></div>'
        )
    out.append("</div>")
    return "".join(out)


# --------------------------------------------------------------------------
# Portal
# --------------------------------------------------------------------------

def tokens(t):
    return ("--paper:{paper}; --paper-2:{paper2}; --ink:{ink}; --ink-2:{ink2}; "
            "--ink-3:{ink3}; --line:{line}; --line-2:{line2}; "
            "--views:{views}; --clones:{clones};").format(**t)


def css(theme):
    """Light by default, dark by preference, and either one forced by data-theme on
    the root — so a host page's own theme toggle can drive this page too."""
    light, dark = tokens(theme["light"]), tokens(theme["dark"])
    return """
:root { %s }
@media (prefers-color-scheme: dark) { :root { %s } }
:root[data-theme="dark"]  { %s }
:root[data-theme="light"] { %s }
* { box-sizing:border-box; }
body {
  margin:0; background:var(--paper); color:var(--ink);
  font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
.wrap { max-width:1120px; margin:0 auto; padding:2.5rem 1.5rem 5rem; }
h1 { font-size:1.7rem; margin:0 0 .3rem; letter-spacing:-.01em; }
h2 { font-size:1rem; margin:2.75rem 0 .85rem; letter-spacing:.02em; }
.eyebrow {
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:.72rem; letter-spacing:.12em; text-transform:uppercase; color:var(--ink-3);
  margin:0 0 .5rem;
}
.muted { color:var(--ink-2); }
.depth {
  border:1px solid var(--line); border-left:3px solid var(--clones);
  background:var(--paper-2); padding:.8rem 1rem; font-size:.86rem; margin:1.25rem 0 0;
}
/* Exactly three columns, because there are exactly six tiles — auto-fit picked five at
   this width and left a lone tile orphaned on a second row. */
.tiles { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:1px;
  border:1px solid var(--line); margin-top:1.5rem; }
@media (max-width:640px) { .tiles { grid-template-columns:repeat(2,minmax(0,1fr)); } }
.tile { background:var(--paper); padding:1rem 1.1rem; outline:1px solid var(--line); }
.tile b { display:block; font-size:1.65rem; font-weight:600; letter-spacing:-.02em; }
.tile span { font-size:.78rem; color:var(--ink-2); }
.chart { width:100%%; height:auto; display:block; margin-top:.5rem; }
.grid { stroke:var(--line); stroke-width:1; }
.tick { fill:var(--ink-3); font-size:11px; font-family:ui-monospace,monospace; }
.line { fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.line--views { stroke:var(--views); }
.line--clones { stroke:var(--clones); }
.dot--views { fill:var(--views); }
.dot--clones { fill:var(--clones); }
.endlabel { font-size:11px; font-family:ui-monospace,monospace; fill:var(--ink-2); }
.bars { display:grid; gap:.4rem; margin-top:.6rem; }
.bar { display:grid; grid-template-columns:minmax(0,14rem) 1fr auto; gap:.75rem;
  align-items:center; font-size:.84rem; }
.bar__label { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--ink-2); }
.bar__track { background:var(--paper-2); height:14px; border-radius:3px; overflow:hidden; }
.bar__fill { display:block; height:100%%; background:var(--views); border-radius:0 3px 3px 0; }
.bar__value { font-family:ui-monospace,monospace; font-size:.8rem; color:var(--ink-2); }
table { width:100%%; border-collapse:collapse; margin-top:.75rem; font-size:.85rem; }
th, td { text-align:right; padding:.45rem .55rem; border-bottom:1px solid var(--line); }
th:first-child, td:first-child { text-align:left; }
th { font-size:.72rem; text-transform:uppercase; letter-spacing:.07em; color:var(--ink-3);
  font-weight:500; cursor:pointer; user-select:none; white-space:nowrap; }
th:hover { color:var(--ink); }
td.num { font-family:ui-monospace,monospace; }
tr.private td:first-child::after {
  content:"private"; margin-left:.5rem; font-size:.66rem; letter-spacing:.06em;
  text-transform:uppercase; color:var(--ink-3); border:1px solid var(--line-2);
  padding:.05rem .3rem; border-radius:3px;
}
.spark { width:120px; height:24px; }
.nodata, .empty { color:var(--ink-3); }
.empty { font-size:.86rem; }
.scroll { overflow-x:auto; }
.foot { margin-top:3rem; font-size:.78rem; color:var(--ink-3); border-top:1px solid var(--line);
  padding-top:1rem; }
""" % (light, dark, dark, light)


JS = """
// Sort the table by any column. Numeric columns carry data-sort so a formatted value
// ("1,204") still sorts as a number rather than as text.
document.querySelectorAll('th[data-col]').forEach((th) => {
  let asc = false;
  th.addEventListener('click', () => {
    const tbody = th.closest('table').querySelector('tbody');
    const rows = [...tbody.rows];
    const col = [...th.parentNode.children].indexOf(th);
    asc = !asc;
    rows.sort((a, b) => {
      const av = a.cells[col].dataset.sort ?? a.cells[col].textContent;
      const bv = b.cells[col].dataset.sort ?? b.cells[col].textContent;
      const an = parseFloat(av), bn = parseFloat(bv);
      const cmp = (!isNaN(an) && !isNaN(bn)) ? an - bn : String(av).localeCompare(String(bv));
      return asc ? cmp : -cmp;
    });
    rows.forEach(r => tbody.appendChild(r));
  });
});
"""


def render_portal(cfg, index, series):
    p = cfg["portal"]
    owner = index.get("owner") or cfg["owner"]
    traffic = fleet_traffic(series, p["window_days"])
    days, snaps, first = history_depth(series)
    repos = index["repos"]

    totals = {
        "repos": len(repos),
        "public": sum(1 for r in repos if not r["private"]),
        "stars": sum(r.get("stars", 0) for r in repos),
        "downloads": sum(r.get("downloads", 0) for r in repos),
        "views": sum(v["views"] for v in traffic.values()),
        "clones": sum(v["clones"] for v in traffic.values()),
    }

    tiles = [
        (f'{totals["repos"]}', "repos tracked"),
        (f'{totals["public"]}', "of them public"),
        (f'{totals["stars"]:,}', "stars"),
        (f'{totals["downloads"]:,}', "release downloads"),
        (f'{totals["views"]:,}', f"views, last {days}d collected"),
        (f'{totals["clones"]:,}', "clones, same window"),
    ]

    rows = []
    for r in sorted(repos, key=lambda x: -x.get("stars", 0)):
        s = series.get(r["name"], {})
        t = s.get("traffic", {})
        recent = sorted(t)[-30:]
        v30 = sum(t[d].get("views", 0) for d in recent)
        c30 = sum(t[d].get("clones", 0) for d in recent)
        rows.append(
            f'<tr class="{"private" if r["private"] else ""}">'
            f'<td>{esc(r["name"])}</td>'
            f'<td class="num" data-sort="{r.get("stars",0)}">{r.get("stars",0)}</td>'
            f'<td class="num" data-sort="{v30}">{v30:,}</td>'
            f'<td class="num" data-sort="{c30}">{c30:,}</td>'
            f'<td class="num" data-sort="{r.get("downloads",0)}">{r.get("downloads",0):,}</td>'
            f'<td class="num" data-sort="{r.get("issues_open",0)}">{r.get("issues_open",0)}</td>'
            f'<td data-sort="{v30}">{sparkline(t)}</td>'
            f'<td class="num muted" data-sort="{r.get("days_of_history",0)}">'
            f'{r.get("days_of_history",0)}d</td>'
            f'</tr>'
        )

    refs = [{"source": k, "count": v["count"]} for k, v in top_referrers(series)]
    dl = sorted([r for r in repos if r.get("downloads")], key=lambda r: -r["downloads"])

    depth_note = (
        f"<strong>{days} distinct days</strong> of traffic and <strong>{snaps} daily "
        f"snapshot{'s' if snaps != 1 else ''}</strong> collected so far"
        + (f", earliest {first}." if first else ".")
        + " GitHub keeps traffic for 14 days and reports stars and downloads only as a"
        " running total, so nothing before the first collection can ever be recovered —"
        " a short history here means the collector is young, not that the fleet is quiet."
    )

    subtitle = p["subtitle"].replace("{owner}", owner)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{esc(p["title"])}</title>
<style>{css(cfg["theme"])}</style>
</head>
<body>
<div class="wrap">
  <p class="eyebrow">Private · {esc(index["date"])}</p>
  <h1>{esc(p["title"])}</h1>
  <p class="muted">{esc(subtitle)}</p>

  <p class="depth">{depth_note}</p>

  <div class="tiles">
    {"".join(f'<div class="tile"><b>{v}</b><span>{l}</span></div>' for v, l in tiles)}
  </div>

  <h2>Daily page views across the fleet</h2>
  <p class="muted" style="font-size:.84rem">Summed over every repo. People.</p>
  {line_chart(traffic, "views", "views")}

  <h2>Daily clones across the fleet</h2>
  <p class="muted" style="font-size:.84rem">
    Its own scale — clones typically run an order of magnitude higher than views, so
    drawing the two together buries the views line on the axis. Most clone volume is CI
    and mirrors rather than people.
  </p>
  {line_chart(traffic, "clones", "clones")}

  <h2>Where the traffic comes from</h2>
  <p class="muted" style="font-size:.84rem">
    Each repo's most recent top-10 referrers, summed. Not a time series — GitHub reports
    a rolling 14-day window, so adding successive snapshots would double-count.
  </p>
  {bars(refs, "source", "count")}

  <h2>Release downloads</h2>
  {bars([{"source": r["name"], "count": r["downloads"]} for r in dl], "source", "count")}

  <h2>By repo</h2>
  <div class="scroll">
  <table>
    <thead><tr>
      <th data-col>Repo</th><th data-col>Stars</th><th data-col>Views 30d</th>
      <th data-col>Clones 30d</th><th data-col>Downloads</th><th data-col>Open issues</th>
      <th>Views trend</th><th data-col>History</th>
    </tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
  </div>

  <p class="foot">
    Generated {esc(index["generated"])} by gh-fleet-analytics. Click any column heading
    to sort.
  </p>
</div>
<script>{JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Public summary
# --------------------------------------------------------------------------

def render_public(index, series):
    """Public repos, and only figures already visible on the repo page itself.

    Traffic is deliberately excluded — see the module docstring. What is left is the
    stuff a visitor could count by hand, presented so they do not have to.
    """
    out = []
    for r in index["repos"]:
        if r["private"] or r["archived"]:
            continue
        out.append({
            "name": r["name"],
            "stars": r.get("stars", 0),
            "forks": r.get("forks", 0),
            "downloads": r.get("downloads", 0),
            "releases": r.get("releases", 0),
            "language": r.get("language"),
            "license": r.get("license"),
            "latest_release": r.get("latest_release"),
            "latest_release_at": r.get("latest_release_at"),
            "pushed_at": r.get("pushed_at"),
        })
    out.sort(key=lambda r: -r["stars"])
    return {
        "generated": index["generated"],
        "totals": {
            "repos": len(out),
            "stars": sum(r["stars"] for r in out),
            "downloads": sum(r["downloads"] for r in out),
            "releases": sum(r["releases"] for r in out),
        },
        "repos": out,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default="config.json", help="path to config.json")
    ap.add_argument("--private-only", action="store_true", help="skip the public summary")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve()
    cfg = load_config(cfg_path)
    base = cfg_path.parent
    index, series = load_all((base / cfg["data"]).resolve())

    portal_out = (base / cfg["portal"]["out"]).resolve()
    portal_out.parent.mkdir(parents=True, exist_ok=True)
    portal_out.write_text(render_portal(cfg, index, series), encoding="utf-8")
    print(f"wrote {portal_out} ({len(index['repos'])} repos)")

    summary_out = (cfg.get("public_summary") or {}).get("out")
    if summary_out and not args.private_only:
        dest = (base / summary_out).resolve()
        if not dest.parent.exists():
            # A missing parent almost always means the sibling website checkout isn't
            # there, which is not an error worth failing the whole render over.
            print(f"skipped public summary: {dest.parent} does not exist")
            return
        payload = render_public(index, series)
        dest.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {dest} ({payload['totals']['repos']} public repos)")


if __name__ == "__main__":
    main()
