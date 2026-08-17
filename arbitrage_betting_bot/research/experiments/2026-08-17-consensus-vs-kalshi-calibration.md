---
experiment_id: 2026-08-17-consensus-vs-kalshi-calibration
date: 2026-08-17
hypothesis_ref: ""
status: inconclusive-underpowered
baseline: "Kalshi's own market price as a predictor of the same outcome"
dataset: "book_probability_log, rows with actual_outcome backfilled, ONE row per (kalshi_ticker, kalshi_side) -- the last scan before kickoff"
training_period: ""
validation_period: ""
out_of_sample_period: ""
n_trades: 338
roi: null
pnl: null
sharpe: null
max_drawdown: null
win_rate: null
fees_included: false
slippage_assumptions: "n/a -- scores probability estimates against Kalshi-resolved outcomes, not a trading simulation"
execution_assumptions: "n/a"
---

## Hypothesis under test

The directional strategy bets when the de-vigged sportsbook consensus disagrees
with Kalshi's price. That is only profitable if the consensus is the BETTER
estimator of the same outcome. This has never been checked at scale. Follow-on
from `2026-08-11-book-weight-validation.md`, which said to revisit once
`book_probability_log` had accumulated "several hundred" events.

## Method

`book_probability_log` stores, for the same outcome: `consensus_prob`,
`kalshi_price`, and (backfilled by `execution/auto_settle.py`) `actual_outcome`
= 1.0 iff Kalshi's settled result matched that row's `kalshi_side`. Brier score
`mean((p - outcome)^2)` for each estimator, plus a paired per-row difference.

**One row per (ticker, side), taking the last scan before kickoff.** This is the
critical methodological point -- see below.

## THE SAMPLE-SIZE TRAP (the main finding of this experiment)

The naive query returns **34,453 rows**, which looks like an enormous sample and
is not one. Every scan re-logs every candidate, so a single game accumulates
~102 rows, all sharing one eventual outcome:

    rows                          34,453
    distinct market+side outcomes    338   <- the real sample
    rows per outcome               101.9
    most-scanned single market       333
    days covered                       5

Treating rows as independent inflates t-statistics by ~sqrt(102) = 10x. Measured
directly -- the same tests, before and after de-clustering:

| cut | naive t (34,453 rows) | corrected (338 outcomes) |
|---|---|---|
| overall paired Brier diff | +17.87 "SIGNIFICANT" | +0.97 not significant |
| <1c disagreement | +18.17 | ~+1.8 |
| 2-5c disagreement | +10.29 | ~+1.0 |
| bot-acts band, 1-2% edge | -3.34 | ~-0.3 (n=15 real games) |

Every "significant" result in the naive version was an artifact.

## Results (n=338 independent outcomes, base rate 46.4%)

| estimator | Brier |
|---|---|
| sportsbook consensus | 0.2491 |
| Kalshi market price | **0.2485** |
| always predict the base rate | 0.2487 |

Paired difference (consensus - Kalshi): **+0.00057, t = +0.97, not significant.**

Bands the bot actually trades in:

| band | n | hit rate | Kalshi implied | z |
|---|---|---|---|---|
| 1-2c edge | 15 | 40.0% | 46.0% | -0.47 |
| 2c+ edge | 3 | 66.7% | 44.7% | +0.77 |

**Minimum detectable gap at n=338: 0.00114. Observed gap: 0.00057.**

## Conclusion

**Inconclusive because underpowered -- by a factor of 2.** This is not "no
difference found"; the sample cannot resolve a difference of the size that
appears to be present. Do not cite either direction as a result.

What is worth carrying forward:

1. **Direction is consistent.** Kalshi edges out consensus on every cut, every
   disagreement size, in both clustered and de-clustered versions. Consistency
   across slices is weak evidence, not none.
2. **Neither estimator clearly beats a constant guess.** Consensus (0.2491) is
   marginally WORSE than always predicting the base rate (0.2487); Kalshi
   (0.2485) is marginally better. Both are near-uninformative on this sample.
3. **Note kalshi_price is the TAKER price**, inclusive of spread, so the
   comparison is biased against Kalshi -- and Kalshi still edges it. That makes
   the direction slightly more notable, not less.

## What would settle it

~1,350 independent outcomes to resolve a 0.00057 gap. At the current ~68
outcomes/day that is **roughly two more weeks**. The backfill is automatic; this
needs time, not work.

If it does resolve against the consensus, the implication is severe: thin
"edges" would be measurement error rather than signal, which would also supply
the mechanism for the separate observation that sub-2% edge bets underperform
(see the MIN_EDGE discussion, itself only ~1.3 sigma and not yet actionable).

## Skeptic review

Self-flagged; no production change rides on this.

- **The de-clustering choice is a judgment call.** "Last scan before kickoff" is
  defensible (closest to the acted-on state) but not the only option; first-scan
  or a per-market average would give slightly different numbers. None would
  change the power problem.
- **5 days is short and seasonally narrow.** All MLB/MLS/EPL in one mid-August
  window. Sport mix and market maturity could both shift the result.
- **The 3 observations in the 2c+ band are worthless** and are reported only to
  show how thin the actionable region is, not as an estimate of anything.
- **Do not re-run this before ~2026-08-31.** The Aug 11 experiment said "revisit
  at several hundred events"; we now have several hundred and it is still
  underpowered. The bar should be minimum-detectable-effect, not row count.
