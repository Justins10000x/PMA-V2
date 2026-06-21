# PMA v2 — Polymarket Mutually-exclusive Arbitrage

A **correct reference implementation** of the "buy every outcome for less than
$1" arbitrage on [Polymarket](https://polymarket.com) negRisk markets.

This is v2 of PMA. v1 chased an opportunity that wasn't real; v2 is the same
idea done correctly, with a human in the loop by default.

---

## Read this first (honest expectations)

This bot is built to be **mechanically correct**, not to be a money printer.

A genuine lockable arbitrage on Polymarket exists only when the cost to *buy*
one share of every outcome — taken from the live ask book — is below $1.00.
On liquid markets those windows are **rare and short-lived**, and the fast ones
are won by latency-optimised bots, not a 60-second Python poll. Treat v2 as:

- a correct, transparent scanner for ask-side dislocations, and
- a safe, human-gated executor for the occasions a real one appears.

If you want a *durable* edge, that's a different strategy (cross-market logical
consistency) and a future project — not this one.

---

## What changed from v1

v1 had two bugs that, together, meant it could not produce arbitrage:

| | v1 (broken) | v2 (correct) |
|---|---|---|
| Which side | summed **bids** (`sum(bid) < 1`) — that's just the spread, never lockable, and posting bids gets adversely selected | summed **asks** with depth; acts only when buying everything **now** costs < $1 |
| Sizing | equal **dollars** per leg → more shares of the unlikely outcome → a directional bet | equal **shares** per leg → payout is identical whoever wins |
| Liquidity | filtered on lifetime `volume`; no depth check | reads the **live CLOB book** and verifies depth before committing |
| Fills | posted resting bids, waited 10 min, legs filled one-sided | **FOK** marketable orders; **auto-unwind** any stranded leg |
| Paper mode | random per-leg coin flip (flattering, fake) | simulates against the **real book snapshot** |
| Secrets | wrote the **private key to disk in plaintext** (README even denied it) | private key read from **env var**, never written |
| Accounting | added idealised expected profit, ignored partials | records **realised** outcomes only; circuit breaker on loss |

The core math lives in `pma/arbitrage.py` and is covered by `tests/`.

---

## How it works

```
discover negRisk groups (Gamma API)
  -> fetch real ask books with depth (CLOB API)
    -> evaluate_group: ask-side, equal-share, depth-aware   [pure + tested]
      -> rank by guaranteed profit
        -> human approval gate            [Track C control surface]
          -> FOK execution + auto-unwind on partials
            -> honest accounting + circuit breaker
```

The trade size N is chosen to maximise guaranteed profit: it buys share-sets
while the marginal cost of the next set (walking up every book together) stays
below $1 and the per-position cap allows.

---

## Project layout

```
PMA-v2/
├── run.py                 # main loop / entrypoint
├── pma/
│   ├── arbitrage.py       # PURE core: depth-walk, equal-share sizing, detection
│   ├── orderbook.py       # discovery (Gamma) + live ask books with depth (CLOB)
│   ├── control.py         # human-in-the-loop approval surface (Track C)
│   ├── execution.py       # FOK orders + auto-unwind + staleness guard
│   ├── state.py           # honest P&L, persistence, reconcile hook
│   └── config.py          # config + env-based secrets + run mode
└── tests/
    └── test_arbitrage.py  # locks in the corrected math
```

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # only needed for live + tests
python -m pytest -q                    # verify the core math
```

---

## Running

The `PMA_MODE` env var sets the rung on the autonomy ladder.

```bash
# Read-only. Finds and prints opportunities, never trades. Start here.
PMA_MODE=scan python run.py

# Simulated execution against the real book snapshot. Still asks you to approve.
PMA_MODE=paper python run.py

# Live. Every trade is gated by a typed 'yes' at the terminal.
PMA_MODE=live POLYMARKET_PRIVATE_KEY=0x... python run.py
```

Other env vars: `PMA_PORTFOLIO`, `PMA_MIN_PROFIT`, `PMA_MAX_LOSS`,
`PMA_REQUIRE_APPROVAL` (set `false` only if you truly want full autonomy).

---

## Safety

- **Human-in-the-loop by default** — nothing trades without your explicit `yes`.
- **Circuit breaker** — halts if session losses exceed `PMA_MAX_LOSS`. For a
  correct arb bot, a loss usually means something is wrong (stale book, failed
  unwind); stopping is the right reflex.
- **Staleness guard** — re-checks the book right before sending and aborts if
  the edge has decayed.
- **Secrets** — the private key is read from the environment at runtime only.

---

## Disclaimer

Educational. Prediction-market trading carries real risk of loss. Arbitrage
opportunities can vanish mid-execution, leaving partial positions and losses.
Paper results do not guarantee live results. Use at your own risk.

## Roadmap

- [ ] v3 / Track B: cross-market logical-consistency arbitrage (the real edge)
- [ ] wire `state.reconcile()` to live balance/position queries
- [ ] optional web dashboard over the existing approval gate
- [ ] record book snapshots for offline backtesting
```
