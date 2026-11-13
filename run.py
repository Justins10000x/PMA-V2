#!/usr/bin/env python3
"""
PMA v2.3 - main loop.

Every 30 minutes (PMA_DISCOVERY_MINS):
    download the event list (Gamma) and rebuild the watchlist: negRisk
    groups that are provably complete and quoted near parity

Each scan:
          fetch real ask books for the watchlist in batches (CLOB)
            -> price both sides after taker fees: buy every YES (pays $1)
               or buy every NO (pays n-1); equal shares, depth-aware
              -> log every priced group to logs/scans.csv
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

from pma import config, control, execution, orderbook, scanlog
from pma.arbitrage import annualised_pct, evaluate_group, top_of_book
from pma.state import Session, reconcile
from pma.watchlist import DiscoveryUnavailable, Watchlist


def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler()],
    )
    return logging.getLogger("pma")


def find_opportunities(session, log, watch):
    """Re-price every watched pair. Returns gaps, best first."""
    watch.refresh(log)          # only refetches the event list when it is due
    todo = watch.pairs

    tokens = []
    for cand, side in todo:
        pick = orderbook.yes_token_id if side == "YES" else orderbook.no_token_id
        tokens += [pick(m) for m in cand.markets]
    books = orderbook.fetch_ask_books([t for t in tokens if t])

    cap = min(config.PER_POSITION_MAX, config.MAX_EXPOSURE - session.committed)
    if cap <= 0:
        log.info("  Exposure limit reached ($%.2f committed).", session.committed)
        cap = config.PER_POSITION_MAX      # keep scanning and logging anyway

    found, rows = [], []
    unpriced = Counter()
    for cand, side in todo:
        legs = orderbook.build_legs(cand, books, side)
        if len(legs) < 2:
            unpriced[side] += 1
            continue
        payout = 1.0 if side == "YES" else float(len(legs) - 1)
        opp = evaluate_group(cand.slug, legs, per_position_max=cap,
                             min_net_profit=config.MIN_NET_PROFIT,
                             payout_per_set=payout, side=side)
        if opp:
            opp.days_to_resolution = cand.days
            opp.annualised_pct = annualised_pct(opp.margin_pct, cand.days)
            opp.has_catch_all = cand.catch_all
            found.append(opp)
        top = top_of_book(legs, payout)
        if top:
            rows.append(scanlog.make_row(cand, side, payout, top, opp))

    scanlog.append(config.SCAN_LOG, rows)
    if unpriced:
        log.info("  could not price %s: a leg had no offers, so the set "
                 "cannot be completed.", dict(unpriced))
    if rows:
        best = max(rows, key=lambda r: r["edge_pct"])
        log.info("  priced %d of %d pairs; closest to a gap: %+.2f%% (%s, %s)",
                 len(rows), len(todo), best["edge_pct"], best["side"],
                 best["slug"][:40])

    found.sort(key=lambda o: (o.annualised_pct is not None, o.annualised_pct or 0),
               reverse=True)
    return found


def scan_once(session, client, log, watch, verbose=False):
    opportunities = find_opportunities(session, log, watch)
    if not watch.pairs:
        log.info("  Watchlist is empty; nothing to price until the event list loads.")
        return
    if not opportunities:
        log.info("  No gap after fees on either side this scan.")
        return

    for opp in opportunities:
        if not verbose and not opp.beats_hurdle(config.HURDLE_APR):
            ann = ("unknown date" if opp.annualised_pct is None else
                   f"{opp.annualised_pct:.1f}%/yr over {opp.days_to_resolution:.0f}d")
            log.info("  below hurdle: %s %s  gap %.2f%% ($%.2f)  %s",
                     opp.side, opp.event[:44], opp.margin_pct, opp.net_profit, ann)
            continue
        print(control.render(opp, config.MODE, config.HURDLE_APR))
        if (config.MODE == "scan" or opp.side != "YES"
                or not opp.beats_hurdle(config.HURDLE_APR)):
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
    parser = argparse.ArgumentParser(description="PMA v2.3 scanner")
    parser.add_argument("--once", action="store_true",
                        help="run a single scan and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full fill plan for below-hurdle gaps too")
    args = parser.parse_args()

    config.validate()
    log = setup_logging()
    log.info("=" * 60)
    log.info("PMA v2.3 - MODE=%s  approval=%s", config.MODE.upper(),
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

    watch = Watchlist()
    errors = 0
    try:
        while True:
            log.info("scanning... (session P&L $%.4f)", session.net)
            started = time.time()
            wait = config.SCAN_INTERVAL
            try:
                scan_once(session, client, log, watch, args.verbose)
                errors = 0
            except SystemExit:
                raise
            except DiscoveryUnavailable as e:
                log.error("  %s", e)
                # +1s so the next scan is not a hair early and skips the retry.
                wait = max(wait, watch.seconds_until_due() + 1)
            except Exception as e:  # noqa: BLE001
                if orderbook.is_network_error(e):
                    # No connection (e.g. the Mac slept): retry at the normal
                    # interval; this puts no load on the API.
                    log.error("  no network connection (%r); retrying in %.0f s",
                              e, wait)
                else:
                    # Back off when something keeps failing instead of
                    # retrying every minute.
                    errors += 1
                    wait = min(config.SCAN_INTERVAL * 2 ** errors, config.BACKOFF_MAX)
                    log.error("  scan error: %r; next scan in %.0f min", e, wait / 60)
            log.info("  scan took %.1fs", time.time() - started)
            if args.once:
                break
            time.sleep(wait)
    except KeyboardInterrupt:
        log.info("Stopped by user. Session P&L: $%.4f over %d trade(s).",
                 session.net, session.trades)
        session.save()


if __name__ == "__main__":
    main()
