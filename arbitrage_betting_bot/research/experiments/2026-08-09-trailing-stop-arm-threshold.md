---
experiment_id: 2026-08-09-trailing-stop-arm-threshold
date: 2026-08-09
hypothesis_ref: research/hypotheses/2026-08-09-trailing-stop-premature-exit.md
status: passed
baseline: value_edge strategy, TRAILING_STOP_ARM_MOVE=0.10, TRAILING_STOP_LOCK_FRACTION=0.35 (config.py as of 2026-08-09 pre-fix)
dataset: "positions table, is_paper=0, close_reason='trailing_stop', settled"
training_period: "2026-07-23 to 2026-08-09 (all trailing-stop closes to date)"
validation_period: ""
out_of_sample_period: "not run — see caveats"
n_trades: 10
roi: null
pnl: -0.13
sharpe: null
max_drawdown: null
win_rate: 0.5
fees_included: true
slippage_assumptions: "actual fill prices used (real Kalshi order records), not modeled"
execution_assumptions: "actual historical closes, poll-based (30s) monitoring cadence as deployed"
---

## Hypothesis under test

`TRAILING_STOP_ARM_MOVE = 0.10` arms on ordinary single-inning price noise, not just
real trend moves, converting favorable-direction positions into breakeven/loss
outcomes. See linked hypothesis file for full detail.

## Method

Not a simulated backtest — a direct read of every real `close_reason='trailing_stop'`
position from the live `positions` table (droplet, `storage/betting_bot.db`), via:

```sql
SELECT id, market_ticker, sport, market_price entry, peak_price, kalshi_close_price exit, pnl, stake
FROM positions WHERE close_reason='trailing_stop' AND is_paper=0 ORDER BY settled_at
```

For position #262 specifically, cross-referenced against Kalshi's own 1-minute
candlesticks (`KalshiClient.fetch_candlesticks`, `period_interval=1`) for the market
`KXMLBTOTAL-26AUG091435BALTEX-9` across the full pre-game-to-settlement window
(11:00-20:00 UTC) to confirm the peak and retracement were a genuine single-minute
whipsaw, not a data artifact.

## Results

n=10 trailing-stop closes out of 39 total settled live trades (26%). Every one moved
favorably after entry (100% positive move-to-peak). Aggregate pnl across all 10:
**-$0.13**. Split by move-to-peak:

- move-to-peak >= 0.16 (n=5: #231, #238, #247, #243, #258): pnl = +0.005 +0.277 +0.069
  +0.008 -0.088 = **+$0.271**
- move-to-peak < 0.16 (n=5: #235, #229, #245, #251, #262): pnl = +0.008 -0.035 -0.178
  -0.208 +0.008 = **-$0.405**

Position #262's candlesticks confirm the mechanism directly: bid spiked to a peak of
54c in one 1-minute candle (18:43 UTC), then retraced to 43-47c the very next candle —
the trailing stop armed and closed on that single whipsaw. The market then continued
to 96-99c within the next ~65 minutes as the actual game outcome (Over, confirmed by
the user's own report of 10 runs by the bottom of the 5th) got priced in.

## Conclusion

**Confirmed, with caveats.** The <0.16 move-to-peak bucket is net negative even with
the "protection" active, while the >=0.16 bucket is solidly positive — consistent with
the arm threshold being tight enough to catch noise, not just trend. Applied fix:
`TRAILING_STOP_ARM_MOVE` raised from 0.10 to 0.15 in `config.py` (deployed to the
droplet 2026-08-09) — cleanly excludes the four clear-noise cases (0.10, 0.10, 0.12,
0.13) from ever arming, while still capturing every case at 0.16 and above.
`TRAILING_STOP_LOCK_FRACTION` (0.35) left unchanged — the large, clearly-real moves
that already arm under the new threshold look reasonable as-is.

Not touched, and worth a formal follow-up experiment once more trailing-stop closes
accumulate under the new threshold: whether 0.15 is actually optimal vs. some other
value, and what the *actual* counterfactual outcome would have been for the 4
now-excluded cases (i.e., replaying their full intraday candlesticks against
`STOP_LOSS_MOVE=0.20` and natural settlement) — this pass used only the recorded
peak/entry/exit for 9 of the 10 rows, not full intraday replay.

## Skeptic review

Not yet run through the formal Skeptic Agent — this fix was fast-tracked directly by
the user's request ("this needs to be fixed immediately") given live-capital impact,
bypassing the normal Quant Research -> Skeptic pipeline for speed. Known weaknesses a
Skeptic would likely flag, noted here proactively:

- **Sample size**: n=10 total, n=5 per bucket. Thin for a permanent parameter choice.
- **No out-of-sample validation**: the 0.15 threshold was chosen by inspecting the
  same 10 trades it's meant to fix (in-sample) — there's no held-out data to confirm
  it generalizes rather than just fitting these 10 points.
- **Only 1 of 10 rows (#262) verified against real intraday price paths.** The other 4
  small-move cases are assumed (not confirmed) to be the same whipsaw mechanism.
- **Regime dependence**: all 10 trades are MLB/MLS totals/spreads from a 2.5-week
  window — unclear whether 0.15 is well-calibrated for other sports/bet types or
  different volatility regimes (e.g. a genuinely slower-moving market where 0.10 was
  appropriately tight).
- Should be revisited by the normal pipeline once n grows, to either confirm 0.15 or
  find a better-supported value.
