"""
Alerts: tell you when a gap clears the hurdle, while it is still live.

Two channels, both optional:
  phone  via ntfy (https://ntfy.sh), a free push service. Install the ntfy app,
         subscribe to a hard-to-guess topic name, and set PMA_NTFY_TOPIC to it.
         Anyone who knows the topic name can read the alerts, so treat it like
         a password.
  Mac    a desktop notification (macOS only; PMA_MAC_NOTIFY=false to turn off).

One alert per episode: a group that stays above the hurdle scan after scan
alerts once, and the same group will not alert again within ALERT_COOLDOWN.
Sending never raises; failures are logged and the scan carries on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Tuple

from . import config


class Notifier:
    def __init__(self, topic: str = config.NTFY_TOPIC, mac: bool = config.MAC_NOTIFY,
                 log=None, opener: Callable = urllib.request.urlopen,
                 platform: str = sys.platform, run: Callable = subprocess.run):
        self.topic = topic.strip()
        self.mac = mac and platform == "darwin"
        self.log = log
        self.opener = opener
        self.run = run

    @property
    def enabled(self) -> bool:
        return bool(self.topic) or self.mac

    def channels(self) -> str:
        names = (["phone (ntfy)"] if self.topic else []) + (["Mac"] if self.mac else [])
        return ", ".join(names) or "none"

    def send(self, title: str, message: str, click: str = "") -> None:
        if self.topic:
            try:
                headers = {"Title": title.encode("ascii", "replace").decode(),
                           "Priority": "4", "Tags": "chart_with_upwards_trend"}
                if click:
                    headers["Click"] = click
                req = urllib.request.Request(
                    f"https://ntfy.sh/{urllib.parse.quote(self.topic)}",
                    data=message.encode("utf-8"), headers=headers, method="POST")
                self.opener(req, timeout=10).close()
            except Exception as e:  # noqa: BLE001
                self._warn("phone alert failed: %r", e)
        if self.mac:
            script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
                      f"with title {json.dumps(title, ensure_ascii=False)}")
            try:
                self.run(["osascript", "-e", script], timeout=5,
                         capture_output=True, check=False)
            except Exception as e:  # noqa: BLE001
                self._warn("Mac alert failed: %r", e)

    def _warn(self, msg, *args):
        if self.log:
            self.log.warning("  " + msg, *args)


class Episodes:
    """Tracks which groups are above the hurdle, scan to scan."""

    def __init__(self, cooldown: float = config.ALERT_COOLDOWN,
                 clock: Callable[[], float] = time.time):
        self.cooldown = cooldown
        self.clock = clock
        self.active: Dict[Tuple[str, str], float] = {}     # key -> start time
        self.last_alert: Dict[Tuple[str, str], float] = {}

    def update(self, above: List) -> Tuple[List, List[Tuple[Tuple[str, str], float]]]:
        """Return (new opportunities to alert on, [(key, minutes) of episodes that ended])."""
        now = self.clock()
        current = {(o.event, o.side): o for o in above}
        ended = [(k, (now - start) / 60) for k, start in self.active.items()
                 if k not in current]
        for k, _ in ended:
            del self.active[k]
        new = []
        for k, opp in current.items():
            if k in self.active:
                continue
            self.active[k] = now
            if now - self.last_alert.get(k, -1e18) >= self.cooldown:
                self.last_alert[k] = now
                new.append(opp)
        return new, ended


def describe(opp, fill: dict = None) -> Tuple[str, str, str]:
    """(title, message, click URL) for one opportunity."""
    title = f"PMA gap: {opp.event[:60]}"
    ann = "unknown" if opp.annualised_pct is None else f"{opp.annualised_pct:.0f}%/yr"
    days = "" if opp.days_to_resolution is None else f", resolves in {opp.days_to_resolution:.0f}d"
    msg = (f"Buy every {opp.side}: {opp.margin_pct:.2f}% after fees ({ann}{days}). "
           f"${opp.net_profit:.2f} on ${opp.total_cost:.2f}.")
    if fill:
        msg += f" {fill['delay_s']:g}s re-check: {fill['verdict']}."
    if opp.side == "YES" and not opp.has_catch_all:
        msg += " No catch-all outcome detected: check the rules."
    click = f"https://polymarket.com/event/{opp.event}"
    return title, msg, click
