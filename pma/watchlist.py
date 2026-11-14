"""
The watchlist: which (group, side) pairs are worth pricing.

Downloading the full event list is the expensive part of a scan and the part
most likely to get the scanner refused by the API. Order books are cheap. So
the event list is fetched occasionally and cached here, and every scan in
between only re-prices the order books of the cached pairs.

Once a watchlist exists, refreshes run in a background thread so scans keep
pricing the current list while the new one downloads.

If a refresh fails, or returns far fewer events than the last good one (a
download cut short), the previous list is kept and the next attempt is
delayed, so a refusing API is not hammered.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Callable, List, Tuple

from . import config, orderbook


class DiscoveryUnavailable(Exception):
    """No event list could be fetched and there is no cached one to fall back on."""


class Watchlist:
    def __init__(self, refresh_secs: float = config.DISCOVERY_INTERVAL,
                 clock: Callable[[], float] = time.time, background: bool = True):
        self.refresh_secs = refresh_secs
        self.clock = clock
        self.background = background
        self.pairs: List[Tuple[orderbook.Candidate, str]] = []
        self.fetched_at: float = 0.0
        self.next_try: float = 0.0
        self.failures = 0
        self.last_event_count = 0
        self._thread: threading.Thread | None = None

    def refreshing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait_for_refresh(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

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
        """Refetch the event list if due.

        With no watchlist yet, this blocks until the first list arrives and
        raises DiscoveryUnavailable if it cannot be fetched. Afterwards it
        starts a background download and returns at once.
        """
        if not self.due() or self.refreshing():
            return
        if self.background and self.pairs:
            def run():
                try:
                    self._refresh_now(log)
                except Exception as e:  # noqa: BLE001
                    log.info("  background event-list refresh crashed: %r", e)
            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            log.info("  refreshing the event list in the background; still "
                     "pricing the current list of %d pairs.", len(self.pairs))
            return
        self._refresh_now(log)

    def _fail(self, log, e, network: bool) -> None:
        if network:
            # Our connection is down (e.g. the Mac slept). Retrying does not
            # load the API, so try again at the normal scan interval.
            wait = config.SCAN_INTERVAL
        else:
            self.failures += 1
            wait = min(config.BACKOFF_BASE * 2 ** (self.failures - 1),
                       config.BACKOFF_MAX)
        self.next_try = self.clock() + wait
        if not self.pairs:
            raise DiscoveryUnavailable(
                f"event list unavailable ({e!r}); next attempt in "
                f"{wait / 60:.0f} min") from (e if isinstance(e, BaseException) else None)
        log.info("  event list refresh failed (%s); keeping the list from "
                 "%.0f min ago, next attempt in %.0f min.",
                 e if isinstance(e, str) else repr(e), self.age_minutes(), wait / 60)

    def _refresh_now(self, log) -> None:
        started = self.clock()
        try:
            events = orderbook.discover_events()
        except Exception as e:  # noqa: BLE001
            self._fail(log, e, orderbook.is_network_error(e))
            return

        # A download cut short (e.g. by sleep) returns far fewer events than
        # the last good one. Do not let it shrink the watchlist.
        if self.pairs and len(events) < 0.8 * self.last_event_count:
            self._fail(log, f"only {len(events)} events, last good list had "
                            f"{self.last_event_count}", network=True)
            return

        pairs, reasons = self.build_pairs(events)
        self.pairs = pairs             # single assignment: safe for the scan thread
        self.last_event_count = len(events)
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
