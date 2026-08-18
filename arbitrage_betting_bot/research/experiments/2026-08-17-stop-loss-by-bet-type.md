---
experiment_id: 2026-08-17-stop-loss-by-bet-type
date: 2026-08-17
hypothesis_ref: ""
status: acted-on-provisional
baseline: "the deployed stop-loss: flat 0.20, totals ramped 0.35 -> 0.20 across the game"
dataset: "every settled live position (is_paper=0, pnl NOT NULL), replayed against its real 1-minute Kalshi candlestick path from entry to game end"
training_period: "2026-07-19 .. 2026-08-16"
validation_period: ""
out_of_sample_period: ""
n_trades: 92
roi: 0.193
pnl: 19.39
sharpe: null
max_drawdown: null
win_rate: null
fees_included: false
slippage_assumptions: "3c and 6c tested explicitly; 3c is the MEASURED median (h2h hard-stop exits, where the threshold is flat so slippage is directly |move| - 0.20). Exit fees are folded into the slippage figure because it was calibrated against realised P&L."
execution_assumptions: "exit at the quoted bid on the first candle at/below the stop level; no partial fills; entry fees NOT charged (~$2.31 across all 92)"
---

## Hypothesis under test

Is the stop-loss earning its keep, and is 0.20 the right threshold?

Prompted by a simple observation: realised P&L was negative while the strategy's
bets looked fine. The question turned out to have a different answer than either
"yes" or "no".

## Method

`stopbt.py` (scratchpad) replays each settled position's real 1-minute candlestick
path through the **actual** `execution/risk_manager.evaluate_stop_loss()` and
`evaluate_trailing_stop()`, not a reimplementation, so the backtest cannot drift
from live behaviour. `datetime` is monkeypatched to each candle's timestamp so
time-ramped policies replay honestly.

Three independent statistics, deliberately sensitive to different things:

1. **P&L under each policy**, dollar-weighted. Sensitive to stake concentration.
2. **Equal-weighted ROI**. Removes stake concentration entirely.
3. **Recovery rate** — of positions that fell X below entry, how many still won.
   Sensitive to price dynamics rather than settlement luck.

Plus a paired bootstrap (20k resamples) and a full jackknife on (1) and (2).

## THE HEADLINE: one threshold cannot serve both books

Of positions that fell 20c below entry, how many still WON:

| bet_type | base win rate | after -10c | after -20c | after -30c |
|---|---|---|---|---|
| h2h (n=38) | 47.4% | 36.7% | **31.8%** | 14.3% |
| totals (n=49) | 44.9% | 28.6% | **10.7%** | 4.0% |

Same starting point, opposite behaviour after an adverse move.

Stopping realises `s` per contract with certainty; holding is worth `p`. So
**stopping is correct iff `s > p`**:

| bet_type | drop | n | s (exit px) | p (recovery) | s - p | verdict |
|---|---|---|---|---|---|---|
| h2h | 0.10 | 30 | 0.284 | 0.367 | -0.082 | HOLD |
| h2h | 0.20 | 22 | 0.220 | 0.318 | **-0.099** | HOLD |
| h2h | 0.25 | 18 | 0.181 | 0.222 | -0.041 | HOLD |
| h2h | 0.30 | 14 | 0.154 | 0.143 | +0.011 | STOP (knife-edge) |
| h2h | 0.35 | 12 | 0.113 | 0.167 | -0.053 | HOLD |
| totals | 0.10 | 35 | 0.335 | 0.286 | +0.050 | STOP |
| totals | 0.20 | 28 | 0.230 | 0.107 | **+0.123** | STOP |
| totals | 0.30 | 25 | 0.134 | 0.040 | +0.094 | STOP |
| totals | 0.35 | 22 | 0.095 | 0.000 | +0.095 | STOP |

**Mechanism.** A totals market resolves by accumulation — runs and goals only ever
get added, the clock only runs one way. Once it is 20c underwater, the innings
needed to rescue it have physically been spent; the move is arithmetic, not
sentiment. An h2h market has no such ratchet: a 20c move means a lead changed
hands, and leads change hands again.

## P&L (n=92, $100.27 staked, 3c slippage, position 246 excluded)

| policy | P&L | ROI | equal-wt ROI | exits |
|---|---|---|---|---|
| no stop | +$19.90 | 19.8% | +6.8% | 0/92 |
| **stop-loss as deployed** | **+$19.39** | **19.3%** | **+5.3%** | 52/92 |
| trailing stop | +$4.62 | 4.6% | **-11.7%** | 43/92 |
| both (the 7-day bug window) | +$2.27 | 2.3% | **-16.0%** | 73/92 |

**The deployed system is a wash** — 19.3% vs 19.8% for doing nothing. Not because
stops don't work, but because one threshold applied to two books that want opposite
treatment cancels out.

Split by bet type (equal-weighted ROI, 3c slippage):

| threshold | h2h | totals |
|---|---|---|
| 0.20 | +0.2% | **+11.5%** |
| 0.25 | +10.8% | +4.7% |
| 0.30 | **+16.9%** | +6.4% |
| 0.35 | +13.1% | +4.7% |
| none | +18.6% | -2.7% |

Paired bootstrap, 20k resamples:

| group | comparison | $ gap | 90% CI | P(A>B) | eq-wt | P |
|---|---|---|---|---|---|---|
| ALL | trail - none | -15.28 | [-32.68, +1.23] | 6% | -18.5pp | 3% |
| h2h | stop_0.20 - none | -6.41 | [-14.69, +1.20] | 9% | -18.4pp | 6% |
| h2h | stop_0.30 - stop_0.20 | +5.61 | [+0.08, +12.11] | **95%** | +16.7pp | **96%** |
| h2h | stop_0.30 - none | -0.80 | [-6.80, +4.10] | 44% | -1.7pp | 43% |
| totals | stop_0.20 - none | +6.63 | [-0.38, +12.72] | **94%** | +14.3pp | **97%** |
| totals | stop_0.20 - deployed | +1.80 | [-3.03, +4.97] | 76% | +5.4pp | **91%** |
| totals | stop_0.20 - stop_0.30 | +1.33 | [-4.73, +6.30] | 68% | +5.1pp | 86% |

Jackknife (drop each position in turn): all three key comparisons are **sign-stable**.

## Changes made

1. **`STOP_LOSS_MOVE` 0.20 -> 0.30** for h2h/spread; `STOP_LOSS_MOVE_BY_BET_TYPE`
   keeps totals at 0.20. 0.30 for h2h is also the **minimum-regret** point (|s-p| is
   smallest there), which matters because the h2h recovery rate rests on n=22.
2. **Removed the totals time ramp**, replaced by `STOP_LOSS_CONFIRM_CHECKS = 2`.
   The ramp was added after position #315 (a thin ~24c quote spike at the end of the
   1st inning closed a position on a game that finished well over). That incident was
   real, but the ramp was the wrong fix: it widened the stop across the entire early
   game to defend against a single-tick artifact, costing -5.4pp on totals. A
   confirmation counter defends against the artifact directly, for ~30s of exposure.
3. **Trailing stop stays off.** Now measured at -18.5pp of equal-weighted ROI.
4. **Added `positions.exit_price` / `positions.trigger_price`** so exit slippage is a
   direct read instead of an inference.
5. **`_achievable_exit_price()` now treats an empty book as no quote**, not as a price
   of 0.00.

## THE VALIDATION THAT MATTERS

Every backtest row is positive while realised P&L is **-$7.23**. That gap was
interrogated before any of the above was acted on.

**Control group.** 36 positions where reality settled naturally AND the simulated
no-stop policy also held to settlement — no policy difference, so any gap is pure
modelling error:

    n=36   ACTUAL $+2.37   SIM $+3.64   gap $+1.27
    entry fees actually paid: $1.27   (the sim charges none)
    gap after crediting fees: $0.00

**Exactly zero.** The model is not optimistic; it misses only entry fees.

**The gap is real, and it is policy history.** Risk management changed four times:

| as-run policy | n | staked | ACTUAL | SIM | gap |
|---|---|---|---|---|---|
| none (< 07-22) | 19 | $30.17 | +$2.43 | +$4.83 | $2.40 |
| trailing only (07-22 .. 08-07) | 20 | $24.41 | -$5.14 | +$9.79 | **$14.93** |
| both (08-09 .. 08-16) | 53 | $45.69 | **-$4.51** | **-$3.48** | $1.03 |

The modern era — 53 positions, today's code — **reconciles within fees**. The July
era does not and cannot: `TRAILING_STOP_ARM_MOVE` went 0.10 -> 0.15 -> a 0.20/0.08
ramp, and `LOCK_FRACTION` 0.20 -> 0.35. Replaying today's trailing stop against July
positions exits trades the real one never armed on. That $14.93 is the model being
wrong about July, not about the strategy.

Where the -$7.23 actually went:

    stop_loss exits      n=28  staked $24.23   -$14.51  (-59.9%)
    trailing_stop exits  n=23  staked $25.40   - $1.20  ( -4.7%)
    settled naturally    n=36  staked $42.36   + $2.37  ( +5.6%)
    manual_close         n=5   staked $ 8.28   + $6.11  (+73.8%)

Worst single day: 2026-07-21, 8 positions settled for **-$9.50**, during the window
when the coupling bug meant no risk management ran at all.

## Skeptic review

- **The h2h conclusion rests on 22 observations.** SE on a 31.8% rate at n=22 is
  ~10pp, and the s-p margin is -0.099 — about 1 sigma on that statistic alone. It is
  the agreement across three different statistics, not any one of them, that
  justifies acting.
- **0.30 was selected post hoc.** Eight thresholds were tested and the winner's
  p-value is quoted. The defensible claim is "loosen h2h", not "0.30 exactly" — which
  is why minimum-regret was used to pick the value rather than the point estimate.
- **Only 53 of 92 positions came from the current codebase.** The h2h/totals split on
  that subset alone is suggestive, not established.
- **The in-sample +26.1% figure for the new config is optimistic** by construction.
- **Position 246 ($8.46, 4x the next-largest h2h stake) was excluded** as a sizing
  bug, on the user's report. Note this made the h2h conclusion STRONGER, since 246
  was the most stop-favourable observation in that book — worth stating plainly
  because excluding a datapoint that happens to help the conclusion deserves
  scrutiny. The jackknife shows no single position flips any sign either way.
- **Removing the ramp reintroduces exposure to #315-type spikes** if
  `STOP_LOSS_CONFIRM_CHECKS` is ever set to 1 or the monitor interval grows.

## Next

**Re-run 2026-08-31, filtered to `entered_at >= 2026-08-17`**, so the comparison runs
against one consistent codebase. By then `exit_price`/`trigger_price` will make
slippage a measurement rather than an inference — the first estimate of it was wrong
by 12c because it conflated the totals ramp with execution cost.
