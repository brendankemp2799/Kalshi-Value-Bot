---
id: 2026-08-09-trailing-stop-premature-exit
date: 2026-08-09
status: confirmed      # investigated directly this session, not via the agent pipeline — see note at bottom
source: user-reported (Baltimore/Texas Over 8.5, position #262)
n_at_time: 39
---

## Observation

User flagged position #262 (`KXMLBTOTAL-26AUG091435BALTEX-9`, Over 8.5, entered 44c)
as "closed for a loss" while the real game was already well past the total (10 runs
by the bottom of the 5th). Investigation of Kalshi's own 1-minute candlesticks for
that market showed:

- Entry 11:56 UTC @ 44c, price flat at 46-47c for the entire pre-game window.
- 18:43 UTC (game underway): a single 1-minute candle spiked the bid to a **peak of
  54c**, then the very next candle (18:44-18:45) whipsawed back to 43-47c.
- `evaluate_trailing_stop()` armed on that spike — `TRAILING_STOP_ARM_MOVE = 0.10`
  means a peak just 10c above entry is enough to arm — and closed the position at
  46c at 18:44:49 once price ticked back to the (armed) stop level.
- Two to three minutes later the real move continued: 55c (18:46) -> 65c (18:47) ->
  80c (19:19) -> **96-99c by 19:49**, as the actual scoring outburst got priced in.
- Net result: pnl = +$0.0078 (not actually a loss, but essentially all of the edge
  was left on the table — the position was worth close to full value minutes after
  being stopped out).

Pulling every live trailing-stop close to date (`close_reason='trailing_stop'`,
`is_paper=0`, n=10 of 39 total settled trades — 26% of all settled trades):

| id  | ticker                              | entry | peak | exit | pnl     | move-to-peak |
|-----|--------------------------------------|-------|------|------|---------|--------------|
| 231 | KXMLBTOTAL-26JUL231715AZSTL-9         | 0.46  | 0.68 | 0.38 | +0.005  | 0.22 |
| 235 | KXMLSTOTAL-26JUL25CLBCIN-4             | 0.46  | 0.59 | 0.44 | +0.008  | 0.13 |
| 238 | KXMLSTOTAL-26JUL25NYRBCLT-4             | 0.38  | 0.82 | 0.48 | +0.277  | 0.44 |
| 229 | KXMLSGAME-26JUL25CLBCIN-TIE             | 0.21  | 0.31 | 0.25 | -0.035  | 0.10 |
| 245 | KXMLBSPREAD-26JUL281910CLECIN-CIN2     | 0.31  | 0.43 | 0.31 | -0.178  | 0.12 |
| 247 | KXMLBGAME-26JUL312210BOSLAD-LAD         | 0.42  | 0.82 | 0.44 | +0.069  | 0.40 |
| 243 | KXMLSTOTAL-26AUG01PORSEA-3               | 0.60  | 0.88 | 0.66 | +0.008  | 0.28 |
| 251 | KXMLBTOTAL-26AUG042140DETSEA-9           | 0.45  | 0.58 | 0.43 | -0.208  | 0.13 |
| 258 | KXMLBGAME-26AUG072010CHCKC-KC             | 0.39  | 0.55 | 0.40 | -0.088  | 0.16 |
| 262 | KXMLBTOTAL-26AUG091435BALTEX-9           | 0.44  | 0.54 | 0.47 | +0.008  | 0.10 |

Every single one of these 10 positions moved favorably after entry (peak > entry in
100% of cases, average move-to-peak ~0.19). Yet aggregate realized pnl across all 10
is **-$0.13** — roughly breakeven-to-negative, despite every trade being "right" in
direction. The five trades that armed on the smallest moves (move-to-peak 0.10-0.13:
#235, #229, #245, #251, #262) contributed -$0.41 combined; three of those five (#229,
#245, #251) show a net *loss* even though the trailing stop was "protecting" them.
`#245` is the starkest: its calculated stop level was 35.2c but it actually filled at
31.0c — exactly back at entry — because the poll-based close (30s cadence) chased a
fast-moving price during the same kind of whipsaw seen in #262.

## Hypothesis

> `TRAILING_STOP_ARM_MOVE = 0.10` is small enough to arm on ordinary single-inning
> price noise (not a real trend), converting favorable-direction positions into
> near-breakeven or losing outcomes instead of letting them run to their real value.
> Raising the arm threshold should reduce false arms without reducing loss protection,
> because `config.STOP_LOSS_MOVE = 0.20` (adverse move from entry, stateless,
> independent of arming) already backstops genuine reversals regardless of whether the
> trailing stop ever arms.

## Suggested experiment

Re-run `evaluate_trailing_stop()` against the recorded peak/entry/exit history for all
10 trailing-stop closes (and ideally full intra-game candlestick paths, as pulled for
#262) at several `TRAILING_STOP_ARM_MOVE` values (0.10 current, 0.15, 0.20) and compare
aggregate pnl. Confirming vs. refuting: a higher threshold should exclude the four
0.10-0.13 cases from arming at all — check whether their un-armed outcomes (settlement
or `STOP_LOSS_MOVE`-triggered exit) would have plausibly beaten what the trailing stop
actually delivered. Full validation needs each ticker's intraday candlesticks (only
pulled for #262 so far) to know what would *actually* have happened if unarmed, not
just the recorded peak.

## Known caveats going in

- n=10 trailing-stop closes total, n=5 in the "small move" bucket this hypothesis
  targets — thin evidence for a permanent parameter choice, even though the direction
  is consistent and the mechanism (single-candle whipsaw vs. real trend) is directly
  observed in #262's own candlesticks, not inferred.
- Only #262 has been checked against real intraday price history end-to-end; the other
  9 rows above are read directly from `positions` (entry/peak/exit/pnl), not re-verified
  against Kalshi candlesticks — the "single whipsaw" mechanism is confirmed for #262,
  assumed (not verified) for the other 4 small-move cases.
- Does not touch `TRAILING_STOP_LOCK_FRACTION` (0.35) — the larger, clearly-real moves
  (#231, #238, #247, #243, move-to-peak 0.22-0.44) all still arm and still look
  reasonable; this hypothesis is specifically about the arm threshold being too tight,
  not the lock fraction once armed.
- A production fix (`TRAILING_STOP_ARM_MOVE` 0.10 -> 0.15) was applied directly this
  session ahead of the normal Quant Research -> Skeptic pipeline, given the user's
  urgency and live-capital impact — see `research/experiments/2026-08-09-trailing-stop-arm-threshold.md`
  for the applied-fix record. A fuller backtest against intraday candlesticks for all
  10 (or more, as new trailing-stop closes accumulate) would still be worth running
  through the normal pipeline to confirm 0.15 (vs. some other value) is well-chosen,
  and to catch anything this fast-tracked pass missed.
