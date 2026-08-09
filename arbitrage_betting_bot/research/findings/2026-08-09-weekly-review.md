---
date: 2026-08-09
triggered_by: threshold
n_settled_live: 39
n_settled_since_last: 39 (recorded delta per last_snapshot.json — see anomaly #1, this is not real trading velocity)
---

## Summary snapshot

`python3 research/metrics.py` against the live DB (`is_paper: false`), same-day as the
`TRIGGER_2026-08-09.md` file this review was woken up by:

```json
{
  "generated_at": "2026-08-09T20:12:44.690020+00:00",
  "is_paper": false,
  "n_settled": 39,
  "n_open": 8,
  "roi_pct": -22.12,
  "win_rate_pct": 41.0,
  "sharpe": {"value": -0.258, "n": 39},
  "max_drawdown": {"value": 0.1348, "n_snapshots": 17},
  "by_sport": [
    {"sport": "soccer_usa_mls", "n": 20, "roi_pct": 5.43, "win_rate_pct": 50.0},
    {"sport": "baseball_mlb", "n": 19, "roi_pct": -38.41, "win_rate_pct": 31.6}
  ],
  "by_bet_type": [
    {"bet_type": "totals", "n": 25, "roi_pct": 6.82, "win_rate_pct": 56.0},
    {"bet_type": "h2h", "n": 12, "roi_pct": -79.32, "win_rate_pct": 16.7},
    {"bet_type": "spread", "n": 2, "roi_pct": -21.33, "win_rate_pct": 0.0}
  ],
  "by_fill_type": [
    {"fill_type": "taker", "n": 30, "roi_pct": -20.57, "win_rate_pct": 33.3},
    {"fill_type": "maker", "n": 9, "roi_pct": -25.69, "win_rate_pct": 66.7}
  ],
  "by_close_reason": [
    {"close_reason": "trailing_stop", "n": 10, "roi_pct": -0.9, "win_rate_pct": 60.0},
    {"close_reason": "manual_close", "n": 5, "roi_pct": 73.84, "win_rate_pct": 100.0}
  ],
  "edge_calibration": [
    {"edge_bucket": "1.5-3%", "n": 25, "roi_pct": -23.88, "win_rate_pct": 36.0},
    {"edge_bucket": "3-5%", "n": 12, "roi_pct": 18.13, "win_rate_pct": 58.3},
    {"edge_bucket": "8%+", "n": 2, "roi_pct": -62.91, "win_rate_pct": 0.0}
  ],
  "fees_and_fills": {"n": 39, "maker_pct": 23.1, "avg_fee_paid": 0.046}
}
```

## New hypotheses this run

- `research/hypotheses/2026-08-09-maker-fill-win-rate-roi-mismatch.md` — maker fills
  win twice as often as taker fills (66.7% n=9 vs 33.3% n=30) but have worse ROI
  (-25.69% vs -20.57%), suggesting a payout-asymmetry effect worth testing against
  entry price. This is genuinely new ground — no prior hypothesis or `failed_ideas/`
  entry touches `fill_type`.

Everything else this run is **confirmation of already-known findings, not new
territory** (see below) — no additional hypothesis files written for those.

## Anomalies / flags

1. **The threshold trigger is a data-visibility artifact, not a real 39-trade-in-hours
   surge.** `research/findings/last_snapshot.json` (written earlier today, 18:43 UTC,
   during a prior broken-environment run per
   `research/findings/2026-08-09-first-review.md`) recorded `n_settled: 0` because that
   run's `storage/betting_bot.db` had zero `is_paper=0` rows reachable. This run (20:12
   UTC, same day) correctly sees the real production data: `n_settled: 39`. The
   `check-thresholds` diff (`39 - 0 = 39 >= 8`) is real arithmetic on a bad baseline,
   not 39 genuinely new settled trades since the last real review. **True new-trade
   count vs. the last known-good state (project ground truth: 38 settled, 18 MLB / 20
   MLS) is ~1 trade** — this run shows 19 MLB / 20 MLS, i.e. one more MLB trade
   settled and nothing else. Flagging clearly so this isn't read as "trading volume
   spiked."
2. **`by_close_reason` only covers 15 of 39 settled trades** (10 `trailing_stop`, 5
   `manual_close`); the other 24 have `close_reason IS NULL` (confirmed via direct
   schema check), which most likely means ordinary market-resolution settlement rather
   than a stop or manual intervention — not a data-quality bug, just a reminder that
   `by_close_reason` in the summary is a partial slice (38% of trades) and shouldn't be
   read as "N=39 categorized."
3. **Max drawdown is 13.48% (17 snapshots)** — below the 15% alert threshold that would
   independently trigger a review, but close enough to be worth watching next run.

## Confirmation of prior known findings (with updated numbers, n=39)

No new hypotheses needed for these — already on record, this run's numbers are
consistent with (not contradicting) them:

- **H2H much worse than totals**: h2h roi -79.32% (n=12, win 16.7%) vs totals +6.82%
  (n=25, win 56.0%). Gap has, if anything, widened since the qualitative finding was
  first made.
- **MLB underperforms MLS**: MLB -38.41% (n=19, win 31.6%) vs MLS +5.43% (n=20, win
  50.0%) — MLS is now net *positive*, sharpening the contrast.
- **1.5-3% edge bucket carries most volume and is net negative**: n=25 of 39 settled
  trades (64% of volume) at -23.88% ROI, vs the 3-5% bucket at +18.13% (n=12). Still
  holds with more data.
- Un-armed trailing stop as historical dominant loss source: not independently
  re-checked this run — `positions` doesn't expose an "armed" flag through
  `metrics.py`'s current breakdowns, and the feature has already shipped, so this
  would need a dedicated experiment (out of scope for this pass) rather than a
  metrics.py re-read.

## Bottom line

Real signal this run: one new hypothesis (`fill_type` payout asymmetry, n=9 maker /
n=30 taker — small, flagged as such). Everything else is the same story as before,
now with slightly more data (n=39 vs. the previously-cited n=38) and, for the first
time, actually visible from this environment rather than reasoned about secondhand.
Sample size is still small in every sub-breakdown; treat accordingly.
