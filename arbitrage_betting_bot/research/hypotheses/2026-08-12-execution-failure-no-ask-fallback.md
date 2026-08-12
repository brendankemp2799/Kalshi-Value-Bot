---
id: 2026-08-12-execution-failure-no-ask-fallback
date: 2026-08-12
status: open
source: performance-analyst
n_at_time: 40
---

## Observation

Direct query of `positions` (`is_paper=0`, read-only — not something `research/metrics.py`
surfaces, since `load_positions()` filters `WHERE execution_status != 'failed'` before any
stat is computed) shows **249 total live order attempts across the whole ~3.5-week live
history** (2026-07-19 through 2026-08-12), split:

- `execution_status='failed'`: **193** (77.5%)
- `execution_status='submitted'`: **56** (22.5%) — this is exactly the 40 settled + 16 open
  positions `summary_report()` reports; the other 193 attempts are invisible to every
  number in the normal weekly review.

`failure_reason` for those 193 (n=193, full history, not a single scan):

| reason | n | % of failures |
|---|---|---|
| GTC mid unfilled after 30s — no liquidity at mid, no ask fallback | 95 | 49.2% |
| GTC mid HTTP 410 — mid_only, no ask fallback | 40 | 20.7% |
| GTC mid unfilled after 900s | 26 | 13.5% |
| GTC mid HTTP 429 | 13 | 6.7% |
| GTC mid unfilled after 600s — maker_only, not trying ask step | 6 | 3.1% |
| GTC ask unfilled after 30s — no resting liquidity at ask | 4 | 2.1% |
| No resting volume — order cancelled with zero fill | 3 | 1.6% |
| GTC mid HTTP 400 | 3 | 1.6% |
| (null) | 3 | 1.6% |

The three reasons that explicitly say "no ask fallback" / "not trying ask step" account for
**141/193 = 73.1% of all execution failures**. These are not rate-limit blips or API errors —
they are cases where the bot rested an order at mid/maker price, it didn't fill, and the code
path never tried crossing the spread to the ask.

Breaking failures down by `bet_type`: `totals` 131 failed / 159 attempted (**82.4% failure
rate**) vs `h2h` 59 failed / 84 attempted (**70.2%**) vs `spread` 3/6 (50%, n too small to
read). By `sport`: 175/193 (90.7%) of all failures are `soccer_usa_mls`.

The newly-widened `book_probability_log` (238 rows with the new columns populated, all from a
single scan — `scan_id='2026-08-12T04:48:52.434025'`, every row stamped
`scanned_at='2026-08-12T05:04:01.031187'` — so this is one snapshot in time, not an
accumulated history yet) cross-validates the mechanism at the candidate level: of 27
candidates that reached execution this scan, 21 were `status='execution_failed'` and **all
21 were `maker_only=1`**; the 6 that succeeded (`status='value'`) were 5 maker_only + 1 not.
Every failed candidate this scan carried reason `"GTC mid unfilled after 900s"` or `"GTC mid
HTTP 429"` — consistent with the historical `positions.failure_reason` breakdown above.

## Hypothesis

The bot's realized live sample (56 order attempts that ever got a fill, 40 settled) is a
small, execution-mechanics-filtered subset of a much larger population of candidates that
actually cleared the edge threshold (249 attempts total) — and the filtering is dominated by
a structural "rests at mid/maker price, never crosses to ask" behavior (73.1% of failures),
not by rate-limiting, API errors, or the edge disappearing. This should suppress realized
trade volume disproportionately for `totals` (82.4% failure rate) and `soccer_usa_mls`
(90.7% of all failures), meaning the sport/bet-type breakdowns in `summary_report()` are
computed over a volume-suppressed, execution-biased sample — not necessarily a
representative one — even before asking whether the *edge estimate itself* is well
calibrated.

## Suggested experiment

Quant Research Agent: for the 141 "no ask fallback" failures (`positions.failure_reason` LIKE
'%no ask fallback%' OR '%not trying ask step%'), reconstruct what a same-side taker fill at
the recorded `market_price` (ask) would have cost net of `KALSHI_TAKER_FEE_RATE_ESTIMATE`,
and compare that reconstructed net edge to `config.MIN_EDGE`/the effective per-bet-type
minimum:

- **Confirms** (this is real missed volume worth someone eventually fixing — though not by
  this agent or by the Quant Research Agent editing `execution/`): a large share of the 141
  would still have cleared the net-of-taker-fee edge bar had they crossed to ask — i.e., the
  no-fallback behavior is leaving edge on the table, not just avoiding low-edge fills.
- **Refutes**: most of the 141 would NOT clear the taker-fee-adjusted bar at the ask price
  actually available — in which case resting at mid and giving up rather than chasing was the
  economically correct choice, and this is a non-issue.

As a secondary check: compare the realized entry-price distribution of the 56 filled
`totals`/`h2h` positions against the (once more scans accumulate) `book_probability_log`
population of all `totals`/`h2h` candidates that cleared the edge bar, to see whether filled
trades cluster at unusually favorable prices (selection effect) — see the companion
hypothesis `2026-08-12-totals-fill-rate-selection-bias.md`.

## Known caveats going in

- The 193/56 split and the `failure_reason` breakdown come from `positions` directly (a
  column `summary_report()` doesn't expose), not from the widened `book_probability_log` —
  the widened table only has one scan's worth of `execution_failed` rows (n=21) so far and
  is cited here only as same-scan corroboration, not as the primary evidence.
- This is a hypothesis about **execution mechanics**, not about whether the edge model is
  well calibrated — it should not be read as contradicting or confirming the existing H2H/
  totals or MLB/MLS findings, only as a reason those realized samples might be smaller and
  more selected than the raw candidate population.
- No code changes belong to this agent or the Quant Research Agent — `execution/` and
  `main.py` are off-limits per project rules. This hypothesis is purely "how much edge is
  being left on the table, quantified," for whoever eventually decides whether to act on it.
- Related to, but distinct from, the still-open
  `2026-08-09-maker-fill-win-rate-roi-mismatch.md` hypothesis: that one is about the payout
  asymmetry among the 9 maker fills that *did* happen; this one is about the ~193 maker/mid
  attempts that *didn't*. Worth the Quant Research Agent treating them as related context for
  each other rather than fully independent.
- Not previously investigated — no `research/failed_ideas/` directory exists yet in this
  project, so nothing has been rejected on this topic before.
