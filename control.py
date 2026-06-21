"""
The control surface — Track C, the human-in-the-loop layer.

This is the "buttons" from our design discussion, in terminal form. The bot is
the analyst: it finds and ranks an opportunity and lays out exactly what it
proposes to do. You are the decision-maker: nothing touches real money until
you approve it here.

For a first correct version a clean terminal gate is the right call (a web
dashboard with literal buttons is a later, optional skin over this same gate).
"""

from __future__ import annotations

from .arbitrage import Opportunity


def render(opp: Opportunity, mode: str) -> str:
    lines = []
    lines.append("  " + "=" * 60)
    lines.append(f"  OPPORTUNITY  [{mode.upper()}]  {opp.event}")
    lines.append("  " + "-" * 60)
    lines.append(f"  Legs:            {len(opp.legs)}")
    lines.append(f"  Shares per leg:  {opp.shares:g}  (equal across all legs)")
    lines.append(f"  Total cost:      ${opp.total_cost:,.4f}")
    lines.append(f"  Guaranteed payout: ${opp.payout:,.4f}  (one leg resolves to $1)")
    lines.append(f"  Net profit:      ${opp.net_profit:,.4f}  ({opp.margin_pct:.3f}%)")
    lines.append("  Fill plan (ask-side, marketable):")
    for p in opp.per_leg_plan:
        ap = f"@{p['avg_price']:.4f}" if p["avg_price"] is not None else "@ n/a"
        lines.append(
            f"    - {p['outcome'][:42]:<42} {p['shares']:g} sh {ap}  ${p['cost']:.4f}"
        )
    lines.append("  " + "=" * 60)
    return "\n".join(lines)


def request_approval(opp: Opportunity, mode: str, require_approval: bool) -> bool:
    """Return True if the trade is cleared to execute.

    In scan mode we never execute. In paper/live mode, when approval is
    required, we ask for an explicit typed 'yes'. No default-yes, no timeouts —
    silence is a no.
    """
    if mode == "scan":
        return False
    if not require_approval:
        return True
    try:
        answer = input("  Execute this trade? type 'yes' to confirm: ").strip().lower()
    except EOFError:
        # Non-interactive session (e.g. piped/cron) — refuse rather than guess.
        return False
    return answer == "yes"
