#!/usr/bin/env python3
"""
PMA v2 — main loop.

Pipeline each scan:
    discover negRisk groups (Gamma)
      -> fetch real ask books with depth (CLOB)
        -> evaluate_group: ask-side, equal-share, depth-aware  (pure, tested)
          -> rank by guaranteed profit
            -> human approval gate (Track C)
              -> FOK execution with auto-unwind on partials
                -> honest accounting + circuit breaker

Modes (set PMA_MODE): scan (default) | paper | live.

    PMA_MODE=scan  python run.py        # read-only, never trades
    PMA_MODE=paper python run.py        # simulated fills vs the real book
    PMA_MODE=live  POLYMARKET_PRIVATE_KEY=0x... python run.py
"""

import logging
import time
from pathlib import Path

from pma import config, control, execution, orderbook
from pma.arbitrage import evaluate_group
from pma.state import Session, reconcile


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler()],
    )
    return logging.getLogger("pma")


def scan_once(session, client, log):
    markets = orderbook.discover_markets()
    groups = orderbook.group_negrisk_events(markets)
    log.info("  %d markets -> %d negRisk groups", len(markets), len(groups))

    opportunities = []
    for ticker, group in groups.items():
        legs = orderbook.build_legs(group)
        if len(legs) < 2:
            continue
        opp = evaluate_group(
            ticker, legs,
            per_position_max=min(
                config.PER_POSITION_MAX,
                config.MAX_EXPOSURE - session.realised_profit,  # respect headroom
            ),
            min_net_profit=config.MIN_NET_PROFIT,
        )
        if opp and opp.is_profitable:
            opportunities.append(opp)

    if not opportunities:
        log.info("  No lockable ask-side arbitrage this scan.")
        return

    opportunities.sort(key=lambda o: o.net_profit, reverse=True)
    for opp in opportunities:
        print(control.render(opp, config.MODE))
        if config.MODE == "scan":
            continue
        if control.request_approval(opp, config.MODE, config.REQUIRE_APPROVAL):
            result = execution.execute(opp, client, paper=(config.MODE == "paper"))
            session.record(result)
            log.info("  result: %s  net session P&L: $%.4f",
                     result["status"], session.net)
            if session.breached_circuit_breaker():
                log.error("  CIRCUIT BREAKER: session loss limit hit — halting.")
                raise SystemExit(1)
        else:
            log.info("  declined by operator — skipping.")


def main():
    config.validate()
    log = setup_logging()
    log.info("=" * 60)
    log.info("PMA v2 — MODE=%s  approval=%s", config.MODE.upper(),
             config.REQUIRE_APPROVAL)
    log.info("Portfolio $%.0f | max exposure $%.0f | per-position $%.0f | "
             "min profit $%.2f", config.PORTFOLIO_SIZE, config.MAX_EXPOSURE,
             config.PER_POSITION_MAX, config.MIN_NET_PROFIT)
    if config.MODE == "scan":
        log.info("SCAN MODE: read-only, no orders will ever be placed.")
    log.info("=" * 60)

    session = Session.load()
    client = execution.load_client() if config.MODE == "live" else None
    reconcile(client)

    try:
        while True:
            ts = time.strftime("%H:%M:%S")
            log.info("[%s] scanning... (session net $%.4f)", ts, session.net)
            try:
                scan_once(session, client, log)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("  scan error: %s", e)
            time.sleep(config.SCAN_INTERVAL)
    except KeyboardInterrupt:
        log.info("Stopped by user. Session net P&L: $%.4f over %d trade(s).",
                 session.net, session.trades)
        session.save()


if __name__ == "__main__":
    main()
