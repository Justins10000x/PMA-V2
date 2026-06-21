"""
Data layer for PMA v2.

Two responsibilities, kept deliberately separate from the arbitrage math:

  discovery (Gamma API)   -> WHICH markets exist, which are negRisk (genuinely
                             mutually exclusive), how they group into events,
                             and the token id of each outcome's YES share.

  order book (CLOB API)   -> the REAL bids/asks WITH SIZES for a token. V1 used
                             Gamma's aggregated bestBid/bestAsk (a single,
                             possibly stale number with no depth). V2 reads the
                             live CLOB book so it can walk depth and verify a
                             trade can actually fill before committing.

Only read endpoints live here; nothing in this file moves money.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Dict, List

from . import config
from .arbitrage import Leg

_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "PMA-v2 (research arbitrage scanner)",
}


def _fetch(url: str):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------
def _markets_batch(limit: int, offset: int) -> List[dict]:
    params = urllib.parse.urlencode(
        {"active": "true", "closed": "false", "limit": limit, "offset": offset}
    )
    data = _fetch(f"{config.GAMMA_API}/markets?{params}")
    return data if isinstance(data, list) else data.get("markets", [])


def discover_markets(max_markets: int = config.MAX_MARKETS) -> List[dict]:
    markets, offset = [], 0
    while len(markets) < max_markets:
        batch = _markets_batch(100, offset)
        if not batch:
            break
        markets.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
        time.sleep(0.25)
    return markets


def _yes_token_id(market: dict):
    """Return the token id for the YES outcome.

    V1 blindly took clobTokenIds[0]. V2 pairs token ids with their outcome
    labels and selects YES explicitly, falling back to index 0 only if labels
    are missing.
    """
    token_ids = market.get("clobTokenIds", "[]")
    outcomes = market.get("outcomes", "[]")
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if not token_ids:
        return None
    for tid, label in zip(token_ids, outcomes):
        if str(label).strip().lower() == "yes":
            return tid
    return token_ids[0]


def group_negrisk_events(markets: List[dict]) -> Dict[str, List[dict]]:
    """Group markets into mutually-exclusive events, keeping only negRisk legs.

    negRisk is Polymarket's flag for genuinely exhaustive, mutually-exclusive
    categorical outcomes — the only structure where "buy every outcome" is a
    valid arbitrage.
    """
    groups: Dict[str, List[dict]] = defaultdict(list)
    for m in markets:
        if not m.get("negRisk"):
            continue
        events = m.get("events", [])
        ticker = events[0].get("ticker") if events else m.get("slug", "unknown")
        groups[ticker].append(m)
    return {k: v for k, v in groups.items() if 2 <= len(v) <= config.MAX_LEGS}


# ---------------------------------------------------------------------------
# ORDER BOOK (with depth)
# ---------------------------------------------------------------------------
def fetch_ask_book(token_id: str) -> List[tuple]:
    """Fetch the live ask book for a token as [(price, size), ...] ascending.

    The CLOB returns prices as strings; we coerce to float and sort so the
    arbitrage walker can rely on ascending order.
    """
    url = f"{config.CLOB_API}/book?token_id={urllib.parse.quote(str(token_id))}"
    data = _fetch(url)
    asks = []
    for level in data.get("asks", []):
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price <= 1 and size > 0:
            asks.append((price, size))
    asks.sort(key=lambda x: x[0])
    return asks


def build_legs(group: List[dict]) -> List[Leg]:
    """Turn a discovered negRisk group into priced Legs with live ask books."""
    legs: List[Leg] = []
    for m in group:
        token_id = _yes_token_id(m)
        if not token_id:
            continue
        asks = fetch_ask_book(token_id)
        if not asks:
            return []  # a leg with no offers means the set can't be completed
        legs.append(
            Leg(
                token_id=str(token_id),
                outcome=m.get("groupItemTitle") or m.get("question", "?")[:60],
                asks=asks,
            )
        )
    return legs
