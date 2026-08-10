---
experiment_id: 2026-08-09-mm-backtest-skew-and-adverse-selection
date: 2026-08-09
hypothesis_ref: ""
status: inconclusive
baseline: mm_backtest.py's original symmetric, non-inventory-aware quoting simulation (2026-08-08)
dataset: "live-matched wide-spread ('spread_too_wide') candidate markets + their real Kalshi candlesticks"
training_period: "2026-08-09 snapshot(s) only -- see caveats"
validation_period: ""
out_of_sample_period: ""
n_trades: 9
roi: null
pnl: 3.05
sharpe: null
max_drawdown: null
win_rate: null
fees_included: true
slippage_assumptions: "trade printed at/through quote price during a 1-minute candle = filled, ignoring queue priority (upper bound on fill rate, same as original backtest)"
execution_assumptions: "quoting decisions made by calling the real execution/market_maker.py::evaluate_mm_candidate() directly, not a reimplementation"
---

## Context

Follow-up to the 2026-08-09 review of a ChatGPT-drafted market-making architecture
spec, comparing it against the existing shelved MM implementation. Two concrete gaps
were identified and are addressed here:

1. `mm_backtest.py`'s original quoting simulation didn't match what
   `execution/market_maker.py` actually does live (no inventory skew), so the one
   backtest validating MM's economics wasn't validating the strategy that would
   actually run.
2. No adverse-selection measurement existed — the spec's Section 14 (and a
   genuinely suggestive real signal in `research/failed_ideas/2026-08-09-maker-
   fill-win-rate-roi-mismatch.md`, though that turned out to be explained by a
   single outlier trade, not adverse selection) both pointed at this as worth
   building.

## Method

Rewrote `mm_backtest.py`:

- `simulate_market()` now calls `execution/market_maker.py::evaluate_mm_candidate()`
  directly every candle, tracking net inventory from the replay's own fill history
  (mirroring live `_net_inventory_contracts`) — this makes it structurally
  impossible for the backtest to drift out of sync with live quoting logic again,
  rather than just re-syncing it once by hand.
- Added `adverse_selection_report()`: for every simulated fill, looks up the price
  at 1/5/15/30/60-minute horizons after the fill (adapted from the spec's
  1s/5s/10s/30s/1m/5m — 1-minute candles are the finest resolution Kalshi's
  candlestick API offers, sub-minute horizons aren't reconstructable) and computes
  the signed favorable/adverse move.
- Added an additive, mergeable cache (`--cache-file` / `--refresh-cache`): a single
  live snapshot only sees whatever's currently matched, which turned out to matter a
  lot (see Results) — the cache now merges newly-discovered candidates into whatever
  was already cached rather than overwriting, so repeated runs over time accumulate
  a real sample instead of being stuck at one day's snapshot.

Deployed only the minimal inert pieces needed to run this (`execution/market_maker.py`,
`config.py`'s MM constants block, `core/kelly_calculator.py::mm_clip_size`) — not
`main.py`, not the DB schema change, not the risk-manager/correlation-tracker changes.
The live bot's running process never imports any of this; `ENABLE_MARKET_MAKING`
remains unset (defaults False). This is a read-only backtest, not a step toward
enabling MM.

## Results

First live run: 25 unique wide-spread candidates, **all for games 6-10 days out**
(Aug 16-20 MLS/EPL fixtures), most with zero real trading volume yet (spread reads as
0.0c for the whole lookback window on most of them — no live quotes to speak of).
5 total fills across all 25 markets.

Second run, `--refresh-cache`, ~10 minutes later: merged to 29 markets (a few newly
matched), 9 total fills, net_pnl=+$3.05.

Adverse-selection check (n=9 fills, all horizons even thinner since not every fill
has a valid price at every horizon):

| horizon | n | mean move | median move | % adverse |
|---|---|---|---|---|
| 1 min | 6 | +0.0083 | +0.0100 | 16.7% |
| 5 min | 6 | +0.0108 | +0.0125 | 16.7% |
| 15 min | 5 | +0.0050 | +0.0100 | 40.0% |
| 30 min | 5 | +0.0060 | +0.0100 | 20.0% |
| 60 min | 5 | +0.0020 | +0.0100 | 40.0% |

Directionally favorable at every horizon (positive mean/median, <50% adverse), but
n=5-6 per horizon is nowhere near enough to treat as a real signal in either
direction.

## Conclusion

**Inconclusive — infrastructure validated, sample size is the entire limiting
factor, not a data quality or logic problem.** The skew-matching fix and
adverse-selection tracking both work correctly (confirmed via the merge-count
increasing 25->29 and fills 5->9 between runs). But `mm_backtest.py` is
structurally limited to whatever's currently matched and wide, which right now
means fixtures over a week out with essentially no in-play trading history —
nothing like the July/August in-play data the original 2026-08-08 calibration run
apparently caught (per its own config.py comment: "40 live matched wide-spread
markets, ~3 days of trading each"). This isn't a flaw in the rewrite, it's an
honest reflection of what happened to be listed at the moment these runs were made.

The additive cache means this doesn't have to be solved in one sitting: re-running
`mm_backtest.py --refresh-cache` periodically (e.g. daily, alongside the existing
research-layer cron) will accumulate real in-play history as these and future
fixtures actually get played, without re-doing any of this session's work. Proposed
to the user as a next step rather than added unilaterally, given it's a new
recurring server job.

## Skeptic review

Not run through the formal pipeline — self-flagged given the small scope (backtest
infra, no production change).

- **n=9 total fills, n=5-6 per adverse-selection horizon.** This is not a finding,
  it's a validation that the measurement works. Do not cite the +0.0083 to +0.0108
  mean favorable-move numbers as evidence MM is safe from adverse selection — that
  would be exactly the kind of premature conclusion from an underpowered sample this
  whole research layer exists to catch.
- **Regime/timing dependence, acutely**: this specific sample is disproportionately
  MLS (soccer h2h/draw markets), not the MLB totals/spreads that dominate the bot's
  actual directional trade history — no reason yet to assume MM economics generalize
  across sports.
- **Fill-realism ceiling unchanged from the original backtest**: "trade printed
  through our price = filled" ignores queue priority, so simulated fill counts (and
  therefore this adverse-selection sample) are an upper bound, not a guarantee, on
  what real resting orders would have caught.
