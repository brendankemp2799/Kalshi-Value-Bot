---
id: 2026-08-12-totals-fill-rate-selection-bias
date: 2026-08-12
status: open
source: performance-analyst
n_at_time: 40
---

## Observation

`research/metrics.py summary_report()` (n_settled=40) currently shows `totals` as the
best-performing bet type: n=25, ROI +6.82%, win rate 56.0% — vs `h2h` at n=13, ROI -65.88%,
win rate 23.1%. This is a known, already-flagged finding (H2H underperforms totals).

A direct query of `positions` (`is_paper=0`, all attempted live orders, not just the ones
`summary_report()` sees) shows that `totals` also has the **highest execution-failure rate
of any bet type**: 131 failed / 159 total attempts = **82.4%** never filled, vs `h2h` at 59
failed / 84 attempts = **70.2%**, vs `spread` at 3/6 = 50% (n=6, too small to read). In other
words: the 25 settled `totals` trades behind the "+6.82% ROI" number are the ~17.6% of
`totals` candidates that Kalshi actually let through — a much more heavily filtered subset
than the ~30% fill rate for `h2h`.

## Hypothesis

The realized `totals` sample may not be representative of the full `totals` edge population
because it is more heavily selected by fill mechanics than `h2h` is. If orders only fill when
the market moves favorably between placement and the mid-price resting window (rather than
filling roughly at random with respect to eventual outcome), then the 25 filled `totals`
trades could be systematically better-priced (and better-fated) than the 131 that never
filled — meaning `totals`' ROI advantage over `h2h` is partly a fill-selection artifact, not
purely a better-calibrated edge signal for that bet type.

## Suggested experiment

Quant Research Agent: compare the entry-price / edge distribution of the 25 filled
(`execution_status='submitted'`) `totals` positions against the 131 failed `totals` attempts
(same table, `positions.market_price`, `positions.edge`, `positions.kalshi_spread`) —

- **Confirms**: filled `totals` trades cluster at meaningfully more favorable prices (e.g.
  closer to fair value, lower kalshi_spread, or higher recorded edge) than the failed
  attempts as a group — evidence of a real selection effect on top of whatever edge H2H/
  totals calibration exists.
- **Refutes**: filled and failed `totals` attempts look similar on price/edge/spread — in
  which case which ones happened to fill looks closer to random, and the totals ROI advantage
  stands on its own.

If more scans accumulate in the widened `book_probability_log` before this is picked up, a
cleaner version of this test becomes possible: join `book_probability_log.status='value'`
rows to `positions` via `position_id` (once `link_book_probability_to_position()` has
populated it — currently 0 of 238 widened rows have a `position_id` set, since the one scan
logged so far didn't yet get linked) and compare the *scan-time* consensus/edge snapshot,
not just the eventual `positions` row, against contemporaneous rejected/failed candidates
for the same games.

## Known caveats going in

- n=159 total `totals` attempts and n=25 filled is still a modest sample for a
  price-distribution comparison; treat any result as a lead, not a confirmed effect.
- This does not contradict the existing "totals > h2h" finding on realized numbers — it's a
  question about *why*, and specifically about whether that gap would hold up if fill
  mechanics were fixed (see the companion hypothesis
  `2026-08-12-execution-failure-no-ask-fallback.md`) and totals volume increased.
  Fixing the fill mechanics is out of scope for this agent and the Quant Research Agent —
  `execution/`/`main.py` are off-limits.
- `book_probability_log`'s widened columns currently cover exactly one scan (238 rows, one
  `scan_id`, one `scanned_at` timestamp) with zero `position_id` links populated yet, so the
  cleaner join described above isn't runnable today — flagging so the Quant Research Agent
  doesn't expect it to work until more scans have accumulated and linked.
- Not previously investigated — no `research/failed_ideas/` directory exists yet, so nothing
  has been rejected on this topic before.
