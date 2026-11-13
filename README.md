# PMA v2.3 — Polymarket negRisk scanner

A research tool that scans [Polymarket](https://polymarket.com) for events
where buying a full set of outcomes costs less than the set pays out, and
reports whether that gap survives taker fees and the capital lock-up. It
logs every priced group so the behaviour of these gaps can be measured.

It is read-only by default. It is not investment advice and it is not a way
to make reliable money: see "What to expect" below.

---

## What to expect

On liquid markets a real gap of this kind is rare and closes quickly. Most
scans report nothing, and that is the correct result. The gaps that do
persist usually persist for a reason:

- **Fees.** Most categories charge a taker fee of
  `shares x rate x p x (1 - p)`. On legs priced near 50c that is about 1% of
  the share price, enough to erase a typical gap. All figures are after fees.
- **Time value.** Capital is locked until the market resolves. A 2% gap that
  resolves in five months is about 5% a year, which can be worse than holding
  cash. The scanner annualises every gap and compares it with a hurdle rate.
- **Unlisted outcomes.** If a market lists "Democrat" and "Republican" and
  someone else wins, every leg pays $0. The scanner warns when it cannot find
  a catch-all outcome. The warning is a heuristic; read the market rules.
- **Resolution risk.** Markets can resolve late, be disputed, or be voided.
  None of that is modelled.

Treat the output as a list of things to investigate, not a list of trades.

---

## How it works

```
discover events with all their markets (Gamma API)
  -> keep negRisk groups that are provably complete
    -> skip groups whose quoted asks are clearly above $1
      -> fetch real ask books in batches (CLOB API)
        -> price both sides after taker fees, walking book depth:
             buy every YES (set pays $1) or buy every NO (set pays n-1)
          -> log every priced group to logs/scans.csv
            -> annualise against days to resolution, compare with hurdle
              -> human approval gate (paper / live, YES side only)
```

Design rules, and the mistake each one prevents:

| Rule | Why |
|---|---|
| Sum **asks**, never bids | Bids summing below $1 is just the spread; it cannot be bought |
| Equal **shares** per leg | Equal dollars per leg is a directional bet, not a hedge |
| Walk real book **depth** | A top-of-book quote says nothing about fillable size |
| Group must be **complete** | A partial set below $1 looks like a gap and guarantees nothing |
| Price **after fees** | A gap smaller than the taker fee is not a gap |
| **Annualise** the gap | Raw percentage ignores how long the capital is locked |
| Human approves every trade | The scanner proposes; a person decides |

### The two sides

In a negRisk event exactly one outcome wins. Two full sets can be bought:

- **Every YES** pays $1 at resolution. Capital is locked until then.
- **Every NO** pays n-1 dollars at resolution, because all legs but one win.
  Polymarket's negRisk conversion can in principle turn a full NO set back
  into cash before resolution, which is why this side is the one professional
  traders compete for; published on-chain analysis puts the typical life of
  such a gap at seconds. This tool reports NO-side gaps but does not trade or
  convert them.

### Changes in v2.3

- The event list is downloaded every 30 minutes (`PMA_DISCOVERY_MINS`) and
  cached as a watchlist; scans in between only re-price order books. Scans
  are several times faster and put far less load on the API.
- Event pages are requested 50 at a time, dropping to 20 if refused; the
  API began rejecting pages of 100 in November 2026.
- When the API refuses requests the scanner keeps its last watchlist and
  waits, doubling the delay each time, instead of retrying every minute.

### Changes in v2.2

- Taker fees modelled per market from its fee schedule, with a conservative
  default (`PMA_DEFAULT_FEE_RATE`) when a market reports none.
- NO-side sets priced alongside YES-side sets (scan only).
- Every priced group is appended to `logs/scans.csv`; `analyze.py` summarises
  how close each group got to parity, how often it crossed and for how long.

v2.1 added per-event completeness checks, the annualised hurdle, the
catch-all warning and batched order books.

---

## Layout

```
run.py                 main loop / entrypoint
analyze.py             summary of the scan log
pma/arbitrage.py       pure pricing math: depth walk, equal-share sizing
pma/orderbook.py       discovery, completeness checks, order books
pma/control.py         approval prompt and opportunity display
pma/execution.py       FOK orders, unwind on partial fill, staleness check
pma/scanlog.py         CSV log of every priced group
pma/state.py           session accounting and persistence
pma/config.py          settings, read from environment variables
tests/                 offline tests for the pricing and scanner logic
```

## Running

Scan mode needs only the Python standard library (3.10+).

```bash
python run.py --once               # one read-only scan
PMA_MODE=scan python run.py        # read-only loop
PMA_MODE=paper python run.py       # simulated fills against the live book
python analyze.py                  # summarise logs/scans.csv
python -m pytest -q                # tests (pip install pytest)
```

Live mode (`PMA_MODE=live`) places real orders, needs `py-clob-client` and a
`POLYMARKET_PRIVATE_KEY` environment variable, and asks for a typed `yes`
before every trade. The key is read at runtime and never written to disk.
The live order path has not been exercised against the real exchange; do not
rely on it without testing with trivial size first.

Settings: `PMA_PORTFOLIO`, `PMA_MIN_PROFIT`, `PMA_HURDLE_APR`, `PMA_MAX_LOSS`,
`PMA_DEFAULT_FEE_RATE`, `PMA_MAX_EVENTS`, `PMA_DISCOVERY_MINS`, `PMA_REQUIRE_APPROVAL`.

---

## Disclaimer

For education and research. Prediction-market trading carries real risk of
loss, including total loss of a position. Whether you may use Polymarket
depends on where you live; check before trading. Use at your own risk.

## Roadmap

- [ ] Cross-market consistency checks (linked markets priced inconsistently)
- [ ] Wire `state.reconcile()` to live balances and positions
- [ ] Record book snapshots for offline backtesting
