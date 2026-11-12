"""
Scan log: one CSV row per priced group, per side, per scan.

Rows are written whether or not a gap exists. That is the point: a few weeks
of this shows how close each group gets to parity, how often it crosses, and
how long a crossing lasts. Summarise it with `python analyze.py`.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import List

FIELDS = [
    "ts_utc", "slug", "side", "legs", "days", "catch_all",
    "ask_sum", "fees", "payout_per_set", "edge_pct", "min_top_size",
    "gap", "shares", "cost", "net", "margin_pct", "annualised_pct",
]


def make_row(cand, side, payout_per_set, top, opp) -> dict:
    return {
        "ts_utc": "",
        "slug": cand.slug,
        "side": side,
        "legs": len(cand.markets),
        "days": "" if cand.days is None else round(cand.days, 2),
        "catch_all": int(cand.catch_all),
        "ask_sum": top["ask_sum"],
        "fees": top["fees"],
        "payout_per_set": payout_per_set,
        "edge_pct": top["edge_pct"],
        "min_top_size": top["min_size"],
        "gap": int(opp is not None),
        "shares": opp.shares if opp else "",
        "cost": opp.total_cost if opp else "",
        "net": opp.net_profit if opp else "",
        "margin_pct": opp.margin_pct if opp else "",
        "annualised_pct": (
            round(opp.annualised_pct, 3)
            if opp and opp.annualised_pct is not None else ""
        ),
    }


def append(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        for row in rows:
            writer.writerow({**row, "ts_utc": ts})
