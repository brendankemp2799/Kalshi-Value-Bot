---
experiment_id: 2026-08-09-trailing-stop-vs-stoploss-only
date: 2026-08-09
hypothesis_ref: research/hypotheses/2026-08-09-trailing-stop-premature-exit.md
status: passed
baseline: value_edge strategy, real intraday Kalshi price paths for every settled live position
dataset: "positions table, is_paper=0, status='closed', replayed against real Kalshi 1-minute candlesticks"
training_period: "2026-07-19 to 2026-08-09 (all settled live trades to date)"
validation_period: ""
out_of_sample_period: "not run — see caveats"
n_trades: 38
roi: null
pnl: null
sharpe: null
max_drawdown: null
win_rate: null
fees_included: true
slippage_assumptions: "actual bid/ask candlestick prices, taker-fee formula (0.07*price*(1-price)*contracts) applied to all simulated stop-triggered exits, matching observed real fee data"
execution_assumptions: "1-minute candlestick resolution (vs. real 30s polling) — peak-tracking uses each candle's high, trigger-check uses each candle's close; degenerate post-settlement quotes (e.g. bid crashes to 0 while ask stays pinned at 1.00) filtered out via a spread-sanity guard"
---

## Question under test

User asked: "Looking back at historical bets, how would [today's dynamic arm-move
change] have changed the results of those bets? More profitable or less in total?"
Answering this required a real backtest, not the small n=10 sample the original fix
was based on — this experiment builds and runs that backtest, then (per user
follow-up) extends it to isolate trailing-stop's contribution from stop-loss's.

## Method

Built `backtest_dynamic_arm.py` (not committed — ad hoc analysis script, logic
described here for reproducibility): for every settled live position, fetched real
Kalshi 1-minute candlesticks from ~3h before `commence_time` (or `entered_at` if
later) through settlement, then replayed each position's price path through a
from-scratch reimplementation of `evaluate_trailing_stop()`/`evaluate_stop_loss()`
under six regimes: flat 0.10 (original), flat 0.15 (this morning's fix), dynamic
(today's ramp), each with and without stop-loss active, plus a stop-loss-only
regime (trailing stop permanently disarmed) and a pure natural-settlement baseline
(no risk management at all).

**Validation of the simulator**: for the 23 positions that genuinely settled
naturally in reality (no early exit of any kind), the simulator's natural-settlement
pnl matched the real recorded pnl exactly on all 23 (0 mismatches) — confirms the
outcome-determination logic (reading the true resolved price from the tail of each
candlestick series) is correct, not just plausible-looking.

38/39 settled live positions had usable candlestick data (1 skipped, no data
available for that specific ticker/window).

## Results

**Three-way arm-threshold comparison, all applied consistently across each
position's full real price history** (not just the n=10 that historically triggered
under the old config — a broader and more honest test than the original fix's
evidence base):

| Regime | Original n=10 subset | Full n=38 |
|---|---|---|
| flat 0.10 (original) | +$0.64 | **-$1.83** |
| flat 0.15 (this morning) | +$1.93 | -$4.59 |
| dynamic (today) | +$1.88 | -$3.56 |

On the narrow n=10 sample that motivated today's fixes, dynamic performs about as
well as this morning's flat-0.15 fix, both far ahead of the original 0.10 — this
validates the original motivation. But on the fuller n=38 sample, **both of today's
fixes underperform the original flat 0.10 threshold**. Mechanism (traced through
individual positions, e.g. #239, #193, #224, #205): raising the early-game arm
threshold to avoid whipsaws (BALTEX-style) also means positions stay *unprotected*
longer during genuine early reversals, which the old 0.10 threshold would have
caught early (small loss/breakeven) but the new thresholds miss — leaving only the
unchanged, coarser stop-loss (0.20 adverse move) to catch it much later, at a
materially worse price. This failure mode couldn't appear in the original n=10
sample by construction (that sample was pre-selected from positions where the old
0.10 threshold *did* arm).

**Bigger finding (user follow-up: isolate stop-loss's contribution)**:

| Regime | Full n=38 |
|---|---|
| **Stop-loss only, no trailing stop** | **+$18.70** |
| No risk management at all (hold everything to settlement) | +$10.93 |
| flat 0.10 trailing-stop + stop-loss | -$1.83 |
| dynamic trailing-stop + stop-loss | -$3.56 |
| flat 0.15 trailing-stop + stop-loss | -$4.59 |
| flat 0.10 trailing-stop alone (no stop-loss) | -$4.14 |
| dynamic trailing-stop alone (no stop-loss) | -$7.72 |
| flat 0.15 trailing-stop alone (no stop-loss) | -$10.55 |

Stop-loss alone beat every other regime, including doing nothing at all. Removing
stop-loss from any trailing-stop regime makes it worse (confirms stop-loss itself is
net-protective, doing its job). But trailing stop, at *every* threshold tested today
and previously, was net-value-destructive when combined with stop-loss — it was
cutting real winners short (see #262: natural settlement value $1.68 vs. the $0.008
the real trailing-stop exit actually captured) by more than it was preventing losses,
across this entire sample, not just the whipsaw cases that originally motivated
today's tuning work.

## Conclusion

**Confirmed, with an important scope expansion beyond the original hypothesis.** The
original hypothesis (arm threshold too tight, causing premature exits) was correct as
far as it went, but tuning the threshold — at any value tried — couldn't fix the
larger problem: trailing stop's core mechanism (protect gains via peak-tracking and
partial lock-in) underperforms simply letting stop-loss be the only exit-risk
mechanism, over this sample. Applied fix: `ENABLE_TRAILING_STOP` set to `false` in
production `.env` (droplet), `ENABLE_STOP_LOSS` left `true`, unchanged. Deployed and
verified live 2026-08-09. `config.py`'s trailing-stop tuning constants (EARLY/LATE/
LOCK_FRACTION) left in place, documented as currently inert, in case trailing stop is
revisited and re-enabled later against a larger sample.

## Skeptic review

Not run through the formal Skeptic Agent — fast-tracked directly with the user given
this extends same-day production risk-logic changes. Known weaknesses proactively
flagged:

- **Sample size, again**: n=38, and the aggregate is dominated by a handful of
  large-swing trades (#240, #241, #242, #262 alone account for a large share of the
  natural-settlement upside). A different 38 trades could tell a different story.
- **Hindsight/look-ahead risk in the "natural settlement" and "stop-loss only"
  baselines**: these backtests know, with certainty, how every position actually
  resolved. In real time, nobody knows in advance which pullback is noise and which
  is a genuine reversal — trailing stop's entire premise is that it can tell the
  difference better than "wait and see." This backtest shows it currently can't, on
  this data, but doesn't prove it never could with better tuning or a fundamentally
  different design (e.g. volatility-scaled thresholds, longer confirmation windows).
- **Regime/period dependence**: all 38 trades are from a single 3-week window,
  July-August 2026, mostly MLB/MLS. Stop-loss's strong showing here relies on
  config.py's own earlier finding that these markets' real losses "decline gradually
  (20-185 min), not in a single tick" — a real, but market/period-specific, empirical
  regularity, not a guarantee.
- **1-minute candlestick resolution vs. real 30s polling** — the peak/close split
  (high for peak-tracking, close for trigger-check) is a reasoned approximation,
  validated against position #262's exact real recorded peak, but not proven exactly
  correct for all 38 replayed positions individually.
- Should be revisited once more settled trades accumulate under the stop-loss-only
  regime, to see whether the conclusion holds prospectively, not just in this
  retrospective replay.
