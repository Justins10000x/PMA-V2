"""
Data layer for PMA v2.1.

  discovery (Gamma /events) -> each event arrives with ALL of its markets
                               nested, so we can check that a group is complete
                               before treating it as "every outcome".

  order books (CLOB /books) -> real asks WITH SIZES, fetched in batches.

V2.0 bug fixed here: discovery used to page through the first N *markets* and
group them by event. If some of an event's legs fell outside that window, the
scanner saw a partial set, and a partial set summing below $1 looks exactly
like an arbitrage while guaranteeing nothing. V2.1 works per event and rejects
any group it cannot show to be complete.

Only read endpoints live here; nothing in this file moves money.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import config
from .arbitrage import AskLevel, Leg

_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "PMA-v2 (research scanner)",
}


def _fetch(url: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _as_list(value) -> list:
    """Gamma returns some arrays as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------
def discover_events(max_events: int = config.MAX_EVENTS) -> List[dict]:
    """Page through open events. Each event carries its full `markets` list."""
    events, offset = [], 0
    while len(events) < max_events:
        params = urllib.parse.urlencode(
            {"active": "true", "closed": "false", "limit": 100, "offset": offset}
        )
        batch = _fetch(f"{config.GAMMA_API}/events?{params}")
        batch = batch if isinstance(batch, list) else batch.get("events", [])
        if not batch:
            break
        events.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
        time.sleep(0.2)
    return events


def yes_token_id(market: dict) -> Optional[str]:
    """Token id of the YES outcome, matched by label rather than position."""
    token_ids = _as_list(market.get("clobTokenIds"))
    outcomes = _as_list(market.get("outcomes"))
    if not token_ids:
        return None
    for tid, label in zip(token_ids, outcomes):
        if str(label).strip().lower() == "yes":
            return str(tid)
    return str(token_ids[0])


def leg_title(market: dict) -> str:
    return str(market.get("groupItemTitle") or market.get("question") or "?")[:60]


# Words that suggest a leg covering "anything not listed". A heuristic only:
# it can miss a catch-all, and numeric bracket sets are exhaustive without one.
_CATCH_ALL = re.compile(
    r"\b(other|another|others|none|no one|nobody|neither|field|any other)\b"
    r"|^no\b",
    re.IGNORECASE,
)


def has_catch_all(titles: List[str]) -> bool:
    return any(_CATCH_ALL.search(t) for t in titles)


def _parse_date(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def days_to_resolution(event: dict, markets: List[dict],
                       now: Optional[datetime] = None) -> Optional[float]:
    """Days until the latest listed end date; None if unknown or already past.

    The end date is the scheduled one. Markets can resolve later than this, so
    treat the annualised figure built on it as optimistic.
    """
    now = now or datetime.now(timezone.utc)
    dates = [_parse_date(event.get("endDate"))]
    dates += [_parse_date(m.get("endDate")) for m in markets]
    dates = [d for d in dates if d]
    if not dates:
        return None
    days = (max(dates) - now).total_seconds() / 86400
    if days <= 0:
        return None
    return max(days, 1.0)


@dataclass
class Candidate:
    slug: str
    title: str
    markets: List[dict]            # every open leg of the event
    days: Optional[float]
    catch_all: bool


def select_complete_group(event: dict) -> Tuple[Optional[Candidate], str]:
    """Return (candidate, "ok") or (None, reason the event was rejected)."""
    markets = [m for m in _as_list(event.get("markets")) if isinstance(m, dict)]
    neg_risk = bool(event.get("negRisk")) or (
        bool(markets) and all(m.get("negRisk") for m in markets)
    )
    if not neg_risk:
        return None, "not_negrisk"

    # Augmented events hold unnamed placeholder outcomes whose meaning changes
    # over time, so "all named outcomes" is not a complete set.
    if event.get("negRiskAugmented") or event.get("enableNegRisk") is False:
        return None, "augmented_or_disabled"

    open_markets = [m for m in markets if not m.get("closed")]
    if len(open_markets) < 2:
        return None, "too_few_legs"
    if len(open_markets) > config.MAX_LEGS:
        return None, "too_many_legs"

    # Every open leg must be buyable, or the set cannot be completed.
    for m in open_markets:
        if (
            m.get("active") is False
            or m.get("acceptingOrders") is False
            or m.get("enableOrderBook") is False
            or not yes_token_id(m)
        ):
            return None, "untradable_leg"

    slug = str(event.get("slug") or event.get("ticker") or event.get("id"))
    return (
        Candidate(
            slug=slug,
            title=str(event.get("title") or slug),
            markets=open_markets,
            days=days_to_resolution(event, open_markets),
            catch_all=has_catch_all([leg_title(m) for m in open_markets]),
        ),
        "ok",
    )


def gamma_ask_sum(markets: List[dict]) -> Optional[float]:
    """Sum of Gamma's quoted best asks, or None if any leg has no quote.

    Used only to skip groups that are clearly above $1 before spending
    order-book requests on them. Never used to decide a trade.
    """
    total = 0.0
    for m in markets:
        try:
            ask = float(m.get("bestAsk"))
        except (TypeError, ValueError):
            return None
        if ask <= 0:
            return None
        total += ask
    return total


# ---------------------------------------------------------------------------
# ORDER BOOKS (with depth)
# ---------------------------------------------------------------------------
def _parse_asks(book: dict) -> List[AskLevel]:
    asks = []
    for level in book.get("asks") or []:
        try:
            price, size = float(level["price"]), float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < price <= 1 and size > 0:
            asks.append((price, size))
    asks.sort(key=lambda x: x[0])
    return asks


def fetch_ask_book(token_id: str) -> List[AskLevel]:
    """Live ask book for one token as [(price, size), ...] ascending."""
    url = f"{config.CLOB_API}/book?token_id={urllib.parse.quote(str(token_id))}"
    return _parse_asks(_fetch(url))


def fetch_ask_books(token_ids: List[str], chunk: int = 50) -> Dict[str, List[AskLevel]]:
    """Ask books for many tokens. Batches via POST /books; falls back to
    one request per token if the batch call fails."""
    books: Dict[str, List[AskLevel]] = {}
    ids = list(dict.fromkeys(str(t) for t in token_ids))
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        try:
            resp = _fetch(f"{config.CLOB_API}/books",
                          body=[{"token_id": t} for t in part])
            for book in resp if isinstance(resp, list) else []:
                tid = str(book.get("asset_id") or book.get("token_id") or "")
                if tid:
                    books[tid] = _parse_asks(book)
        except Exception:  # noqa: BLE001 - fall back to single fetches below
            pass
        for t in part:
            if t not in books:
                try:
                    books[t] = fetch_ask_book(t)
                except Exception:  # noqa: BLE001
                    books[t] = []
                time.sleep(0.05)
    return books


def build_legs(candidate: Candidate, books: Dict[str, List[AskLevel]]) -> List[Leg]:
    """Priced legs for a candidate; [] if any leg has no offers."""
    legs: List[Leg] = []
    for m in candidate.markets:
        token_id = yes_token_id(m)
        asks = books.get(token_id or "", [])
        if not asks:
            return []
        legs.append(Leg(token_id=token_id, outcome=leg_title(m), asks=asks))
    return legs
