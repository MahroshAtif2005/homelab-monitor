#!/usr/bin/env python3
"""Render the repo's star history to a self-contained SVG, from our own data.

Why this exists
---------------
star-history.com stopped being able to draw this. GitHub restricted the
stargazers endpoint on 2026-06-30 to a repository's own admins and
collaborators, and star-history.com's servers are neither — so the embedded
chart now returns a 200 whose entire content is the sentence "GitHub restricted
access to star data".

Their workaround is to embed a token in the README so their servers can read the
repo as us. The token they ask for needs `Contents: read and write`, which would
hand a third party a credential that can push to this repository — the same one
whose CI publishes images to Docker Hub. Not worth a chart.

We can read our own stargazers perfectly well, so we render it ourselves: this
script runs in Actions with the repo's own token, writes two SVGs (one per
theme), and the README points at those. No third party, no token to leak, and it
keeps working whatever star-history.com does next.

Usage
-----
    GH_TOKEN=... python scripts/star_history.py [--repo owner/name] [--out-dir docs]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"

# Theme steps are chosen per surface, not flipped: these are the dashboard's own
# --accent / --tx / --mut / --bd tokens for each mode, so the chart reads as part
# of the project rather than a bolted-on image.
THEMES = {
    "light": {"surface": "#ffffff", "ink": "#1f2328", "muted": "#636c76",
              "grid": "#d0d7de", "accent": "#9a6700", "fill": "#9a6700"},
    "dark":  {"surface": "#0d1117", "ink": "#e6edf3", "muted": "#8b949e",
              "grid": "#30363d", "accent": "#d29922", "fill": "#d29922"},
}

W, H = 800, 320
PAD_L, PAD_R, PAD_T, PAD_B = 58, 116, 44, 40


def _get(url, token):
    req = urllib.request.Request(url, headers={
        # The star+json media type is what turns each entry into {starred_at, user}.
        # Without it GitHub returns bare user objects and there is no timeline.
        "Accept": "application/vnd.github.star+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "homelab-monitor-star-history",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()), r.headers


def fetch_stars(repo, token):
    """Every (starred_at) timestamp, oldest first. Paginated 100 at a time."""
    out, page = [], 1
    while True:
        try:
            data, _ = _get(f"{API}/repos/{repo}/stargazers?per_page=100&page={page}", token)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise SystemExit(
                f"stargazers request failed: HTTP {e.code} {body}\n"
                "Since 2026-06-30 this endpoint needs a token belonging to an admin or "
                "collaborator on the repo. In Actions, GITHUB_TOKEN or STATS_PAT should qualify."
            )
        if not data:
            break
        for row in data:
            ts = row.get("starred_at") if isinstance(row, dict) else None
            if ts:
                out.append(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        if len(data) < 100:
            break
        page += 1
    out.sort()
    return out


def _nice_ticks(hi, count=4):
    """Round y-axis steps (1/2/5 × 10ⁿ) so the labels read as numbers a human
    would have chosen rather than hi/4."""
    if hi <= 0:
        return [0]
    raw = hi / count
    mag = 10 ** len(str(int(raw)).lstrip("-")) / 10 or 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= mag * m:
            step = mag * m
            break
    else:
        step = mag * 10
    # The top tick must SIT ABOVE the maximum, not below it. Stopping at the last
    # tick <= hi caps a 174-star chart at 150, and the curve then runs off the top
    # of the plot and takes the end label with it.
    ticks, v = [], 0
    while v < hi:
        ticks.append(int(v))
        v += step
    ticks.append(int(v))
    return ticks


def _month_ticks(t0, t1):
    """First of each month inside the range — a date axis should land on dates."""
    out = []
    y, m = t0.year, t0.month
    while True:
        m += 1
        if m > 12:
            m, y = 1, y + 1
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        if d >= t1:
            break
        out.append(d)
    return out


def render(stars, repo, theme_name):
    t = THEMES[theme_name]
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    total = len(stars)
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    if total < 2:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
                f'viewBox="0 0 {W} {H}" role="img" aria-label="Not enough stars to chart yet">'
                f'<rect width="{W}" height="{H}" fill="{t["surface"]}"/>'
                f'<text x="{W//2}" y="{H//2}" text-anchor="middle" fill="{t["muted"]}" '
                f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
                f'font-size="14">Not enough stars to chart yet</text></svg>')

    t0, t1 = stars[0], stars[-1]
    span = max((t1 - t0).total_seconds(), 1)
    ymax = max(_nice_ticks(total)[-1], 1)

    x_of = lambda d: PAD_L + (d - t0).total_seconds() / span * plot_w
    y_of = lambda v: PAD_T + plot_h - (v / ymax) * plot_h

    # One point per star is 174 nodes of needless SVG; the curve is a step
    # function, so sampling it at ~2px intervals is visually identical.
    step = max(1, total // 320)
    pts = [(x_of(d), y_of(i + 1)) for i, d in enumerate(stars) if i % step == 0 or i == total - 1]

    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    base = PAD_T + plot_h
    area = (f"M{pts[0][0]:.1f},{base:.1f} "
            + " ".join(f"L{x:.1f},{y:.1f}" for x, y in pts)
            + f" L{pts[-1][0]:.1f},{base:.1f} Z")

    g = []
    g.append(f'<rect width="{W}" height="{H}" fill="{t["surface"]}"/>')
    # Title names the series, so no legend box — there is only one thing plotted.
    g.append(f'<text x="{PAD_L}" y="24" fill="{t["ink"]}" font-family="system-ui,-apple-system,'
             f'Segoe UI,Helvetica,Arial,sans-serif" font-size="14" font-weight="600">'
             f'{esc(repo)} — stars over time</text>')

    # Recessive grid: horizontal only, behind the data, at the label steps.
    for v in _nice_ticks(total):
        y = y_of(v)
        g.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + plot_w}" y2="{y:.1f}" '
                 f'stroke="{t["grid"]}" stroke-width="1" opacity="0.55"/>')
        g.append(f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end" fill="{t["muted"]}" '
                 f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
                 f'font-size="11">{v}</text>')

    for d in _month_ticks(t0, t1):
        x = x_of(d)
        g.append(f'<text x="{x:.1f}" y="{base + 20:.1f}" text-anchor="middle" fill="{t["muted"]}" '
                 f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
                 f'font-size="11">{d.strftime("%b %Y")}</text>')

    g.append(f'<path d="{area}" fill="{t["fill"]}" opacity="0.13"/>')
    g.append(f'<path d="{line}" fill="none" stroke="{t["accent"]}" stroke-width="2" '
             f'stroke-linejoin="round" stroke-linecap="round"/>')

    # One direct label, on the point that matters — the current total. A number on
    # every point would be noise. The dot carries the colour; the text stays ink.
    ex, ey = pts[-1]
    g.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="{t["accent"]}" '
             f'stroke="{t["surface"]}" stroke-width="2"/>')
    g.append(f'<text x="{ex + 12:.1f}" y="{ey - 6:.1f}" fill="{t["ink"]}" '
             f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
             f'font-size="15" font-weight="700">{total}</text>')
    g.append(f'<text x="{ex + 12:.1f}" y="{ey + 10:.1f}" fill="{t["muted"]}" '
             f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
             f'font-size="11">stars</text>')

    # Stamp the last star, not today. Stamping the render date would rewrite both
    # files on every scheduled run and commit a daily no-op diff; this changes only
    # when the data does — and "when the last star arrived" is the more useful fact.
    g.append(f'<text x="{W - 12}" y="{H - 10}" text-anchor="end" fill="{t["muted"]}" '
             f'font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
             f'font-size="10" opacity="0.8">latest star {t1:%Y-%m-%d}</text>')

    label = f"Star history for {repo}: {total} stars, most recent {t1:%Y-%m-%d}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">'
            f'<title>{esc(label)}</title>' + "".join(g) + '</svg>')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY",
                                                     "SikamikanikoBG/homelab-monitor"))
    ap.add_argument("--out-dir", default="docs")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("warning: no GH_TOKEN/GITHUB_TOKEN — the stargazers endpoint needs one "
              "since 2026-06-30", file=sys.stderr)

    stars = fetch_stars(args.repo, token)
    print(f"{args.repo}: {len(stars)} stars"
          + (f", first {stars[0]:%Y-%m-%d}, latest {stars[-1]:%Y-%m-%d}" if stars else ""))

    os.makedirs(args.out_dir, exist_ok=True)
    for name in THEMES:
        path = os.path.join(args.out_dir, f"star-history-{name}.svg")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render(stars, args.repo, name) + "\n")
        print("wrote", path)


if __name__ == "__main__":
    main()
