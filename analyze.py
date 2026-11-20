#!/usr/bin/env python3
"""
Summarise logs/scans.csv: how close each group got to parity, how often it
crossed, and for how long.

    python analyze.py                 # all groups, best edge first
    python analyze.py --gaps-only     # only groups that ever showed a gap
    python analyze.py --file other.csv
    python analyze.py --fills         # results of the fill test (logs/fills.csv)
"""

import argparse
import csv
from collections import Counter, defaultdict
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


def summarise_fills(path):
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        raise SystemExit(f"No fill log at {path} yet. It is written when a gap "
                         "clears the hurdle.")
    if not rows:
        raise SystemExit("Fill log is empty.")
    order = ["held", "shrank", "gone", "unfillable"]
    counts = Counter(r["verdict"] for r in rows)
    print(f"{len(rows)} fill test(s), {rows[0]['ts_utc']} to {rows[-1]['ts_utc']}\n")
    for v in order:
        print(f"  {v:<11} {counts.get(v, 0):>4}  ({counts.get(v, 0) / len(rows):.0%})")
    held = [r for r in rows if r["verdict"] in ("held", "shrank")]
    kept = [_f(r["fresh_net"]) / _f(r["detected_net"]) for r in held
            if _f(r["fresh_net"]) is not None and _f(r["detected_net"])]
    if kept:
        kept.sort()
        print(f"\nWhere still profitable, median share of the detected profit "
              f"left: {kept[len(kept) // 2]:.0%}")
    print(f"\n{'verdict':<11} {'detected':>9} {'after':>9}  group")
    for r in rows[-20:]:
        after = "-" if r["fresh_net"] == "" else f"${_f(r['fresh_net']):.2f}"
        print(f"{r['verdict']:<11} ${_f(r['detected_net']):>8.2f} {after:>9}  {r['slug'][:55]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="logs/scans.csv")
    ap.add_argument("--gaps-only", action="store_true")
    ap.add_argument("--fills", action="store_true",
                    help="summarise the fill test log instead")
    args = ap.parse_args()
    if args.fills:
        summarise_fills("logs/fills.csv" if args.file == "logs/scans.csv" else args.file)
        return

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
