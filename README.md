# PMA v2.1 — Polymarket negRisk scanner

A research tool that scans [Polymarket](https://polymarket.com) for events
where buying one share of every outcome costs less than the $1 the set pays
out, and reports whether that gap is worth anything once the capital lock-up
is priced in.

It is read-only by default. It is not investment advice and it is not a way
to make reliable money: see "What to expect" below.

---

## What to expect

On liquid markets a real gap of this kind is rare and closes quickly. Most
scans report nothing, and that is the correct result. The gaps that do
persist usually persist for a reason:

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
        -> price the set: ask-side, equal shares, walking book depth
          -> annualise against days to resolution, compare with hurdle
            -> human approval gate (paper / live only)
```

Design rules, and the mistake each one prevents:

| Rule | Why |
|---|---|
| Sum **asks**, never bids | Bids summing below $1 is just the spread; it cannot be bought |
| Equal **shares** per leg | Equal dollars per leg is a directional bet, not a hedge |
| Walk real book **depth** | A top-of-book quote says nothing about fillable size |
| Group must be **complete** | A partial set below $1 looks like a gap and guarantees nothing |
| **Annualise** the gap | Raw percentage ignores how long the capital is locked |
| Human approves every trade | The scanner proposes; a person decides |

### Changes in v2.1

- Discovery works per event, with every market of the event in hand. Groups
  with an untradable leg, and augmented events with placeholder outcomes, are
  rejected. v2.0 could silently evaluate partial sets.
- Annualised return and hurdle rate (`PMA_HURDLE_APR`, default 8).
- Warning when no catch-all outcome is detected.
- Order books fetched in batches; clearly overpriced groups are skipped first.
- Exposure limit now tracks committed capital.
- `--once` flag for a single scan.

---

## Layout

```
run.py                 main loop / entrypoint
pma/arbitrage.py       pure pricing math: depth walk, equal-share sizing
pma/orderbook.py       discovery, completeness checks, order books
pma/control.py         approval prompt and opportunity display
pma/execution.py       FOK orders, unwind on partial fill, staleness check
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
python -m pytest -q                # tests (pip install pytest)
```

Live mode (`PMA_MODE=live`) places real orders, needs `py-clob-client` and a
`POLYMARKET_PRIVATE_KEY` environment variable, and asks for a typed `yes`
before every trade. The key is read at runtime and never written to disk.
The live order path has not been exercised against the real exchange; do not
rely on it without testing with trivial size first.

Settings: `PMA_PORTFOLIO`, `PMA_MIN_PROFIT`, `PMA_HURDLE_APR`, `PMA_MAX_LOSS`,
`PMA_MAX_EVENTS`, `PMA_REQUIRE_APPROVAL`.

---

## Disclaimer

For education and research. Prediction-market trading carries real risk of
loss, including total loss of a position. Whether you may use Polymarket
depends on where you live; check before trading. Use at your own risk.

## Roadmap

- [ ] Cross-market consistency checks (linked markets priced inconsistently)
- [ ] Wire `state.reconcile()` to live balances and positions
- [ ] Record book snapshots for offline backtesting
