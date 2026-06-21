"""
PMA v2 configuration.

Secrets are read from the environment, never written to disk. This fixes the
V1 issue where the wallet private key was saved into credentials.json in
plaintext (and where the README falsely claimed it was not stored).

Set before running live:
    export POLYMARKET_PRIVATE_KEY=0x....
    export PMA_MODE=scan        # scan | paper | live
"""

import os

# ----------------------------------------------------------------------------
# RUN MODE  — the "autonomy ladder"
#   scan : read-only. Find & rank opportunities, never trade. (safest)
#   paper: simulate execution against the REAL book snapshot (honest fills).
#   live : place real orders. Always gated by human approval (see control.py).
# ----------------------------------------------------------------------------
MODE = os.environ.get("PMA_MODE", "scan").lower()

# Human-in-the-loop. When True, every trade (paper or live) must be approved
# at the terminal before it executes. Leave True — this is Track C, the
# control surface. Set False only if you truly want full autonomy.
REQUIRE_APPROVAL = os.environ.get("PMA_REQUIRE_APPROVAL", "true").lower() == "true"

# ----------------------------------------------------------------------------
# PORTFOLIO & RISK
# ----------------------------------------------------------------------------
PORTFOLIO_SIZE = float(os.environ.get("PMA_PORTFOLIO", "1000"))

MAX_EXPOSURE_PCT = 0.20          # hard cap on total committed capital
PER_POSITION_PCT = 0.05          # hard cap on any single arbitrage position

MAX_EXPOSURE = round(PORTFOLIO_SIZE * MAX_EXPOSURE_PCT, 2)
PER_POSITION_MAX = round(PORTFOLIO_SIZE * PER_POSITION_PCT, 2)

# Minimum guaranteed profit (in $) for a position to be worth doing. Kept in
# dollars, not %, because tiny-margin fills lose to gas/slippage in practice.
MIN_NET_PROFIT = float(os.environ.get("PMA_MIN_PROFIT", "0.50"))

MAX_LEGS = 12                    # skip very wide groups (fill/latency risk)

# Circuit breaker: halt the whole bot if realised loss in a session exceeds
# this. A correctly-built arb bot should rarely lose, so any breach means
# something is wrong (stale books, bad unwinds) — stop and look.
MAX_SESSION_LOSS = float(os.environ.get("PMA_MAX_LOSS", "25"))

# Staleness guard: re-fetch each leg's book immediately before sending and
# abort if the edge has decayed below this fraction of what we detected.
STALENESS_REFRESH = True
MIN_EDGE_RETENTION = 0.5         # require >=50% of detected profit to remain

# ----------------------------------------------------------------------------
# TIMING
# ----------------------------------------------------------------------------
SCAN_INTERVAL = 60               # seconds between scans
MAX_MARKETS = 600                # discovery breadth per scan

# ----------------------------------------------------------------------------
# ENDPOINTS & PATHS
# ----------------------------------------------------------------------------
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
STATE_FILE = "state.json"
LOG_FILE = "logs/pma.log"

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY")  # live mode only


def validate():
    """Fail fast on an incoherent configuration."""
    if MODE not in ("scan", "paper", "live"):
        raise SystemExit(f"PMA_MODE must be scan|paper|live, got '{MODE}'")
    if MODE == "live" and not PRIVATE_KEY:
        raise SystemExit(
            "Live mode needs POLYMARKET_PRIVATE_KEY in the environment. "
            "It is read at runtime and never written to disk."
        )
    if PER_POSITION_MAX > MAX_EXPOSURE:
        raise SystemExit("PER_POSITION_MAX cannot exceed MAX_EXPOSURE")
