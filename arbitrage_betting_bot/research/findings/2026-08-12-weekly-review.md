---
date: 2026-08-12
triggered_by: weekly
n_settled_live: 40
n_settled_since_last: 1 (40 vs 39 in last_snapshot.json, generated 2026-08-09T20:15:30Z)
---

## Summary snapshot

`python3 research/metrics.py` against the live DB (`is_paper: false`):

```json
{
  "generated_at": "2026-08-12T05:08:25.865751+00:00",
  "is_paper": false,
  "n_settled": 40,
  "n_open": 16,
  "roi_pct": -19.08,
  "win_rate_pct": 42.5,
  "sharpe": {"value": -0.215, "n": 40},
  "max_drawdown": {"value": 0.149, "n_snapshots": 20},
  "by_sport": [
    {"sport": "soccer_usa_mls", "n": 20, "roi_pct": 5.43, "win_rate_pct": 50.0},
    {"sport": "baseball_mlb", "n": 20, "roi_pct": -33.09, "win_rate_pct": 35.0}
  ],
  "by_bet_type": [
    {"bet_type": "totals", "n": 25, "roi_pct": 6.82, "win_rate_pct": 56.0},
    {"bet_type": "h2h", "n": 13, "roi_pct": -65.88, "win_rate_pct": 23.1},
    {"bet_type": "spread", "n": 2, "roi_pct": -21.33, "win_rate_pct": 0.0}
  ],
  "by_fill_type": [
    {"fill_type": "taker", "n": 31, "roi_pct": -16.29, "win_rate_pct": 35.5},
    {"fill_type": "maker", "n": 9, "roi_pct": -25.69, "win_rate_pct": 66.7}
  ],
  "by_close_reason": [
    {"close_reason": "trailing_stop", "n": 10, "roi_pct": -0.9, "win_rate_pct": 60.0},
    {"close_reason": "manual_close", "n": 5, "roi_pct": 73.84, "win_rate_pct": 100.0}
  ],
  "edge_calibration": [
    {"edge_bucket": "1.5-3%", "n": 26, "roi_pct": -18.19, "win_rate_pct": 38.5},
    {"edge_bucket": "3-5%", "n": 12, "roi_pct": 18.13, "win_rate_pct": 58.3},
    {"edge_bucket": "8%+", "n": 2, "roi_pct": -62.91, "win_rate_pct": 0.0}
  ],
  "fees_and_fills": {"n": 40, "maker_pct": 22.5, "avg_fee_paid": 0.0455}
}
```

No `TRIGGER_*.md` file dated after 2026-08-09 exists — the only trigger file present
(`TRIGGER_2026-08-09.md`) was already reviewed in `2026-08-09-weekly-review.md`. This run is
a plain weekly pass. `--check-thresholds`' own math would agree: only 1 new settled trade
since the last snapshot (39→40), well under the 8-trade threshold.

## Comparison to last_snapshot.json (n=39, 2026-08-09)

The one new settled trade is fully accounted for and doesn't change the picture: it landed
in MLB / h2h / the 1.5-3% edge bucket, and it was a **win** (every affected win-rate ticked up:
overall 41.0%→42.5%, MLB 31.6%→35.0%, h2h 16.7%→23.1%; every affected ROI got less negative:
overall -22.12%→-19.08%, MLB -38.41%→-33.09%, h2h -79.32%→-65.88%, 1.5-3% bucket
-23.88%→-18.19%). Directionally this softens the H2H/MLB/low-edge-bucket underperformance
very slightly but does not come close to reversing any of it at these sample sizes — still
firmly "confirmation, not new signal."

The more notable change is **n_open: 8 → 16** (doubled). That's not a performance number
(no pnl yet) but is consistent with what the `book_probability_log` review below surfaces:
a burst of `execution_status='submitted'` fills recently. Not treated as an anomaly on its
own — just noted as context for why open-position count moved more than settled count this
week.

**Max drawdown is now 14.9% (20 snapshots)**, up from 13.48% (17 snapshots) last run and now
right at the 15% `DRAWDOWN_ALERT_FRACTION` threshold — one bad snapshot away from
independently triggering a threshold check. Flagging for next run's attention, not treated as
a finding on its own (n_snapshots=20 is a short bankroll history, and this is a drawdown
metric, not a hypothesis-shaped one).

## `book_probability_log` review (new this run — first review of the widened table)

Direct query via `storage.db.get_connection()` (not via `metrics.py`, which doesn't touch
this table):

- **4,392 total rows**, of which **238 have the widened columns populated**
  (`status IS NOT NULL`) — slightly more than the 226 cited when this task was scoped,
  consistent with at least one more scan having run since. The remaining 4,154 rows predate
  the migration and only carry the original book-calibration columns (`consensus_prob`,
  `bookmaker_count`, `actual_outcome`, etc.) — not reviewed here since that's the table's
  pre-existing, already-understood purpose.
- **Important caveat that shapes everything below**: all 238 widened rows share the exact
  same `scan_id` (`2026-08-12T04:48:52.434025`) and the exact same `scanned_at` timestamp
  (`2026-08-12T05:04:01.031187`, to the microsecond). This table has only been populated by
  **one scan cycle** so far — every number below is a single point-in-time snapshot across
  whatever games were in flight at that moment, not an accumulated history. Treat as "here's
  what one scan's candidate population looks like," not as a trend.

**Status breakdown (n=238, one scan):**

| status | n | % |
|---|---|---|
| no_edge | 188 | 79.0% |
| execution_failed | 21 | 8.8% |
| spread_too_wide | 8 | 3.4% |
| kelly_no_edge | 7 | 2.9% |
| value | 6 | 2.5% |
| few_books | 4 | 1.7% |
| blocked | 4 | 1.7% |

Of the 6 `value` (bet placed) candidates: edges ranged 1.02%–3.57%; 5 of 6 were
`maker_only=1`. `kelly_no_edge` (n=7): every one was a maker_only candidate with edge
1.2–1.76% whose Kelly-recommended stake ($0.33–$0.48) fell just under the $0.50
`MIN_BET_DOLLARS` floor — a real, sharp cutoff, not a calibration issue. `blocked` (n=4): all
four were "already have an open position on this game" (duplicate-ticker guard), all MLS.
`few_books`/`spread_too_wide` (n=12 combined): mechanical quality-filter rejections, mostly
MLS h2h markets at a 6¢ spread vs the 5¢ cap.

**Edge distribution among `no_edge` candidates near the cutoff** (n=188, one scan;
`reason` string parsed to recover each row's own effective per-market minimum, since
quality-tier premiums make the effective bar vary market-to-market rather than being a flat
`MIN_EDGE`): the closest any rejected candidate got to clearing its own bar was **1.69
percentage points** away (edge 0.34% vs a 2.03% effective minimum). Zero of the 188 were
within 1.5pp of the cutoff; 24 were within 2–2.5pp; the remaining **158/188 (84%) were more
than 2.5pp below their effective minimum** — most with near-zero or even negative computed
edge. **This scan's data does not show a population of candidates "just missing" the edge
bar** — the rejected population looks like it mostly has no real edge at all, not marginal
edge. Interesting and directly relevant to the "would loosening MIN_EDGE unlock more volume"
question this table was built to eventually answer, but this is one scan (n=188 candidates,
highly correlated — same handful of games) — **not treated as a hypothesis file** this run;
worth re-checking once several independent scans have accumulated.

**The one genuinely new, robust thing this review surfaced** (cross-validated by, but not
solely based on, `book_probability_log`): all 21 `execution_failed` rows in this scan were
`maker_only=1`, with `reason` either `"GTC mid unfilled after 900s"` or `"GTC mid HTTP 429"`.
Pulling the equivalent field from `positions.failure_reason`/`execution_status` (which
*does* have full ~3.5-week history, not just one scan) confirms this is a persistent,
large-sample pattern: **193 of 249 live order attempts ever made (77.5%) failed to fill**,
and 73.1% of those failures (141/193) are specifically "rested at mid, never crossed to ask"
cases. This population (193 failed attempts) is more than 4x the size of the entire settled
sample this project has been analyzing (n=40) and is completely invisible to
`summary_report()`, since `load_positions()` filters `execution_status != 'failed'` before
computing anything. Two hypotheses written from this (see below).

**position_id linkage**: 0 of 238 widened rows have `position_id` set yet — the
link-back-to-real-positions join this table was designed to eventually support isn't usable
today. Noting for whoever picks up the fill-rate-selection hypothesis below.

## New hypotheses this run

- `research/hypotheses/2026-08-12-execution-failure-no-ask-fallback.md` — 193/249 (77.5%)
  of all live order attempts across the full history fail to fill, and 73.1% of those
  failures are specifically "no ask-crossing fallback" cases rather than rate-limits or lack
  of edge — meaning the realized 56-order sample is a heavily execution-filtered subset of a
  much larger population that cleared the edge bar. Genuinely new: this data isn't reachable
  from `metrics.py` at all.
- `research/hypotheses/2026-08-12-totals-fill-rate-selection-bias.md` — `totals` has both
  the best realized ROI (+6.82%, n=25) and the highest attempt-failure rate (82.4% of 159
  attempts) of any bet type, raising the question of whether the realized totals sample is
  favorably selected by fill mechanics rather than purely reflecting better-calibrated edge.
  Companion to the hypothesis above; doesn't contradict the existing totals > h2h finding,
  questions the mechanism behind it.

Everything else this run (H2H vs totals, MLB vs MLS, the 1.5-3% edge bucket, maker/taker
win-rate mismatch) is confirmation of already-known findings with one more trade of data —
no new hypothesis files for those; see comparison section above.

## Anomalies / flags

1. Max drawdown 14.9% (n=20 snapshots) is right at the 15% alert threshold — didn't cross it
   this run, but worth watching; a single bad settlement could trigger the daily check before
   next week's scheduled review.
2. `n_open` doubled (8→16) with no accompanying pnl signal yet — not a finding, just context
   that ties into the execution-failure data above (a batch of orders recently did fill).
3. `book_probability_log`'s widened columns have exactly one scan's worth of data
   (238 rows, one `scan_id`) as of this review — the table is brand new in its current form;
   next run should have meaningfully more scans to work with, at which point the
   "edge distribution near cutoff" question above is worth revisiting properly instead of as
   a single-snapshot observation.

## Bottom line

Settled-trade numbers moved by exactly one trade this week (39→40) and confirm, rather than
change, everything already on record — no new hypothesis from that side alone. The real
signal this run came from querying `positions`/`book_probability_log` directly rather than
through `metrics.py`: a large (n=193, full-history) and previously-invisible population of
failed order attempts, dominated by a structural "no ask fallback" pattern, sized more than
4x the entire settled-trade sample this project has been analyzing. Two hypotheses written
from that. The widened `book_probability_log` table itself is too new (one scan, zero
`position_id` links) to draw conclusions from in isolation yet, but its first snapshot is
consistent with, and helped surface, the execution-failure pattern found in `positions`.
