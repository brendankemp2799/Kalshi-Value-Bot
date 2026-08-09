---
experiment_id: YYYY-MM-DD-short-slug
date: YYYY-MM-DD
hypothesis_ref: research/hypotheses/YYYY-MM-DD-short-slug.md
status: proposed        # proposed | running | passed | rejected
baseline: value_edge strategy, current config.py thresholds as of this date
dataset: ""             # e.g. "positions table, is_paper=0, sport=baseball_mlb, settled"
training_period: ""
validation_period: ""
out_of_sample_period: ""
n_trades: 0
roi: null
pnl: null
sharpe: null
max_drawdown: null
win_rate: null
fees_included: true
slippage_assumptions: ""
execution_assumptions: ""
---

## Hypothesis under test

(copy from the linked hypothesis file)

## Method

What was actually computed, with which `research/metrics.py` functions or standalone
script (link/paste it — this must be reproducible, not just a described methodology).

## Results

Numbers, not adjectives. Include n at every step — a result on 8 trades and a result
on 80 trades should never be presented the same way.

## Conclusion

Confirmed / refuted / inconclusive, and why. If inconclusive, say what data would be
needed to resolve it (e.g. "revisit once n >= 100").

## Skeptic review

(filled in by the Skeptic Agent — verdict + specific objections checked: look-ahead
bias, data leakage, overfitting, survivorship bias, sample size, multiple-testing,
data quality, fee assumptions, unrealistic fills, unrealistic execution, optimization
against the test set, market-regime dependence)
