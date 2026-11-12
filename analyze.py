#!/usr/bin/env python3
"""
Summarise logs/scans.csv: how close each group got to parity, how often it
crossed, and for how long.

    python analyze.py                 # all groups, best edge first
    python analyze.py --gaps-only     # only groups that ever showed a gap
    python analyze.py --file other.csv
"""

import argparse
import csv
from collections import defaultdict
from datetime import datetime


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarise(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[(r["slug"], r["side"])].append(r)

    out = []
    for (slug, side), rs in groups.items():
        rs.sort(key=lambda r: r["ts_utc"])
        edges = [e for e in (_f(r["edge_pct"]) for r in rs) if e is not None]
        gaps = [r for r in rs if r["gap"] == "1"]
        ann = [a for a in (_f(r["annualised_pct"]) for r in gaps) if a is not None]

        # Longest unbroken run of scans with a gap, as elapsed time.
        longest, start, prev = 0.0, None, None
        for r in rs:
            t = datetime.strptime(r["ts_utc"], "%Y-%m-%dT%H:%M:%SZ")
            if r["gap"] == "1":
                start = start or t
                prev = t
                longest = max(longest, (prev - start).total_seconds())
            else:
                start = None
        out.append({
            "slug": slug, "side": side, "scans": len(rs),
            "gap_scans": len(gaps),
            "best_edge": max(edges) if edges else None,
            "median_edge": sorted(edges)[len(edges) // 2] if edges else None,
            "best_annualised": max(ann) if ann else None,
            "longest_gap_min": longest / 60,
        })
    out.sort(key=lambda g: (g["best_edge"] is not None, g["best_edge"] or 0),
             reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/scans.csv")
    ap.add_argument("--gaps-only", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.file, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        raise SystemExit(f"No scan log at {args.file}. Run the scanner first.")
    if not rows:
        raise SystemExit("Scan log is empty.")

    summary = summarise(rows)
    if args.gaps_only:
        summary = [g for g in summary if g["gap_scans"]]

    scans = len({r["ts_utc"] for r in rows})
    print(f"{scans} scan(s), {rows[0]['ts_utc']} to {rows[-1]['ts_utc']}, "
          f"{len(summary)} group/side pair(s)\n")
    print(f"{'side':<4} {'scans':>5} {'gaps':>5} {'best edge':>10} "
          f"{'median':>8} {'best ann.':>10} {'longest':>9}  group")
    for g in summary:
        fmt = lambda v, s="%": "-" if v is None else f"{v:.2f}{s}"  # noqa: E731
        print(f"{g['side']:<4} {g['scans']:>5} {g['gap_scans']:>5} "
              f"{fmt(g['best_edge']):>10} {fmt(g['median_edge']):>8} "
              f"{fmt(g['best_annualised']):>10} "
              f"{g['longest_gap_min']:>7.1f}m  {g['slug'][:60]}")
    print("\nedge = one share-set at the best asks, after fees; negative means "
          "no gap.\nlongest = longest unbroken run of scans showing a gap.")


if __name__ == "__main__":
    main()
