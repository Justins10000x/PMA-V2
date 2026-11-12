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


def render(opp: Opportunity, mode: str, hurdle_apr: float = 0.0) -> str:
    lines = []
    lines.append("  " + "=" * 60)
    lines.append(f"  GAP  [{mode.upper()}]  buy every {opp.side}  |  {opp.event}")
    lines.append("  " + "-" * 60)
    lines.append(f"  Legs:            {len(opp.legs)}")
    lines.append(f"  Shares per leg:  {opp.shares:g}  (equal across all legs)")
    lines.append(f"  Total cost:      ${opp.total_cost:,.4f}  (includes ${opp.total_fees:,.4f} taker fees)")
    if opp.side == "YES":
        lines.append(f"  Payout at resolution: ${opp.payout:,.4f}  (if a listed outcome wins)")
    else:
        lines.append(f"  Payout at resolution: ${opp.payout:,.4f}  (all legs but one pay $1)")
    lines.append(f"  Gap after fees:  ${opp.net_profit:,.4f}  ({opp.margin_pct:.3f}%)")
    if opp.annualised_pct is None:
        lines.append("  Annualised:      unknown (no future end date listed)")
    else:
        lines.append(
            f"  Annualised:      {opp.annualised_pct:.1f}% over "
            f"{opp.days_to_resolution:.0f} day(s) locked  (hurdle {hurdle_apr:g}%)"
        )
    verdict = "CLEARS HURDLE" if opp.beats_hurdle(hurdle_apr) else "BELOW HURDLE - not worth the lock-up"
    lines.append(f"  Verdict:         {verdict}")
    if opp.side == "NO":
        lines.append("  NOTE: scan only. A full NO set can in principle be converted to")
        lines.append("        cash before resolution (negRisk conversion), which this tool")
        lines.append("        does not do. These gaps usually close within seconds.")
    elif not opp.has_catch_all:
        lines.append("  WARNING: no catch-all outcome detected. If an unlisted outcome")
        lines.append("           wins, every leg pays $0. Read the market rules first.")
    lines.append("  Fill plan (ask-side, marketable):")
    for p in opp.per_leg_plan:
        ap = f"@{p['avg_price']:.4f}" if p["avg_price"] is not None else "@ n/a"
        lines.append(
            f"    - {p['outcome'][:42]:<42} {p['shares']:g} sh {ap}  ${p['cost']:.4f}"
            f"  +fee ${p.get('fee', 0):.4f}"
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
