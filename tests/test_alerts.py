"""
Tests for the fill test and alerts (v2.4). No network needed.

Run with:  python -m pytest -q   (or: python tests/test_alerts.py)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pma import alerts, filltest  # noqa: E402
from pma.arbitrage import Leg, annualised_pct, evaluate_group  # noqa: E402


def make_opp(days=30.0):
    legs = [Leg("A", "A", [(0.30, 1000)]), Leg("B", "B", [(0.66, 1000)])]
    opp = evaluate_group("demo-event", legs, per_position_max=48)
    opp.days_to_resolution = days
    opp.annualised_pct = annualised_pct(opp.margin_pct, days)
    opp.has_catch_all = True
    return opp


def run(opp, books, hurdle=8):
    return filltest.recheck(opp, delay=5, fetch_books=lambda toks: books,
                            sleep=lambda s: None, hurdle_apr=hurdle)


# --- fill test -------------------------------------------------------------
def test_fill_held_when_books_unchanged():
    opp = make_opp()
    row = run(opp, {"A": [(0.30, 1000)], "B": [(0.66, 1000)]})
    assert row["verdict"] == "held" and row["fillable"] == 1
    assert abs(row["fresh_net"] - opp.net_profit) < 1e-3


def test_fill_shrank_gone_and_unfillable():
    opp = make_opp(days=30)
    # Gap narrows from 4% to ~0.4%: still profitable, ~5%/yr, below 8%.
    assert run(opp, {"A": [(0.30, 1000)], "B": [(0.696, 1000)]})["verdict"] == "shrank"
    # Asks now sum above $1.
    assert run(opp, {"A": [(0.35, 1000)], "B": [(0.66, 1000)]})["verdict"] == "gone"
    # Depth on one leg has gone.
    row = run(opp, {"A": [(0.30, 3)], "B": [(0.66, 1000)]})
    assert row["verdict"] == "unfillable" and row["fresh_net"] == ""
    assert run(opp, {"A": [(0.30, 1000)]})["verdict"] == "unfillable"


def test_fill_includes_fees():
    legs = [Leg("A", "A", [(0.49, 1000)], fee_rate=0.04),
            Leg("B", "B", [(0.47, 1000)], fee_rate=0.04)]
    opp = evaluate_group("fees", legs, per_position_max=48)
    opp.days_to_resolution, opp.annualised_pct = 30, annualised_pct(opp.margin_pct, 30)
    row = run(opp, {"A": [(0.49, 1000)], "B": [(0.47, 1000)]})
    # Same books: the fresh net equals the fee-inclusive detected net.
    assert abs(row["fresh_net"] - opp.net_profit) < 1e-3


# --- episodes --------------------------------------------------------------
def test_episodes_alert_once_then_report_close_and_cooldown():
    clock = {"t": 0.0}
    ep = alerts.Episodes(cooldown=1800, clock=lambda: clock["t"])
    opp = make_opp()
    new, ended = ep.update([opp])
    assert new == [opp] and ended == []
    clock["t"] = 60
    new, ended = ep.update([opp])            # still open: no second alert
    assert new == [] and ended == []
    clock["t"] = 600
    new, ended = ep.update([])               # closed after 10 minutes
    assert new == [] and ended == [(("demo-event", "YES"), 10.0)]
    clock["t"] = 900
    new, _ = ep.update([opp])                # reopens within cooldown: no alert
    assert new == [] and ("demo-event", "YES") in ep.active
    clock["t"] = 3000
    ep.update([])
    new, _ = ep.update([opp])                # after cooldown: alerts again
    assert new == [opp]


# --- notifier --------------------------------------------------------------
def test_notifier_disabled_by_default_off_mac():
    n = alerts.Notifier(topic="", mac=True, platform="linux")
    assert not n.enabled and n.channels() == "none"
    n.send("t", "m")                          # no-op, no error


def test_notifier_posts_to_ntfy_and_mac():
    sent, ran = [], []

    class Resp:
        def close(self):
            pass

    def opener(req, timeout):
        sent.append(req)
        return Resp()

    n = alerts.Notifier(topic="pma-secret-123", mac=True, platform="darwin",
                        opener=opener, run=lambda *a, **k: ran.append(a[0]))
    title, msg, click = alerts.describe(make_opp(), {"delay_s": 5, "verdict": "held"})
    n.send(title, msg, click)
    req = sent[0]
    assert req.full_url == "https://ntfy.sh/pma-secret-123"
    assert req.get_method() == "POST" and req.data.decode() == msg
    assert req.get_header("Title").startswith("PMA gap: demo-event")
    assert req.get_header("Click") == "https://polymarket.com/event/demo-event"
    assert "5s re-check: held" in msg
    assert ran and ran[0][0] == "osascript"


def test_notifier_never_raises():
    class Log:
        def __init__(self):
            self.lines = []

        def warning(self, msg, *args):
            self.lines.append(msg % args)

    log = Log()

    def boom(*a, **k):
        raise OSError("offline")

    n = alerts.Notifier(topic="x", mac=True, platform="darwin", log=log,
                        opener=boom, run=boom)
    n.send("t", "m")
    assert len(log.lines) == 2


def test_describe_warns_without_catch_all():
    opp = make_opp()
    opp.has_catch_all = False
    _, msg, _ = alerts.describe(opp)
    assert "No catch-all" in msg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests passed.")
