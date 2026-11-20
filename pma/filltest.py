"""
Fill test: could a gap the scanner found actually have been bought?

When a gap clears the hurdle, wait a few seconds (roughly the time an order
takes to reach the exchange), fetch the order books again, and price the same
number of share-sets against them, fees included. One row per test goes to
logs/fills.csv, so over time you can see what share of gaps were real.

Verdicts:
  held        still fillable and still above the hurdle
  shrank      still fillable and profitable, but below the hurdle
  gone        still fillable, but no longer profitable
  unfillable  the depth needed to buy the full set has disappeared

Read-only: nothing here places an order.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List

from . import config, orderbook
from .arbitrage import AskLevel, Leg, Opportunity, annualised_pct, effective_cost

FIELDS = [
    "ts_utc", "slug", "side", "delay_s", "days", "shares",
    "detected_cost", "detected_net", "detected_ann_pct",
    "fillable", "fresh_cost", "fresh_net", "fresh_ann_pct", "verdict",
]


def recheck(opp: Opportunity, delay: float = config.FILL_DELAY,
            fetch_books: Callable[[List[str]], Dict[str, List[AskLevel]]] = None,
            sleep: Callable[[float], None] = time.sleep,
            hurdle_apr: float = config.HURDLE_APR) -> dict:
    fetch_books = fetch_books or orderbook.fetch_ask_books
    sleep(delay)
    books = fetch_books([leg.token_id for leg in opp.legs])

    fillable, fresh_cost = True, 0.0
    for leg in opp.legs:
        fresh = Leg(leg.token_id, leg.outcome, books.get(leg.token_id, []),
                    leg.fee_rate, leg.fee_exp)
        cost, filled = effective_cost(fresh.cost_asks(), opp.shares)
        if filled < opp.shares - 1e-9:
            fillable = False
            break
        fresh_cost += cost

    row = {
        "slug": opp.event, "side": opp.side, "delay_s": delay,
        "days": "" if opp.days_to_resolution is None else round(opp.days_to_resolution, 2),
        "shares": opp.shares, "detected_cost": opp.total_cost,
        "detected_net": opp.net_profit,
        "detected_ann_pct": "" if opp.annualised_pct is None else round(opp.annualised_pct, 3),
        "fillable": int(fillable), "fresh_cost": "", "fresh_net": "",
        "fresh_ann_pct": "",
    }
    if not fillable:
        row["verdict"] = "unfillable"
        return row

    fresh_net = opp.shares * opp.payout_per_set - fresh_cost
    ann = annualised_pct(fresh_net / fresh_cost * 100, opp.days_to_resolution) if fresh_cost else None
    row.update(fresh_cost=round(fresh_cost, 4), fresh_net=round(fresh_net, 4),
               fresh_ann_pct="" if ann is None else round(ann, 3))
    if fresh_net <= 0:
        row["verdict"] = "gone"
    elif hurdle_apr > 0 and (ann is None or ann < hurdle_apr):
        row["verdict"] = "shrank"
    else:
        row["verdict"] = "held"
    return row


def append(path: str, row: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists() or p.stat().st_size == 0
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({**row, "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
