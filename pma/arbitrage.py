"""
PMA v2 — core arbitrage math (pure, deterministic, no I/O).

This module is the heart of PMA v2. It encodes the two corrections that V1
got fundamentally wrong:

  1. ASK-SIDE ONLY.
     A lockable arbitrage exists only when the cost to *buy* one share of
     every mutually-exclusive outcome — walking the real ask book — is below
     $1.00. V1 summed the best *bids* (`sum(bid) < 1`), which is just the
     bid-ask spread and is the normal, no-arbitrage state of every market.
     You cannot lock anything in by posting bids; you get adversely selected.

  2. EQUAL SHARES, NOT EQUAL DOLLARS.
     Exactly one outcome resolves to $1 per share. To guarantee the payout you
     must hold the SAME share count N on every leg. We therefore size by shares
     and derive cost. V1 spent equal *dollars* per leg, which buys more of the
     cheap (unlikely) outcome and turns the "arb" into a directional bet.

Because the optimal trade size depends on order-book depth (buying more shares
walks you up the book and raises the average price), this module finds the
share count N that maximises guaranteed profit subject to a per-position cap.

Everything here is side-effect free so it can be unit-tested without a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# One ask level for a single outcome: (price_per_share, size_in_shares).
AskLevel = Tuple[float, float]


@dataclass
class Leg:
    """One outcome of a mutually-exclusive group, with its live ask book."""

    token_id: str
    outcome: str
    asks: List[AskLevel]  # MUST be sorted ascending by price (raw, before fees)
    fee_rate: float = 0.0  # taker fee rate for this market (0 = fee-free)
    fee_exp: float = 1.0

    def total_depth(self) -> float:
        return sum(size for _, size in self.asks)

    def fee_per_share(self, price: float) -> float:
        """Taker fee per share: rate * (p * (1 - p)) ** exponent.

        Highest at 50c, near zero at the extremes. Modelled as an extra USDC
        cost per share, which is an approximation of how it is collected.
        """
        if self.fee_rate <= 0:
            return 0.0
        return self.fee_rate * (price * (1.0 - price)) ** self.fee_exp

    def cost_asks(self) -> List[AskLevel]:
        """Ask levels with the taker fee folded into each price."""
        return [(p + self.fee_per_share(p), size) for p, size in self.asks]


@dataclass
class Opportunity:
    event: str
    legs: List[Leg]
    shares: float        # N — equal shares to buy on every leg
    total_cost: float    # $ to acquire N shares of every leg (VWAP-walked)
    payout: float        # guaranteed payout = N * $1
    net_profit: float    # payout - total_cost
    margin_pct: float    # net_profit / total_cost * 100
    per_leg_plan: List[dict] = field(default_factory=list)
    side: str = "YES"            # "YES": buy every YES. "NO": buy every NO.
    payout_per_set: float = 1.0  # $1 for a YES set, n-1 for a NO set
    total_fees: float = 0.0      # taker fees included in total_cost
    # Set by the scanner after evaluation (not part of the pricing math):
    days_to_resolution: Optional[float] = None
    annualised_pct: Optional[float] = None
    has_catch_all: bool = False

    @property
    def is_profitable(self) -> bool:
        return self.net_profit > 0

    def beats_hurdle(self, hurdle_apr_pct: float) -> bool:
        """True if the annualised return clears the hurdle. Unknown dates fail."""
        if hurdle_apr_pct <= 0:
            return True
        return self.annualised_pct is not None and self.annualised_pct >= hurdle_apr_pct


def annualised_pct(margin_pct: float, days: Optional[float]) -> Optional[float]:
    """Simple (non-compounded) annualised return for capital locked `days`.

    A 2% gap locked for six months is about 4% a year, which can be worse
    than holding cash. Returns None when the resolution date is unknown.
    """
    if days is None or days <= 0:
        return None
    return margin_pct * 365.0 / days


def effective_cost(asks: List[AskLevel], shares: float) -> Tuple[float, float]:
    """Cost to buy `shares` shares by walking the ask book from the top.

    Returns (cost, filled). `filled` is < `shares` when the book is too thin,
    which is exactly the depth check V1 never performed.
    """
    remaining = shares
    cost = 0.0
    for price, size in asks:
        if remaining <= 0:
            break
        take = min(remaining, size)
        cost += take * price
        remaining -= take
    filled = shares - max(0.0, remaining)
    return cost, filled


def _price_at_depth(asks: List[AskLevel], depth: float) -> Optional[float]:
    """Marginal ask price for the share sitting at cumulative position `depth`.

    Returns None if `depth` is at or beyond the book's total depth.
    """
    cum = 0.0
    for price, size in asks:
        if depth < cum + size:
            return price
        cum += size
    return None


def evaluate_group(
    event: str,
    legs: List[Leg],
    *,
    per_position_max: float,
    min_net_profit: float = 0.0,
    payout_per_set: float = 1.0,
    side: str = "YES",
) -> Optional[Opportunity]:
    """Find the profit-maximising equal-share arbitrage for one group.

    Returns an Opportunity if a strictly profitable, fully fillable trade
    exists within the per-position dollar cap, else None.

    The marginal cost of the Nth share-set (one share of every leg) is
    non-decreasing in N, so guaranteed profit(N) = N - cost(N) is concave.
    We therefore walk the order book in segments of constant marginal cost,
    buying sets while the marginal set-cost is below $1 and the position cap
    allows, stopping the instant either condition fails.
    """
    if len(legs) < 2:
        return None

    # Price everything after taker fees. Raw books are kept for the fill plan.
    cost_books = [leg.cost_asks() for leg in legs]

    # Equal shares means we can fill at most as many sets as the THINNEST leg.
    max_fillable = min(leg.total_depth() for leg in legs)
    if max_fillable <= 0:
        return None

    # Breakpoints = every leg's cumulative-depth boundary. Between consecutive
    # breakpoints, every leg sits at a single price level, so marginal set-cost
    # is constant.
    breakpoints = {max_fillable}
    for leg in legs:
        cum = 0.0
        for _, size in leg.asks:
            cum += size
            if cum < max_fillable:
                breakpoints.add(round(cum, 9))
    segments = sorted(b for b in breakpoints if 0 < b <= max_fillable)

    shares = 0.0
    total_cost = 0.0
    prev = 0.0
    for bp in segments:
        mid = (prev + bp) / 2.0
        marginal = 0.0
        ok = True
        for book in cost_books:
            p = _price_at_depth(book, mid)
            if p is None:
                ok = False
                break
            marginal += p
        if not ok:
            break

        # Once the marginal set-cost reaches the payout there is no profit to add.
        if marginal >= payout_per_set:
            break

        width = bp - prev
        segment_cost = width * marginal

        if total_cost + segment_cost <= per_position_max:
            shares = bp
            total_cost += segment_cost
        else:
            # Position cap binds inside this segment — take the affordable slice.
            affordable = (per_position_max - total_cost) / marginal
            shares = prev + affordable
            total_cost = per_position_max
            break

        prev = bp

    if shares <= 0:
        return None

    payout = shares * payout_per_set
    net_profit = payout - total_cost
    if net_profit <= min_net_profit:
        return None

    per_leg_plan = []
    total_fees = 0.0
    for leg, book in zip(legs, cost_books):
        cost, filled = effective_cost(leg.asks, shares)
        with_fees, _ = effective_cost(book, shares)
        fee = with_fees - cost
        total_fees += fee
        per_leg_plan.append(
            {
                "token_id": leg.token_id,
                "outcome": leg.outcome,
                "shares": round(filled, 4),
                "cost": round(cost, 4),
                "fee": round(fee, 4),
                "avg_price": round(cost / filled, 4) if filled else None,
            }
        )

    return Opportunity(
        event=event,
        legs=legs,
        shares=round(shares, 4),
        total_cost=round(total_cost, 4),
        payout=round(payout, 4),
        net_profit=round(net_profit, 4),
        margin_pct=round((net_profit / total_cost) * 100, 4) if total_cost else 0.0,
        per_leg_plan=per_leg_plan,
        side=side,
        payout_per_set=payout_per_set,
        total_fees=round(total_fees, 4),
    )


def top_of_book(legs: List[Leg], payout_per_set: float = 1.0) -> Optional[dict]:
    """How far one share-set is from parity at the best asks, after fees.

    edge_pct is negative when there is no gap (the usual case). Logged every
    scan so you can see how close each group gets, not just when it crosses.
    """
    if len(legs) < 2 or any(not leg.asks for leg in legs):
        return None
    raw = sum(leg.asks[0][0] for leg in legs)
    fees = sum(leg.fee_per_share(leg.asks[0][0]) for leg in legs)
    cost = raw + fees
    return {
        "ask_sum": round(raw, 6),
        "fees": round(fees, 6),
        "edge_pct": round((payout_per_set - cost) / cost * 100, 4),
        "min_size": min(leg.asks[0][1] for leg in legs),
    }
