---
id: 2026-08-09-maker-fill-win-rate-roi-mismatch
date: 2026-08-09
status: rejected
source: performance-analyst
n_at_time: 39
moved_from: research/hypotheses/2026-08-09-maker-fill-win-rate-roi-mismatch.md
---

## Original hypothesis

Maker-filled trades are systematically entered at higher-probability / lower-payout
price points than taker-filled trades, so their few losses wipe out a disproportionate
share of many small wins — explaining maker's 66.7% win rate but worse -25.69% ROI vs
taker's 33.3% win rate / -20.57% ROI.

## What was actually tested

Ran the suggested experiment: compared mean entry price and mean win/loss pnl-as-%-of-
stake by `fill_type`, n=9 maker / n=30 taker (`positions` table, `is_paper=0`,
`status='closed'`).

- Mean entry price: maker 40.67¢ vs taker 39.00¢ — a 1.7¢ difference, not the
  "meaningfully higher" clustering the hypothesis predicted.
- Mean win-side payout: maker wins average **+81.6%** of stake vs taker wins **+60.3%**
  — maker's win-side payout is *better*, not worse, directly contradicting the
  "lower-payout" half of the hypothesis.
- Mean loss-side payout: maker -100.0% vs taker -86.3% — both are close to full losses
  (typical of these binary-outcome markets), no meaningful asymmetry difference.

## Real explanation found

Pulled all 9 individual maker fills with stake and pnl:

| id | price | stake | pnl | edge | bet_type |
|---|---|---|---|---|---|
| 232 | 0.41 | $1.64 | +$1.27 | 2.3% | totals |
| 233 | 0.41 | $1.64 | +$1.27 | 2.3% | totals |
| 234 | 0.41 | $1.64 | +$1.27 | 2.3% | totals |
| 236 | 0.28 | $0.84 | +$2.16 | 3.2% | totals |
| 231 | 0.46 | $0.92 | $0.00 | 1.8% | totals |
| **246** | **0.47** | **$8.46** | **-$8.46** | **11.7%** | **h2h** |
| 248 | 0.34 | $1.02 | -$1.02 | 3.5% | totals |
| 257 | 0.44 | $1.32 | -$1.32 | 3.8% | totals |
| 262 | 0.44 | $1.32 | +$0.01 | 3.8% | totals |

Position #246 is a single outlier: $8.46 stake (5-10x every other maker fill), the only
h2h bet in the set, edge of 11.7% (3-6x every other maker fill's edge), and it lost in
full. It alone accounts for 45% of all maker capital deployed ($8.46 of $18.80 total)
and, removed, the remaining 8 trades net **pnl=+$3.63 on $10.34 staked = +35.1% ROI** —
a completely different picture from the reported -25.69%.

## Conclusion

**Rejected.** The win-rate/ROI mismatch is not a systematic maker-fill effect — it's
n=9 being thin enough that one atypically large, atypically high-edge outlier trade
swings the whole aggregate. Exactly the "insufficient sample size" failure mode
`research/metrics.py` and the Skeptic checklist both warn about. No entry-price or
payout-asymmetry pattern exists once #246 is set aside.

## Skeptic review

Not run through the formal Skeptic Agent — resolved directly given how clear-cut the
single-outlier explanation is once the per-trade data is inspected (no ambiguity to
adjudicate). Worth flagging for whoever revisits fill_type analysis later: #246 being
both maker AND h2h AND the only large/high-edge fill in this fill-type bucket makes it
worth separately checking whether *h2h maker fills specifically* (a much smaller,
n=1 here) are a distinct, real risk category — one data point can't answer that, but
it's a real difference from the seven totals bets sitting alongside it that happened to
be small and low-edge.
