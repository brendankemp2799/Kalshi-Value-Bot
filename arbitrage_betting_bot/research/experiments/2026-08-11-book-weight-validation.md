---
experiment_id: 2026-08-11-book-weight-validation
date: 2026-08-11
hypothesis_ref: ""
status: inconclusive
baseline: core/odds_converter.py's static BOOK_WEIGHTS priors (Pinnacle=1.0 down to Fliff=0.25)
dataset: "positions table, is_paper=0, status='closed', pnl IS NOT NULL, bookmakers_json IS NOT NULL"
training_period: ""
validation_period: ""
out_of_sample_period: ""
n_trades: 39
roi: null
pnl: null
sharpe: null
max_drawdown: null
win_rate: null
fees_included: false
slippage_assumptions: "n/a -- this scores each book's pre-game de-vigged probability against the Kalshi-resolved actual outcome, not a trading simulation"
execution_assumptions: "reuses core/odds_converter.py::consensus_stats() directly (called with a single-book list) rather than a reimplementation of the de-vig/matching logic, so this can't drift out of sync with what the live bot actually computes"
---

## Hypothesis under test

`core/odds_converter.py::BOOK_WEIGHTS` assigns each sportsbook a fixed sharpness
weight (Pinnacle=1.0, exchanges 0.85-0.90, major US retail 0.65-0.70, soft
offshore books down to 0.25) based on general industry reputation, never checked
against this bot's own data. Prompted by a user question during a live review of
the fair-value model ("is this the best possible method?"). If the priors are
right, higher-weighted books should show better empirical calibration (lower
Brier score) against real, Kalshi-resolved outcomes than lower-weighted books,
and the full weighted consensus should be competitive with or better than the
best individual book.

## Method

For each of the 39 settled, non-paper positions with a captured `bookmakers_json`
snapshot: the position's own `side`/`team_name` IS the specific outcome that was
priced, and `pnl >= 0` IS whether that outcome actually happened — Kalshi resolves
markets against real game results (`execution/auto_settle.py`), so this is ground
truth, not a proxy. For every book present in that position's snapshot, computed
that book's own de-vigged probability for our specific side by calling
`consensus_stats([single_book], outcome_name, market_key=..., point=...)` — same
function the live bot uses for the full weighted consensus, just fed one book at
a time so weighting is a no-op and the result is that book's own number. Scored
each book-position pair via Brier score: `(book_prob - actual)^2`, lower is
better-calibrated. Averaged per book across every position it appeared in, and
did the same for the full weighted consensus for comparison.

`outcome_name` had to be derived carefully: `positions.team_name` stores a
display label ("Over 3.5", "Team +1.5"), but `consensus_stats()` expects the bare
outcome name with `point` passed separately, mirroring exactly what
`core/value_detector.py`'s `_detect_totals`/`_detect_spread` pass at scan time
(`direction_label`/`covering_team`, not the combined label). Passing the combined
label directly (first attempt) turned out to be harmless in practice —
`_names_match()`'s substring-containment fallback already tolerates the extra
suffix — verified by hand on position #149 (Pinnacle's own totals line that day
was 3.25, not our traded 3.5 — correctly returns "no data" either way, gated by
the point filter, not the name filter) and re-running the full analysis after
the fix produced byte-identical output to before it. Worth having checked rather
than assumed, but not a finding in itself.

Script: `/tmp/book_weight_validation.py` on the droplet (not committed — one-off
analysis, reusable via this experiment file if rerun later).

## Results

n=39 positions, 0 skipped (bad JSON). 31 distinct books appeared at least 5 times.

**Full weighted consensus Brier: 0.2209 (n=39).**

Per-book (n=5+ appearances only, sorted best/lowest Brier first):

| book | weight | n | mean Brier |
|---|---|---|---|
| leovegas_se | 0.45 | 10 | 0.1262 |
| winamax_de | 0.65 | 10 | 0.1274 |
| betclic_fr | 0.50 | 10 | 0.1287 |
| unibet_fr | 0.50 | 10 | 0.1291 |
| marathonbet | 0.75 | 10 | 0.1312 |
| winamax_fr | 0.65 | 9 | 0.1383 |
| betus | 0.70 | 10 | 0.1385 |
| **pinnacle** | **1.00** | 8 | 0.1436 |
| coolbet | 0.60 | 14 | 0.1501 |
| williamhill | 0.55 | 14 | 0.1536 |
| betsson | 0.55 | 11 | 0.1602 |
| nordicbet | 0.50 | 12 | 0.1664 |
| betonlineag | 0.80 | 14 | 0.1668 |
| lowvig | 0.90 | 14 | 0.1669 |
| sport888 | 0.50 | 17 | 0.1724 |
| fanatics | 0.50 | 14 | 0.1826 |
| bovada | 0.40 | 9 | 0.1847 |
| unibet_nl / unibet_se | 0.50 | 16 | 0.1873 |
| fanduel | 0.70 | 21 | 0.1889 |
| williamhill_us | 0.55 | 7 | 0.1930 |
| draftkings | 0.70 | 7 | 0.1933 |
| pmu_fr | 0.50 | 19 | 0.1959 |
| gtbets | 0.35 | 16 | 0.1981 |
| betrivers | 0.50 | 23 | 0.2078 |
| onexbet | 0.30 | 24 | 0.2088 |
| mybookieag | 0.30 | 22 | 0.2123 |
| betmgm | 0.55 | 22 | 0.2169 |
| tipico_de | 0.50 | 16 | 0.2393 |
| **matchbook (exchange)** | **0.85** | 12 | 0.2490 |
| **betfair_ex_eu (exchange)** | **0.90** | 10 | 0.2591 |

Two notable patterns:

1. **No clean monotonic relationship between current weight and empirical Brier
   score.** Pinnacle (1.0) scores respectably (0.1436, better than the
   consensus and most books) but isn't the single best. Several 0.50-0.65
   weighted books (leovegas_se, winamax_de, betclic_fr, unibet_fr) outscore
   every book weighted 0.80+.
2. **The two betting exchanges — the second- and third-highest weights in the
   whole table (0.90, 0.85) — scored the worst of all 31 books** (0.2591,
   0.2490), well behind even the lowest-weighted soft books like onexbet (0.30,
   Brier 0.2088).
3. **The full weighted consensus (0.2209) underperformed 27 of the 31 individual
   books.** Only tipico_de, matchbook, and betfair_ex_eu scored worse than the
   consensus.

## Conclusion

**Inconclusive — real, surprising signal, but not enough to act on.** This
sample does not clearly validate the current `BOOK_WEIGHTS` priors, and the
exchange result in particular runs directly counter to the assumption that
Betfair/Matchbook are top-tier sharp sources. But per-book n is 7-24 and the
consensus's n=39 isn't even comparable in sample size to any single book's
subset — nowhere near enough to justify editing `BOOK_WEIGHTS` from this alone.
What this does justify: building Phase 2 (a persistent, non-pruned log of
every scanned candidate's per-book probabilities + eventual outcome, not just
the 39 we happened to bet on) to get a larger, less selection-biased sample
before treating either the exchange result or the consensus-underperformance
result as real. Revisit once that log has accumulated substantially more than
39 events — ideally several hundred, and ideally not all selected for already
having edge.

## Skeptic review

Not run through the formal Skeptic Agent — self-flagged given this is a
read-only analysis with no production change riding on it yet.

- **Severe selection bias, not just small n.** All 39 positions are markets the
  model already decided had enough edge to bet — i.e., markets where Kalshi and
  our consensus already disagreed enough to clear a ~3% fee-adjusted bar. This is
  not a random sample of games; it's conditioned on disagreement existing in the
  first place, which could distort book comparisons in ways that don't apply to
  the broader market. Phase 2 exists specifically to fix this.
- **Per-book n varies (7-24) and reflects opportunity, not just quality.** A
  book's sample size here depends on how often it happened to quote the exact
  line we traded (see the point-match discussion in Method) — a book that offers
  more alternate lines gets more chances to be scored, which is a confound
  separate from its actual accuracy.
- **Consensus vs. individual-book Brier isn't a fair comparison as computed.**
  The consensus is evaluated on all 39 positions; each individual book only on
  the subset where it happened to have a matching line. A book that's only
  scored on its "easy" or more-liquid markets could look better than a
  consensus that's forced to average in every position, including ones where
  most books had thin or stale coverage.
- **No look-ahead bias**: every book's probability is exactly what it was
  quoting into the position's own captured `bookmakers_json` at entry time —
  nothing here uses hindsight.
- **Do not cite the exchange result as "exchanges are unreliable" beyond this
  specific sample.** n=10-12 per exchange, in a selection-biased sample, is
  genuinely not enough to overturn a well-founded general prior (exchanges
  reflecting real matched money is a sound theoretical reason to weight them
  high) — this is a flag to investigate further, not a refutation.
