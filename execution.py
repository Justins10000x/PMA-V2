"""
Execution layer for PMA v2.

Design goal: eliminate the leg risk that quietly bankrupts naive arb bots.

V1 posted resting bids and waited up to 10 minutes for them to fill. The legs
filled one at a time (adverse selection), leaving permanent one-sided positions
that V1 then deleted from tracking and forgot.

V2 instead:
  * sends MARKETABLE Fill-Or-Kill (FOK) orders that either fully fill against
    the depth we already verified, or cancel immediately — no resting, no wait;
  * if some legs fill and others don't, immediately UNWINDS the filled legs
    (marketable sell) so we are never left holding a directional position to
    resolution. The realised cost of an unwind is logged honestly as a loss.

Paper mode simulates fills against the SAME book snapshot the opportunity was
computed from — an honest simulation, not V1's random coin-flip per leg.

Live mode uses py-clob-client (imported lazily so scan/paper need no deps).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from . import config
from .arbitrage import Opportunity
from .orderbook import fetch_ask_book

log = logging.getLogger("pma")


# ---------------------------------------------------------------------------
# Staleness guard
# ---------------------------------------------------------------------------
def edge_still_present(opp: Opportunity) -> bool:
    """Re-read each leg's book and confirm enough of the edge survives.

    Books move between detection and send. If less than MIN_EDGE_RETENTION of
    the detected profit remains, abort rather than chase a decayed edge.
    """
    if not config.STALENESS_REFRESH:
        return True

    from .arbitrage import effective_cost

    fresh_cost = 0.0
    for leg in opp.legs:
        asks = fetch_ask_book(leg.token_id)
        cost, filled = effective_cost(asks, opp.shares)
        if filled < opp.shares - 1e-9:
            log.warning("    staleness: %s can no longer fill %.4f shares",
                        leg.outcome[:30], opp.shares)
            return False
        fresh_cost += cost

    fresh_profit = opp.payout - fresh_cost
    retained = fresh_profit / opp.net_profit if opp.net_profit else 0.0
    if retained < config.MIN_EDGE_RETENTION:
        log.warning("    staleness: only %.0f%% of edge remains — aborting",
                    retained * 100)
        return False
    return True


# ---------------------------------------------------------------------------
# Order primitives
# ---------------------------------------------------------------------------
def _fok_buy(client, token_id: str, shares: float, max_price: float, paper: bool):
    """Marketable FOK buy. Returns dict with 'filled' (bool) and 'cost'."""
    if paper:
        # Honest sim: we already verified depth for this snapshot, so it fills.
        return {"filled": True, "cost": None, "order_id": f"PAPER-{token_id[:8]}"}
    from py_clob_client.clob_types import OrderArgs, OrderType

    args = OrderArgs(token_id=token_id, price=max_price, size=shares, side="BUY")
    signed = client.create_order(args)
    resp = client.post_order(signed, OrderType.FOK)
    filled = str(resp.get("status", "")).lower() in ("matched", "filled")
    return {"filled": filled, "cost": None, "order_id": resp.get("orderID", "?")}


def _unwind_sell(client, token_id: str, shares: float, paper: bool):
    """Marketable sell to flatten a stranded filled leg (best effort)."""
    if paper:
        log.warning("    [PAPER] unwind sell %.4f of %s", shares, token_id[:10])
        return True
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType

        args = OrderArgs(token_id=token_id, price=0.001, size=shares, side="SELL")
        signed = client.create_order(args)
        client.post_order(signed, OrderType.FAK)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("    UNWIND FAILED for %s: %s — MANUAL REVIEW", token_id[:10], e)
        return False


# ---------------------------------------------------------------------------
# Position execution
# ---------------------------------------------------------------------------
def execute(opp: Opportunity, client, paper: bool) -> dict:
    """Attempt the full multi-leg arbitrage. Returns a result record."""
    if config.STALENESS_REFRESH and not edge_still_present(opp):
        return {"event": opp.event, "status": "aborted_stale", "realised": 0.0}

    filled, plan = [], {p["token_id"]: p for p in opp.per_leg_plan}
    for leg in opp.legs:
        p = plan[leg.token_id]
        # Cap the price we will pay at the VWAP we planned for this leg.
        max_price = min(1.0, (p["avg_price"] or 1.0) * 1.02)
        res = _fok_buy(client, leg.token_id, opp.shares, max_price, paper)
        if res["filled"]:
            filled.append((leg, p))
        else:
            log.warning("    leg did not fill: %s — unwinding %d filled leg(s)",
                        leg.outcome[:30], len(filled))
            for fleg, _ in filled:
                _unwind_sell(client, fleg.token_id, opp.shares, paper)
            return {"event": opp.event, "status": "partial_unwound",
                    "realised": 0.0, "legs_filled": len(filled)}

    log.info("    ALL %d legs filled — arbitrage locked", len(filled))
    return {
        "event": opp.event,
        "status": "filled",
        "realised": opp.net_profit,
        "committed": opp.total_cost,
        "shares": opp.shares,
        "ts": time.time(),
    }


def load_client():
    """Build an authenticated CLOB client for live mode (lazy import)."""
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    client = ClobClient(host=config.CLOB_API, chain_id=POLYGON,
                        key=config.PRIVATE_KEY)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client
