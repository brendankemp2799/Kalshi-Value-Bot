---
id: 2026-08-12-live-lead-change-momentum
date: 2026-08-12
status: confirmed     # open | investigating | confirmed | rejected
source: manual        # user-initiated feature scoping, not performance-analyst
n_at_time: 40         # settled live trades in the 2026-08-12 weekly review (pre-game strategy)
experiment_ref: research/experiments/2026-08-12-mls-lead-change-momentum.md
---

> **RESOLVED 2026-08-12 — CONFIRMED, LATENCY-GATED.** n=40 lead-change goals, median
> total move +25¢. Net residual is **+10 to +12¢ at 1–5s** of lateness (82–92% of
> events profitable), decaying to **+3¢ at 30s** and crossing zero between 60s and 120s.
> The edge is real; capturing it is a latency problem.
>
> ESPN's REST feed lags the market's first tick by a median **31s** (40/40 events),
> landing at the thin end of the curve. A faster push/websocket feed is worth roughly
> 3–4× the per-contract edge.
>
> **An earlier pass of this experiment wrongly concluded "rejected"** — it priced entry
> off 1-minute candle asks, adding ~60s of unmodelled delay to an already-31s-late
> timestamp. See the Correction section of the experiment record. Two large unknowns
> remain before this is buildable: post-goal **book depth** (unmeasured, and worst at
> exactly the low-latency end where the edge looks best) and **round-trip exit cost**
> (`settled` is a +300s mark, not a realized exit).

## Observation

This hypothesis is **not** derived from the bot's own trading data — it proposes a new
strategy in a market segment the bot has never traded. The bot is pre-game only:
`data/odds_fetcher.py:180-191` discards every event whose `commence_time <= now`, so
in-play games have never entered the pipeline and there is no in-house prior on them.

What is known from `findings/2026-08-12-weekly-review.md` (n=40 settled), and what
motivates scoping this carefully rather than optimistically:

- Overall ROI −19.08%, win rate 42.5%, Sharpe −0.215, max drawdown 14.9%.
- **MLS +5.43% vs MLB −33.09%** — MLS is the only sport with positive realized ROI,
  which is why v1 is scoped to MLS only.
- **Totals +6.82% (n=25) vs h2h −65.88% (n=13)** — h2h is where this bot loses money.
  The rules below are h2h/3-way moneyline rules, so they are being proposed *against*
  the grain of the only segment evidence available. This is a deliberate, user-chosen
  scope, and it raises the burden of proof rather than lowering it.
- **193 of 249 live order attempts (77.5%) never filled**, 73.1% of those "rested at
  mid, never crossed to ask" — the existing maker-first execution ladder is the bot's
  single largest failure mode, and it is structurally the worst possible fit for
  reacting to a scoring event.

Prior groundwork exists but produced no recorded result: `kalshi_reaction_latency.py`
was written to answer exactly this question and was run (MLB 2026-08-11, MLS
2026-08-12), but it only prints to stdout and both caches were gitignored and
subsequently iCloud-evicted to `.icloud` stubs. No numbers survived.

## Hypothesis

> When an MLS goal changes which team is leading, the Kalshi 3-way moneyline market
> reprices slowly enough that a bot learning of the goal from ESPN's public API can
> still buy the newly-favored outcome at a price that is profitable after taker fees.

Falsifiable, and decomposes into three independently measurable gates. **All three must
hold** — any one failing kills the strategy:

1. **Publication lag (Q1).** ESPN's site API must expose a goal quickly enough to act on.
2. **Reaction latency (Q2).** Kalshi's price must not have finished repricing by then.
3. **Liquidity (Q3).** There must be takeable size at a spread that leaves the move ahead.

Formally: edge exists only if
`publication_lag + execution_time < time_to_50pct_of_eventual_move`, **and** fillable
size exists at the decision-time ask.

Gate 1 is the one most likely to kill this, and the one no prior work has measured.
`kalshi_reaction_latency.py` measures from ESPN's `wallclock` field — *when the goal
physically happened* — which a live bot can never observe. The gap between that and
*when ESPN publishes* is unmeasurable from historical data and requires a live probe.

### Rules under test

Classified off the pre-event Kalshi baseline price:

| Rule | Trigger | Market bought |
|---|---|---|
| A1 | Scoring team was tied, takes the lead, baseline > 0.50 (favorite) | scorer's own YES |
| A2 | Same, baseline < 0.50 (underdog) | scorer's own YES |
| A3 | Trailing team equalizes (score level after goal) | TIE market YES |
| A4 | Any team leads with ≤15 min left, price below a ceiling | leader's YES |

## Suggested experiment

**Phase 0 — measure the three gates.** Zero Odds API credits; MLB StatsAPI, ESPN's site
API and Kalshi markets/trades/candlesticks are all free and unmetered.

- `espn_publication_lag_probe.py` (new): poll live MLS matches ~every 5s, record
  wall-clock first-observation time per goal against the goal's own `wallclock`.
  Distribution of `observed_at - wallclock` answers Q1.
- `kalshi_reaction_latency.py` (fix + extend): answers Q2 per rule class, and Q3 once
  decision-time bid/ask is captured.

**Phase 1 — P&L, not price movement.** `live_backtest.py` (new), replaying recorded
games through the *real* pure `evaluate_lead_change_trigger()` that any shipped engine
would use — same discipline as `mm_backtest.py:12-21`. Adds the three things price
movement alone cannot tell you: settlement outcome, realistic entry (decision-time
**ask** plus `config.KALSHI_TAKER_FEE_RATE_ESTIMATE`, not trade price), and realistic
decision time (`event_ts + measured publication lag + assumed execution latency`).

### Pass bar — set now, before any data is collected

A rule advances to implementation only if **both** hold:

- **n ≥ 40** qualifying events for that rule.
- **Positive ROI after fees at the p90 latency assumption**, not the median.

The p90 requirement is the important half. If a rule is only profitable at median
latency and flips negative when 15s is added, it is measuring the absence of bad luck,
not an edge. Rules that miss the bar get written up in `failed_ideas/` and are not built.

## Known caveats going in

- **Confirmation-bias risk is high.** This feature was scoped before any measurement,
  and A-family was chosen over the latency-insensitive alternatives (clock-decay totals,
  in-game Poisson divergence) that were also on the table. The pre-registered bar above
  exists specifically so the result is not fitted after the fact.
- **The segment evidence points the other way.** These are h2h rules, and h2h is the
  bot's worst segment (−65.88%, n=13). Small n, but not encouraging.
- **A1/A2 split for free, A3 is not.** Splitting on baseline needs no extra fetching.
  A3 requires the TIE market, which `kalshi_reaction_latency.py:337` does not currently
  request (`teams_needed` only ever contains scoring teams).
- **Survivorship in the market-matching step.** Events whose Kalshi market can't be
  fuzzy-matched are dropped. If match failure correlates with thin/illiquid games, the
  measured sample is biased toward exactly the games most likely to be tradeable — which
  would flatter every number. Track the no-match count as a first-class output, not a
  footnote.
- **MLS in-game liquidity is entirely unknown.** The pre-game filters
  (`MIN_KALSHI_VOLUME = 500`, `MAX_KALSHI_SPREAD = 0.05`) may exclude essentially every
  in-play MLS market. If so, Q3 fails and nothing else matters.
- **The probe measures ESPN, not the fastest available feed.** A negative Q1 result rules
  out *this* data source, not the idea — but faster feeds are generally paid, which
  conflicts with the zero-additional-cost constraint.
- **Two bugs in the prior tooling** mean any recollection of the earlier runs' output is
  unreliable and must not be used as a prior: the persistence check at
  `kalshi_reaction_latency.py:185-194` is an all-or-nothing gate rather than the
  per-trade filter its comment describes (forcing `NO_REACTION`), and `time_to_50pct_s`
  at `:196-197` is computed only for upward moves.
