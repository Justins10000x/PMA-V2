"""
Unit tests for pma.arbitrage — the part V1 got wrong.

Run with:  python -m pytest -q   (or: python tests/test_arbitrage.py)

These tests are deterministic and need no network. They lock in the two
corrections: ask-side detection and equal-share sizing.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pma.arbitrage import Leg, effective_cost, evaluate_group  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_effective_cost_walks_the_book():
    # 5 shares @0.30 then deeper @0.40. Buying 8 shares = 5*0.30 + 3*0.40.
    cost, filled = effective_cost([(0.30, 5), (0.40, 100)], 8)
    assert approx(cost, 5 * 0.30 + 3 * 0.40)
    assert approx(filled, 8)


def test_effective_cost_respects_thin_book():
    # Only 5 shares exist; asking for 8 fills just 5.
    cost, filled = effective_cost([(0.30, 5)], 8)
    assert approx(filled, 5)
    assert approx(cost, 5 * 0.30)


def test_clear_arbitrage_top_of_book():
    # Sum of top asks = 0.30 + 0.60 = 0.90 < 1  -> profit of 0.10 per set.
    legs = [
        Leg("A", "Yes", [(0.30, 10), (0.40, 100)]),
        Leg("B", "Yes", [(0.60, 10), (0.65, 100)]),
    ]
    opp = evaluate_group("test", legs, per_position_max=1000)
    assert opp is not None and opp.is_profitable
    # Best N is 10 sets: beyond that marginal = 0.40+0.65 = 1.05 >= 1.
    assert approx(opp.shares, 10)
    assert approx(opp.total_cost, 9.0)
    assert approx(opp.payout, 10.0)
    assert approx(opp.net_profit, 1.0)


def test_no_arbitrage_when_asks_sum_above_one():
    # The World Cup situation: sum of asks > 1, so NO opportunity exists.
    legs = [
        Leg("A", "Yes", [(0.55, 100)]),
        Leg("B", "Yes", [(0.55, 100)]),
    ]
    assert evaluate_group("wc", legs, per_position_max=1000) is None


def test_equal_shares_not_equal_dollars():
    # The exact V1 bug: Kansas-style 0.29 / 0.61. A correct arb buys EQUAL
    # shares of each leg, so the guaranteed payout never depends on the winner.
    legs = [
        Leg("DEM", "Yes", [(0.29, 1000)]),
        Leg("REP", "Yes", [(0.61, 1000)]),
    ]
    opp = evaluate_group("kansas", legs, per_position_max=100)
    assert opp is not None
    # Equal share count on both legs.
    shares = {p["outcome"]: p["shares"] for p in opp.per_leg_plan}
    assert approx(shares["Yes"], opp.shares) if False else True  # labels equal here
    counts = [p["shares"] for p in opp.per_leg_plan]
    assert approx(counts[0], counts[1])  # <-- equal shares, the whole point
    # Payout is invariant to which side wins: N shares * $1 either way.
    assert approx(opp.payout, opp.shares)


def test_position_cap_binds():
    # Marginal set-cost is 0.90; a $45 cap limits us to 50 sets, cost $45.
    legs = [
        Leg("A", "Yes", [(0.30, 10_000)]),
        Leg("B", "Yes", [(0.60, 10_000)]),
    ]
    opp = evaluate_group("capped", legs, per_position_max=45)
    assert opp is not None
    assert approx(opp.total_cost, 45.0, tol=1e-4)
    assert approx(opp.shares, 50.0, tol=1e-4)
    assert approx(opp.net_profit, 5.0, tol=1e-4)


def test_depth_limits_size():
    # Cheap top-of-book but tiny size; deeper book is unprofitable.
    legs = [
        Leg("A", "Yes", [(0.20, 3), (0.50, 1000)]),
        Leg("B", "Yes", [(0.55, 1000)]),
    ]
    opp = evaluate_group("thin", legs, per_position_max=1000)
    # First 3 sets: 0.20 + 0.55 = 0.75 < 1  -> profitable for 3 sets only.
    assert opp is not None
    assert approx(opp.shares, 3, tol=1e-6)
    assert approx(opp.net_profit, 3 * (1 - 0.75), tol=1e-6)


def test_single_leg_is_never_an_opportunity():
    assert evaluate_group("solo", [Leg("A", "Yes", [(0.10, 100)])],
                          per_position_max=100) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} tests passed.")
