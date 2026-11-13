"""
The watchlist: which (group, side) pairs are worth pricing.

Downloading the full event list is the expensive part of a scan and the part
most likely to get the scanner refused by the API. Order books are cheap. So
the event list is fetched occasionally and cached here, and every scan in
between only re-prices the order books of the cached pairs.

If a refresh fails, the previous list is kept and the next attempt is delayed,
doubling each time, so a refusing API is not hammered.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Callable, List, Tuple

from . import config, orderbook


class DiscoveryUnavailable(Exception):
    """No event list could be fetched and there is no cached one to fall back on."""


class Watchlist:
    def __init__(self, refresh_secs: float = config.DISCOVERY_INTERVAL,
                 clock: Callable[[], float] = time.time):
        self.refresh_secs = refresh_secs
        self.clock = clock
        self.pairs: List[Tuple[orderbook.Candidate, str]] = []
        self.fetched_at: float = 0.0
        self.next_try: float = 0.0
        self.failures = 0

    # -- timing -------------------------------------------------------------
    def due(self) -> bool:
        return self.clock() >= self.next_try

    def seconds_until_due(self) -> float:
        return max(0.0, self.next_try - self.clock())

    def age_minutes(self) -> float:
        return (self.clock() - self.fetched_at) / 60 if self.fetched_at else 0.0

    # -- building -----------------------------------------------------------
    @staticmethod
    def build_pairs(events: List[dict]):
        """Complete negRisk groups that are near enough to parity to watch."""
        reasons: Counter = Counter()
        pairs = []
        slack = config.PREFILTER_SLACK
        for ev in events:
            cand, reason = orderbook.select_complete_group(ev)
            if cand is None:
                reasons[reason] += 1
                continue
            asks = orderbook.gamma_ask_sum(cand.markets)
            bids = orderbook.gamma_bid_sum(cand.markets)
            want_yes = asks is None or asks <= 1.0 + slack
            # A leg with no YES bid has no NO offers, so the NO set cannot be
            # completed. Only watch the NO side when every leg is quoted.
            want_no = bids is not None and bids >= 1.0 - slack
            if want_yes:
                pairs.append((cand, "YES"))
            if want_no:
                pairs.append((cand, "NO"))
            if not (want_yes or want_no):
                reasons["far_from_parity"] += 1
        return pairs, reasons

    def refresh(self, log) -> None:
        """Refetch the event list if due. Raises DiscoveryUnavailable only when
        the fetch fails and nothing is cached."""
        if not self.due():
            return
        started = self.clock()
        try:
            events = orderbook.discover_events()
        except Exception as e:  # noqa: BLE001
            if orderbook.is_network_error(e):
                # Our connection is down (e.g. the Mac slept). Retrying does
                # not load the API, so try again at the normal scan interval.
                wait = config.SCAN_INTERVAL
            else:
                self.failures += 1
                wait = min(config.BACKOFF_BASE * 2 ** (self.failures - 1),
                           config.BACKOFF_MAX)
            self.next_try = self.clock() + wait
            if not self.pairs:
                raise DiscoveryUnavailable(
                    f"event list unavailable ({e!r}); next attempt in "
                    f"{wait / 60:.0f} min") from e
            log.info("  event list refresh failed (%r); keeping the list from "
                     "%.0f min ago, next attempt in %.0f min.",
                     e, self.age_minutes(), wait / 60)
            return

        self.pairs, reasons = self.build_pairs(events)
        self.failures = 0
        self.fetched_at = self.clock()
        self.next_try = self.fetched_at + self.refresh_secs
        log.info("  event list refreshed in %.0fs: %d events -> %d group/side "
                 "pairs to watch", self.fetched_at - started, len(events),
                 len(self.pairs))
        if len(events) >= config.MAX_EVENTS:
            log.info("  event cap reached (%d); raise PMA_MAX_EVENTS to see more.",
                     config.MAX_EVENTS)
        log.info("  skipped: %s", dict(reasons) or "none")
