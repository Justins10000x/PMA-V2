#!/usr/bin/env python3
"""
PMA v2.1 - main loop.

Each scan:
    discover events with all their markets (Gamma)
      -> keep only negRisk groups that are provably complete
        -> skip groups whose quoted asks are clearly above $1
          -> fetch real ask books in batches (CLOB)
            -> evaluate: ask-side, equal shares, depth-aware   (pure, tested)
              -> annualise against the lock-up, compare with the hurdle
                -> human approval gate
                  -> FOK execution with unwind on partial fills

Modes (set PMA_MODE): scan (default) | paper | live.

    python run.py --once                # one scan, then exit
    PMA_MODE=scan  python run.py        # read-only loop, never trades
    PMA_MODE=paper python run.py        # simulated fills vs the real book
    PMA_MODE=live  POLYMARKET_PRIVATE_KEY=0x... python run.py
"""

import argparse
import logging
import time
from collections import Counter
from pathlib import Path

from pma import config, control, execution, orderbook
from pma.arbitrage import annualised_pct, evaluate_group
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


def find_opportunities(session, log):
    """Return opportunities sorted best-first (by annualised return)."""
    events = orderbook.discover_events()
    reasons = Counter()
    candidates = []
    for ev in events:
        cand, reason = orderbook.select_complete_group(ev)
        if cand is None:
            reasons[reason] += 1
            continue
        quoted = orderbook.gamma_ask_sum(cand.markets)
        if quoted is not None and quoted > 1.0 + config.PREFILTER_SLACK:
            reasons["asks_above_1"] += 1
            continue
        candidates.append(cand)

    log.info("  %d events -> %d complete negRisk groups worth pricing",
             len(events), len(candidates))
    log.info("  skipped: %s", dict(reasons) or "none")

    tokens = [orderbook.yes_token_id(m) for c in candidates for m in c.markets]
    books = orderbook.fetch_ask_books(tokens)

    headroom = config.MAX_EXPOSURE - session.committed
    cap = min(config.PER_POSITION_MAX, headroom)
    if cap <= 0:
        log.info("  Exposure limit reached ($%.2f committed).", session.committed)
        return []

    found = []
    for cand in candidates:
        legs = orderbook.build_legs(cand, books)
        if len(legs) < 2:
            continue
        opp = evaluate_group(cand.slug, legs, per_position_max=cap,
                             min_net_profit=config.MIN_NET_PROFIT)
        if not opp:
            continue
        opp.days_to_resolution = cand.days
        opp.annualised_pct = annualised_pct(opp.margin_pct, cand.days)
        opp.has_catch_all = cand.catch_all
        found.append(opp)

    found.sort(key=lambda o: (o.annualised_pct is not None, o.annualised_pct or 0),
               reverse=True)
    return found


def scan_once(session, client, log):
    opportunities = find_opportunities(session, log)
    if not opportunities:
        log.info("  No ask-side gap below $1 this scan.")
        return

    for opp in opportunities:
        print(control.render(opp, config.MODE, config.HURDLE_APR))
        if config.MODE == "scan" or not opp.beats_hurdle(config.HURDLE_APR):
            continue
        if opp.total_cost > config.MAX_EXPOSURE - session.committed:
            log.info("  skipping: not enough exposure headroom left.")
            continue
        if control.request_approval(opp, config.MODE, config.REQUIRE_APPROVAL):
            result = execution.execute(opp, client, paper=(config.MODE == "paper"))
            session.record(result)
            log.info("  result: %s  session P&L: $%.4f  committed: $%.2f",
                     result["status"], session.net, session.committed)
            if session.breached_circuit_breaker():
                log.error("  CIRCUIT BREAKER: session loss limit hit - halting.")
                raise SystemExit(1)
        else:
            log.info("  declined by operator - skipping.")


def main():
    parser = argparse.ArgumentParser(description="PMA v2.1 scanner")
    parser.add_argument("--once", action="store_true",
                        help="run a single scan and exit")
    args = parser.parse_args()

    config.validate()
    log = setup_logging()
    log.info("=" * 60)
    log.info("PMA v2.1 - MODE=%s  approval=%s", config.MODE.upper(),
             config.REQUIRE_APPROVAL)
    log.info("Portfolio $%.0f | max exposure $%.0f | per-position $%.0f | "
             "min gap $%.2f | hurdle %g%% a year", config.PORTFOLIO_SIZE,
             config.MAX_EXPOSURE, config.PER_POSITION_MAX,
             config.MIN_NET_PROFIT, config.HURDLE_APR)
    if config.MODE == "scan":
        log.info("SCAN MODE: read-only, no orders will ever be placed.")
    log.info("=" * 60)

    session = Session.load()
    client = execution.load_client() if config.MODE == "live" else None
    reconcile(client)

    try:
        while True:
            log.info("scanning... (session P&L $%.4f)", session.net)
            started = time.time()
            try:
                scan_once(session, client, log)
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                log.error("  scan error: %r", e)
            log.info("  scan took %.1fs", time.time() - started)
            if args.once:
                break
            time.sleep(config.SCAN_INTERVAL)
    except KeyboardInterrupt:
        log.info("Stopped by user. Session P&L: $%.4f over %d trade(s).",
                 session.net, session.trades)
        session.save()


if __name__ == "__main__":
    main()
