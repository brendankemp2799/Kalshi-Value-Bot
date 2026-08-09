---
id: 2026-08-09-maker-fill-win-rate-roi-mismatch
date: 2026-08-09
status: open
source: performance-analyst
n_at_time: 39
---

## Observation

`research/metrics.py summary_report()`'s `by_fill_type` breakdown, over the full
n_settled=39 live trades now visible for the first time from this environment, shows:

- `maker`: n=9, roi_pct=-25.69%, win_rate_pct=66.7% (6 wins / 9 trades)
- `taker`: n=30, roi_pct=-20.57%, win_rate_pct=33.3% (10 wins / 30 trades)

Maker fills win exactly twice as often as taker fills (66.7% vs 33.3%) but post a
*worse* ROI (-25.69% vs -20.57%). Both fill types are currently net-negative, but the
win-rate/ROI relationship is inverted from what you'd expect if maker fills were simply
"better" (better price → better ROI *and* comparable-or-better win rate). This
combination — high win rate, worse ROI — implies the win/loss payout is asymmetric for
maker fills specifically: infrequent losses must be large relative to frequent small
wins.

## Hypothesis

Maker-filled trades are systematically entered at higher-probability / lower-payout
price points than taker-filled trades (limit orders more likely to fill on the
favorite/high-price side of the book), so their few losses (3 of 9) wipe out a
disproportionate share of their many small wins (6 of 9). If so, win_rate alone is a
misleading performance signal for maker fills, and the entry-price distribution and
per-trade win/loss payout ratio should differ materially between the two fill types.

## Suggested experiment

Quant Research Agent: for the 9 maker and 30 taker settled trades, compare (a) mean
`market_price` (entry price) by `fill_type`, and (b) mean winning-trade pnl-as-%-of-stake
vs mean losing-trade pnl-as-%-of-stake, by `fill_type`. 

- **Confirms** the hypothesis: maker trades cluster at meaningfully higher entry prices
  and/or show a much larger win/loss payout asymmetry than taker trades.
- **Refutes** it: entry-price distributions and payout asymmetry are similar across fill
  types — in which case the maker ROI figure is just noise driven by which 3 of 9 maker
  trades happened to lose.

## Known caveats going in

- n=9 for maker fills is small — only 3 losing trades drive the entire -25.69% ROI
  figure; one different outcome swings it substantially. Treat as a lead, not a
  finding, until tested.
- Overall n_settled=39 is still below the ~30-trade threshold `metrics.py` itself flags
  as low-confidence for sub-breakdowns this granular (here split further into 9 and 30).
- Not previously investigated: no prior hypothesis on `fill_type` exists in
  `research/hypotheses/`, and there is no `research/failed_ideas/` entry to check
  against (that directory doesn't exist yet — nothing has been rejected so far).
- This is unrelated to the already-known H2H/totals, MLB/MLS, and 1.5-3% edge-bucket
  findings — worth checking whether fill_type correlates with bet_type or edge bucket
  (e.g. if H2H trades disproportionately fill as maker) before treating this as an
  independent effect.
