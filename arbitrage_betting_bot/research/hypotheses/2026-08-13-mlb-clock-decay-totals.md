---
id: 2026-08-13-mlb-clock-decay-totals
date: 2026-08-13
status: rejected      # open | investigating | confirmed | rejected
source: manual
n_at_time: 40
preregistered: true   # written BEFORE any MLB result was computed
experiment_ref: research/experiments/2026-08-13-mlb-clock-decay-totals.md
---

> **RESOLVED 2026-08-13 — REJECTED. 0 of 6 rules passed the bar.**
> Clock decay is flat at adequate sample (CD-UNDER@6/@7 return +0.3%/+0.4% on
> n=270/252). The Poisson arms look profitable but fail n>=200 and Bonferroni,
> and show exactly the over-dispersion bias predicted below — their edge sits in
> regulation games (+12.6%) and reverses in extras (-12.3%), and flips negative
> from July (+14.3%) to August (-4.2%). Full write-up in the experiment record.

## Why this is pre-registered

The soccer version of this work (`research/experiments/2026-08-13-mls-latency-insensitive.md`)
was **not** pre-registered, and that is precisely why it ended unresolvable: 15 rules were
tested, exactly one cleared the raw 95% CI, and ~0.8 false positives are expected at that
rate — so the one winner could not be distinguished from noise, and no amount of
re-analysis of the same data can fix that.

Everything below — rule set, parameters, λ estimation method, and the pass bar — is fixed
now, before any MLB number has been computed.

## Observation

Two facts motivate testing baseball rather than continuing with soccer:

- **MLB is by far the deepest dataset on Kalshi.** ~1,690 settled `KXMLBGAME` markets and
  ~10,363 settled `KXMLBTOTAL` markets reaching back to 2026-06-08 (~10 weeks), versus
  ~945 matches for all 31 soccer leagues pooled. Kalshi's settled-market endpoint retains
  ~66 days, and MLB fills that window densely.
- **Totals are the only segment of the existing pre-game bot with positive realized ROI**
  (+6.82%, n=25, vs h2h −65.88%, n=13 in `findings/2026-08-12-weekly-review.md`).

Market structure confirmed before writing this: `KXMLBTOTAL` offers a per-game ladder
(median 11 lines, range 3.5–25.5) with `floor_strike` giving the exact line and
`strike_type="greater"` meaning YES = over. Settled `result` is available.

## Hypothesis

> As innings elapse in a low-scoring MLB game, the probability of exceeding the game's
> total falls faster than the Kalshi price reflects, so buying UNDER at a fixed inning
> is profitable after fees.

Latency-insensitive by construction: the trigger is a published game state (runs and
inning), not an event to react to. A data feed tens of seconds late is sufficient. This
is the same premise that made the soccer clock-decay family worth testing, applied where
the sample is an order of magnitude larger.

## Rules under test — exactly six, fixed now

Line selection: for each game, the **main line** = the ladder line whose YES price at
first pitch is closest to 0.50. Chosen because it is the market's own centre and requires
no judgement call.

| rule | trigger (evaluated at the end of inning N) | action |
|---|---|---|
| `CD-UNDER@5` | projected final (`runs_so_far × 9 / 5`) < main line | buy UNDER |
| `CD-UNDER@6` | projected final (`runs_so_far × 9 / 6`) < main line | buy UNDER |
| `CD-UNDER@7` | projected final (`runs_so_far × 9 / 7`) < main line | buy UNDER |
| `POIS-UNDER@5` | Poisson fair P(under) − market P(under) ≥ 0.05 | buy UNDER |
| `POIS-UNDER@6` | same, at inning 6 | buy UNDER |
| `POIS-UNDER@7` | same, at inning 7 | buy UNDER |

Six rules deliberately, not fifteen: every extra rule raises the multiple-testing bar and
that is what sank the soccer analysis.

Poisson model: remaining runs ~ Poisson(λ · innings_remaining / 9), so
P(over) = P(runs_so_far + X > line). Entry at the 1-minute candle's `1 − yes_bid`
(buying NO/under), net of `config.KALSHI_TAKER_FEE_RATE_ESTIMATE`. Held to settlement.

## λ is estimated OUT OF SAMPLE — the fix for the soccer flaw

In the soccer study λ was estimated from the same matches whose outcomes it predicted,
which flattered every `POIS-*` rule. Here:

- **λ is estimated on June 2026 games only.**
- **All six rules are evaluated on July–August 2026 games only.**

No parameter is fitted on the test period.

## Pass bar — fixed now

A rule is confirmed only if **both** hold on the July–August test period:

1. **n ≥ 200** qualifying trades.
2. The **Bonferroni-corrected** Wilson CI on the win rate (α = 0.05/6, z ≈ 2.64) clears
   the break-even rate implied by the average price paid.

Raw-CI results will be reported but are explicitly **not** sufficient. A rule that clears
raw and fails corrected is recorded as unresolved, not as a win.

Additionally, and independent of the statistics, any rule whose average cost exceeds
**0.90** is disqualified from implementation regardless of its numbers. At 90c a single
loss erases nine wins; the soccer work showed such rules look safest exactly when the
sample is too small to see the tail.

## Known caveats going in

- **Runs are not Poisson.** Baseball scoring is over-dispersed (big innings cluster), so
  the Poisson fair value will understate the tails and thus overstate P(under). This
  biases the `POIS-UNDER` rules toward *false positives* and is the main reason those
  three are being watched more sceptically than the `CD-*` three.
- **Extra innings** break the `innings_remaining` denominator. Games going past 9 will be
  handled by treating regulation as the horizon and letting settlement decide; this makes
  UNDER look slightly better than reality and must be checked in the write-up.
- **Line selection uses the price at first pitch**, which is in-sample in a mild sense
  (it uses market data from the game being traded) but involves no outcome information.
- **Depth is not modelled**, only spreads. Same limitation as the soccer study.
- **One 10-week window, one season, no regime variation.**
- Prior related result: the soccer clock-decay family produced no confirmed rule, and its
  most promising candidate decayed from +28.1% ROI at n=16 to +3.2% at n=247 as the
  sample grew. A similar decay here should be treated as the expected outcome, not a
  surprise.
