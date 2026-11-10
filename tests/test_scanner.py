"""
Tests for the v2.1 scanner layer: group completeness, catch-all detection,
time value, the quote prefilter and order-book parsing. No network needed.

Run with:  python -m pytest -q   (or: python tests/test_scanner.py)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pma import orderbook  # noqa: E402
from pma.arbitrage import Leg, annualised_pct, evaluate_group  # noqa: E402


def market(title, ask=None, **extra):
    m = {
        "groupItemTitle": title,
        "negRisk": True,
        "closed": False,
        "active": True,
        "clobTokenIds": f'["{title}-YES", "{title}-NO"]',
        "outcomes": '["Yes", "No"]',
    }
    if ask is not None:
        m["bestAsk"] = ask
    m.update(extra)
    return m


def event(markets, **extra):
    ev = {"slug": "demo", "title": "Demo", "negRisk": True, "markets": markets}
    ev.update(extra)
    return ev


# --- completeness ----------------------------------------------------------
def test_complete_group_is_accepted():
    cand, reason = orderbook.select_complete_group(
        event([market("Dem"), market("Rep")]))
    assert reason == "ok" and len(cand.markets) == 2


def test_non_negrisk_event_is_rejected():
    ev = event([market("A", negRisk=False), market("B", negRisk=False)],
               negRisk=False)
    assert orderbook.select_complete_group(ev) == (None, "not_negrisk")


def test_untradable_leg_rejects_whole_group():
    # The v2.0 failure mode: a leg we cannot buy means the set is partial.
    ev = event([market("A"), market("B"), market("C", acceptingOrders=False)])
    assert orderbook.select_complete_group(ev) == (None, "untradable_leg")
    ev = event([market("A"), market("B"), market("C", active=False)])
    assert orderbook.select_complete_group(ev) == (None, "untradable_leg")


def test_augmented_event_is_rejected():
    ev = event([market("A"), market("B")], negRiskAugmented=True)
    assert orderbook.select_complete_group(ev) == (None, "augmented_or_disabled")


def test_closed_legs_are_dropped_not_fatal():
    # A leg already resolved (eliminated) no longer needs buying.
    ev = event([market("A"), market("B"), market("Out", closed=True)])
    cand, reason = orderbook.select_complete_group(ev)
    assert reason == "ok" and len(cand.markets) == 2


def test_single_open_leg_is_rejected():
    ev = event([market("A"), market("Out", closed=True)])
    assert orderbook.select_complete_group(ev) == (None, "too_few_legs")


# --- catch-all heuristic ---------------------------------------------------
def test_catch_all_detection():
    assert not orderbook.has_catch_all(["Democrat", "Republican"])
    assert orderbook.has_catch_all(["Democrat", "Republican", "Other"])
    assert orderbook.has_catch_all(["<500B", "1.5T+", "No IPO by December 31"])
    assert not orderbook.has_catch_all(["Norway", "Nottingham Forest"])


# --- yes token -------------------------------------------------------------
def test_yes_token_matched_by_label_not_position():
    m = {"clobTokenIds": '["NO-TOK", "YES-TOK"]', "outcomes": '["No", "Yes"]'}
    assert orderbook.yes_token_id(m) == "YES-TOK"


# --- prefilter -------------------------------------------------------------
def test_gamma_ask_sum():
    assert abs(orderbook.gamma_ask_sum([market("A", 0.55), market("B", 0.5)]) - 1.05) < 1e-9
    assert orderbook.gamma_ask_sum([market("A", 0.55), market("B")]) is None


# --- time value ------------------------------------------------------------
def test_annualised_return():
    # 2.32% locked for 135 days is roughly 6.3% a year.
    assert abs(annualised_pct(2.32, 135) - 6.2726) < 1e-3
    # 1% locked for 11 days is roughly 33% a year.
    assert abs(annualised_pct(1.0, 11) - 33.18) < 1e-2
    assert annualised_pct(2.0, None) is None


def test_hurdle_blocks_slow_gaps_and_unknown_dates():
    legs = [Leg("A", "Yes", [(0.30, 1000)]), Leg("B", "Yes", [(0.68, 1000)])]
    opp = evaluate_group("demo", legs, per_position_max=50)
    opp.annualised_pct = annualised_pct(opp.margin_pct, 180)   # ~4.1%
    assert not opp.beats_hurdle(8)
    opp.annualised_pct = annualised_pct(opp.margin_pct, 10)    # ~74%
    assert opp.beats_hurdle(8)
    opp.annualised_pct = None
    assert not opp.beats_hurdle(8)
    assert opp.beats_hurdle(0)          # hurdle disabled


def test_days_to_resolution():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    ev = {"endDate": (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")}
    assert abs(orderbook.days_to_resolution(ev, [], now) - 30) < 1e-6
    # Past or missing dates are unknown, never a tiny positive number.
    assert orderbook.days_to_resolution({"endDate": "2026-01-01T00:00:00Z"}, [], now) is None
    assert orderbook.days_to_resolution({}, [], now) is None
    # Under a day is floored at 1 so the annualised figure cannot explode.
    soon = {"endDate": (now + timedelta(hours=2)).isoformat()}
    assert orderbook.days_to_resolution(soon, [], now) == 1.0


# --- order books -----------------------------------------------------------
def test_parse_asks_sorts_and_drops_bad_levels():
    book = {"asks": [{"price": "0.40", "size": "10"}, {"price": "0.30", "size": "5"},
                     {"price": "bad", "size": "1"}, {"price": "0.5", "size": "0"}]}
    assert orderbook._parse_asks(book) == [(0.30, 5.0), (0.40, 10.0)]


def test_batch_books_and_fallback(monkeypatch=None):
    calls = {"batch": 0, "single": 0}

    def fake_fetch(url, body=None):
        if body is not None:                      # POST /books
            calls["batch"] += 1
            return [{"asset_id": "T1", "asks": [{"price": "0.2", "size": "9"}]}]
        calls["single"] += 1                      # GET /book fallback
        return {"asks": [{"price": "0.7", "size": "4"}]}

    original = orderbook._fetch
    orderbook._fetch = fake_fetch
    try:
        books = orderbook.fetch_ask_books(["T1", "T2"])
    finally:
        orderbook._fetch = original
    assert books == {"T1": [(0.2, 9.0)], "T2": [(0.7, 4.0)]}
    assert calls == {"batch": 1, "single": 1}


def test_build_legs_needs_every_leg_priced():
    cand, _ = orderbook.select_complete_group(event([market("A"), market("B")]))
    assert orderbook.build_legs(cand, {"A-YES": [(0.3, 5)]}) == []
    legs = orderbook.build_legs(cand, {"A-YES": [(0.3, 5)], "B-YES": [(0.6, 5)]})
    assert [leg.outcome for leg in legs] == ["A", "B"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} tests passed.")
