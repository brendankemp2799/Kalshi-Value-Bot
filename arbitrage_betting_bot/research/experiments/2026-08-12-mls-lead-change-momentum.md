---
experiment_id: 2026-08-12-mls-lead-change-momentum
date: 2026-08-12
hypothesis_ref: research/hypotheses/2026-08-12-live-lead-change-momentum.md
status: passed        # passed conditionally -- edge exists, but is latency-gated. See Conclusion.
baseline: n/a -- proposed NEW strategy, no existing live/in-game baseline to beat
dataset: "ESPN MLS goals + Kalshi KXMLSGAME trades/candlesticks. Run over 17 match dates 2026-05-02 .. 2026-08-08, but the May dates yielded NO usable rows -- Kalshi's MLS market history only begins ~2026-07-16 (see the 2026-08-13 correction below). Effective sample: 7 dates, 2026-07-16 .. 2026-08-08."
training_period: n/a -- single-pass measurement, no parameters fitted
validation_period: n/a
out_of_sample_period: n/a
n_trades: 40
roi: null             # measured per-contract in cents; ROI depends on entry price and exit policy
pnl: null             # no capital deployed
sharpe: null
max_drawdown: null
win_rate: 0.82        # share of events with positive residual at 5s lateness
fees_included: true
slippage_assumptions: "1c crossing cost (in-match spreads measured at 1-2c). Depth NOT modelled -- entry assumes the clip fills at top-of-book immediately post-goal, when the book is thinnest."
execution_assumptions: "Lateness d measured from the market's own first tick (fastest existing participant), NOT from the on-field event. Exit modelled as a mark at +300s, not a realized round-trip."
supersedes_conclusion_of: "first pass in this same file (2026-08-12), which used 1-min candle asks and wrongly concluded 'rejected' -- see Correction"
---

## Hypothesis under test

> When an MLS goal changes which team is leading, the Kalshi 3-way moneyline market
> reprices slowly enough that a bot learning of the goal quickly can still buy the
> newly-favored outcome at a price that is profitable after taker fees.

Gated on **Q1** publication lag, **Q2** reaction latency, **Q3** in-match liquidity.

## Correction — the first pass of this experiment reached the wrong conclusion

The initial run concluded **rejected**, on the basis that the residual was negative even
at a nominally zero-latency entry. That was a **methodological error**, not a data
problem, and it is worth recording precisely because the mistake is easy to repeat:

- Entry was priced off `_ask_at()`, which returns the ask close of the first **1-minute
  candle** ending at or after the target time — silently adding up to ~60s of delay.
- ESPN's stamp is itself a median **31s** behind the market's first tick.
- The residual decays steeply across exactly that 30–120s range.

So the first pass priced entry roughly **90s late** while attributing the result to a
"zero-latency" entry. Candlestick granularity is not adequate for a question whose
answer moves on a sub-second-to-one-minute scale. The corrected measurement
(`research/repricing_decay_curve.py`) uses the raw trade tape.

The correction was prompted by the challenge: *"you are comparing the ESPN public API to
live data — wouldn't a websocket be much faster?"* That is exactly right, and the first
pass had conflated "ESPN publishes late" with "the market moved before the true event",
which imply opposite answers.

## Method

Two scripts, both free data only (ESPN site API + Kalshi markets/trades/candlesticks,
never the Odds API):

1. `research/mls_lead_change_residual.py` — identifies every goal that took the scoring
   team from level to ahead (the A1/A2 trigger), matches the scoring team's own Kalshi
   market, and records baseline / settled prices. Rows: `research/findings/mls_lead_change_residual.json`.
2. `research/repricing_decay_curve.py` — the corrected timing measurement. Defines
   `t0` = first trade reaching 20% of the eventual move = **the fastest existing
   participant's reaction**, then measures net residual at a grid of lateness values `d`
   after `t0`. Rows: `research/findings/mls_repricing_decay.json`.

`d` is therefore *lateness versus the fastest bot already trading this market*, not
versus the on-field event. Entry = trade price at `t0+d` + 1c crossing cost, net of
`KALSHI_TAKER_FEE_RATE_ESTIMATE * p * (1-p)`.

## Results

**Q3 — liquidity: PASSES.** In-match KXMLSGAME spreads are **1–2¢** with continuous flow
(547 trades in one 15-minute window on `KXMLSGAME-26AUG01MTLNE-NE`). Markets days out
from kickoff show 28–49¢ spreads and zero liquidity — so in-match tradeability is
invisible from pre-game data, and `MAX_KALSHI_SPREAD = 0.05` would reject those.

**Q1 — ESPN is genuinely slow.** ESPN's goal `wallclock` lags the market's first tick by
a median **+31.2s** (p25 +29.2s, p75 +148.2s), and the market moved first in **40/40**
events. ESPN never leads.

**Q2 — the repricing has two phases.** From the first tick: 20%→50% of the move in a
median **0.9s** (21/40 within 1s), but 50%→90% takes a median **54.8s**. A fast burst,
then a slower grind.

**The decay curve** (n=40, goals with ≥5c move):

| lateness vs fastest participant | median residual | mean | profitable | A1 | A2 |
|---|---|---|---|---|---|
| 0.0s | +17.37¢ | +23.12¢ | 38/40 (95%) | +15.31 | +17.99 |
| 0.5s | +13.80¢ | +18.77¢ | 38/40 (95%) | +13.81 | +13.78 |
| 1.0s | +11.76¢ | +17.51¢ | 37/40 (92%) | +10.40 | +12.75 |
| 2.0s | +9.91¢ | +15.97¢ | 33/40 (82%) | +10.41 | +9.91 |
| 5.0s | +10.82¢ | +14.44¢ | 33/40 (82%) | +12.36 | +9.76 |
| 10.0s | +7.44¢ | +11.92¢ | 31/40 (78%) | +10.89 | +7.28 |
| 20.0s | +3.31¢ | +9.39¢ | 29/40 (72%) | +14.32 | +1.34 |
| 30.0s | +3.30¢ | +9.07¢ | 26/40 (65%) | +8.96 | +2.21 |
| 60.0s | +1.88¢ | +8.74¢ | 23/40 (57%) | +5.55 | +0.29 |
| 120.0s | −1.20¢ | +5.33¢ | 15/40 (38%) | +0.22 | −1.35 |

**Break-even sits between 60s and 120s.** Median total move available: +25¢.

## Conclusion

**Confirmed, conditionally — the edge is real and latency-gated, not absent.**

The pre-registered bar (n ≥ 40, positive ROI after fees at the p90 latency assumption)
is met for any end-to-end latency materially under ~60s.

Where the plausible data sources land:

- **ESPN REST polling** (what was built): ~31s stamp lag + poll interval + execution
  ≈ 35–40s → median **+3¢**, ~65% hit rate. Positive but thin, and thin enough that the
  unmodelled costs below could erase it.
- **A push/websocket feed at 1–5s**: median **+10 to +12¢**, ~82–92% hit rate. A
  materially different proposition.

So the answer to "would a faster feed help?" is **yes, substantially** — roughly 3–4×
the per-contract edge moving from ~35s to ~5s.

### What must not be glossed over

- **`settled` is a mark, not a realized exit.** It is the price at +300s. Realizing it
  costs a second crossing plus a second fee (~2–3¢ round trip), or you hold to
  resolution and eat the variance. Subtract that before believing any row above.
- **Depth is not modelled.** Entry assumes the full clip fills at top-of-book in the
  seconds after a goal — precisely when the book is thinnest and everyone is hitting it.
  On real size, slippage could be several cents. This is the single largest unknown, and
  it bites hardest at exactly the low-latency end where the edge looks best.
- **`d=0` is not "instant."** It means *tying the fastest bot already in this market*.
  Sub-5s requires beating participants watching low-latency video.
- **Unit economics at current bankroll.** ~2.4 lead-change goals per match date. At
  +10¢/contract on a $25 clip that is roughly $2.50/goal ≈ $6/match date. An
  enterprise sports feed (Sportradar/Genius, MLS's official data partners) is priced far
  above what that supports. The edge must be captured with a cheap or free feed to be
  worth anything at a ~$184 bankroll.

## Skeptic review

Self-review; not run through the formal Skeptic Agent.

- **Look-ahead bias** — `settled` is +300s, strictly after every entry. `t0` is derived
  from the eventual move, which *is* look-ahead in the event-selection sense: a live bot
  cannot know at t0 that a 25¢ move is coming. This does **not** bias the decay curve
  (which is conditional on a goal having occurred, a fact the bot learns from its feed),
  but it does mean the "20% of eventual move" definition of t0 is a post-hoc anchor. A
  live implementation would need a causal trigger definition.
- **Survivorship** — `no_kalshi_match: 0`, `no_price_data: 0` across the full run. Every
  qualifying goal matched. No liquidity-based filtering of the sample.
- **Fee assumptions** — uses the estimate, not the authoritative per-order fee. Small
  relative to a +10¢ signal, but the estimate should be replaced before sizing.
- **Sample** — n=40 meets the bar, but spans one partial season (May, Jul–Aug 2026) and
  one league. Not validated across seasons or leagues.
- **Multiple testing** — one hypothesis, one pre-registered bar. The `d` grid varies an
  *assumption*, not a fitted parameter.
- **The first pass's error** — documented above under Correction. The lesson generalizes:
  never measure a sub-minute timing question with 1-minute bars.

## Follow-up 1 (RESOLVED 2026-08-13): depth, and why a faster feed is not worth buying

`research/post_goal_depth.py` measures fillable size after a goal — contracts printing
at or below our entry price. Kalshi exposes no historical orderbook, so realized trades
are used as a *lower bound*: every trade that printed is size someone actually got.

| entry lateness | median | p25 | ≥$25 | ≥$100 | ≥$500 |
|---|---|---|---|---|---|
| 1s | $212 | $13 | 28/40 | 22/40 | 11/40 |
| 5s | $339 | $36 | 32/40 | 28/40 | 19/40 |
| 30s | $934 | $348 | 38/40 | 35/40 | 27/40 |

At 1s, 12 of 40 goals had essentially nothing fillable (five at $0).

**Depth grows faster than the edge decays**, which inverts the case for a fast feed.
Combining edge × fillable size, net of a ~2.5¢ round-trip exit:

| clip | d=5s $/month | d=30s $/month |
|---|---|---|
| $10 | $15 | $13 |
| $25 | $38 | $34 |
| $50 | $62 | **$67** |
| $100 | $102 | **$135** |

Being 25 seconds faster is worth ~nothing, and above ~$50 clips it is actively *worse* —
the fast window is too thin to deploy size into. **The binding constraint is capacity,
not latency.**

Bankroll was $162.80 with `MAX_PCT_BANKROLL = 0.05`, i.e. a **$8.14** maximum bet — which
puts this strategy at roughly **$13–15/month**. Mid-tier push feeds run $30–200/mo (and
are typically 5–20s, not sub-second); genuinely low-latency feeds (Sportradar, Genius —
MLS's official data partners) are enterprise-priced and often require operator licensing.

**Decision: do not buy a sports feed.** The strategy is bankroll-constrained, not
latency-constrained.

## Follow-up 2 (RESOLVED 2026-08-13): infrastructure is not the bottleneck either

Measured on the production droplet (DigitalOcean 1vCPU/1GB, NYC1):

- **ICMP ping to Kalshi: 3.2ms.** Effectively co-located. No hosting change would help.
- **A real latency bug was found and fixed:** every Kalshi call used bare
  `requests.get()`/`.post()` with no `Session`, paying a fresh TLS handshake each time.
  Measured on the droplet: **118.6ms → 7.3ms median, 111ms saved per call (94%)**. Fixed
  in `data/kalshi_auth.py::session()` (thread-local pool) across all 14 Kalshi call sites.
- **Memory is the one real constraint:** 168MB available of 957MB, no swap, with the bot
  already at ~460MB across two processes. Any added process needs swap or a 2GB upgrade.
- **The real blocker is architectural, not hardware:** `run_scan()` blocks on its
  `ThreadPoolExecutor` until every GTC order resolves — up to
  `LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS` = 900s — so a live engine sharing that loop would
  be starved for 15 minutes at a time.

## Follow-up 3 (RESOLVED 2026-08-13): the tape-only variant does not work

Tested and rejected — see `research/experiments/2026-08-13-tape-only-burst.md`.
Detecting the burst from Kalshi's own tape (no sports feed) fails: **0 of 34 detector
configurations were profitable** on the mark-to-market. A tape detector fires *because*
the price already moved, so it structurally buys after the move.

## Follow-ups still open

1. **True orderbook depth.** The depth figure above is a lower bound from realized
   trades. Kalshi has no orderbook endpoint in this repo (`grep orderbook` → zero hits);
   adding one would measure resting size directly rather than inferring it.
2. **Round-trip exit modelling.** The decay curve still uses a +300s mark. A realistic
   exit (second crossing + second fee) should be folded in before any sizing decision.

## Correction 2 (2026-08-13): the stated sample span was wrong

This record originally claimed a dataset spanning "17 completed match dates
2026-05-02 .. 2026-08-08". **Kalshi's MLS market history only begins ~2026-07-16** —
`fetch_settled_markets_in_window` returns zero settled markets for 2026-03-07,
2026-04-11, 2026-05-09 and 2026-06-13. The May dates therefore contributed no usable
rows, and the effective sample was 7 dates, 2026-07-16 to 2026-08-08.

The n=41 result is unaffected (every measured goal came from the July/August dates, and
`no_kalshi_match: 0` is consistent with that — May goals were filtered by the
lead-change gate before ever reaching the market lookup). Only the claimed breadth was
wrong, and it made the sample look more regime-diverse than it is.

Discovered while establishing the data ceiling for
`research/experiments/2026-08-13-mls-latency-insensitive.md`. The entire usable Kalshi
MLS universe is **51 matches across 7 dates**, which caps every MLS in-game backtest and
makes the pre-registered "n ≥ 40" bar unreachable for most rules.

## Where this leaves the strategy

The edge is real but sits behind three constraints that all point the same way:

- It cannot be captured faster (capacity, not latency, is binding — Follow-up 1).
- It cannot be captured without a sports feed (Follow-up 3).
- It cannot pay for a sports feed at the current bankroll (Follow-up 1).

So the honest position is **shelved, not refuted**: revisit if the bankroll grows by
roughly an order of magnitude, at which point $50–100 clips become permissible and the
$67–135/month range starts to justify a feed subscription. Nothing about the measurement
needs redoing at that point — only the sizing assumptions change.
