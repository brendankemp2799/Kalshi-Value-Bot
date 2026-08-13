---
experiment_id: 2026-08-13-mls-latency-insensitive
date: 2026-08-13
hypothesis_ref: none -- NOT pre-registered, see Caveats
status: rejected         # CD-UNDER@45 rejected; CD-UNDER@55 survives raw test but fails Bonferroni
baseline: n/a -- proposed new strategies
dataset: "945 completed matches across 31 verified soccer leagues, 35 dates 2026-07-10 .. 2026-08-13 (expanded from an initial 51-match MLS-only sample; see UPDATE 2)"
training_period: n/a -- no parameters fitted except the in-sample lambda noted below
validation_period: n/a
out_of_sample_period: none available
n_trades: 247            # CD-UNDER@45 on the full 31-league pool
roi: 0.032               # CD-UNDER@45 at n=247; was +28.1% at n=16 -- decayed with sample size
pnl: null
sharpe: null
max_drawdown: null
win_rate: 0.834          # CD-UNDER@45, 206/247
fees_included: true
slippage_assumptions: "Entry at the 1-min candle yes_ask (buy YES) or 1 - yes_bid (buy NO). Median spread 1-2c measured at the decision points. No depth modelling."
execution_assumptions: "Latency-insensitive by design -- entry at a fixed match minute, held to settlement. A 30s-late feed is sufficient."
---

## Hypotheses under test

Two strategies that, unlike everything previously tested here, do **not** react to an
event. Their premise is that the market misprices a *known, publicly visible state* —
the score and the clock — so a feed that is 30s late is fine and no race is involved.
This directly addresses why the two prior attempts failed
(`2026-08-12-mls-lead-change-momentum.md`, `2026-08-13-tape-only-burst.md`).

**Clock decay** — pure state rules:
- `CD-UNDER@M` — score is 0-0 at minute M → buy UNDER 2.5 (NO on "Over 2.5")
- `CD-TIE@M` — score is level at minute M → buy TIE

**Poisson divergence** — a real fair value from (score, minutes remaining). Remaining
goals ~ Poisson(λ · remaining/90), giving an exact P(over 2.5); P(tie) via a
Skellam-style convolution of the two teams' independent remaining-goal distributions.
Bet whichever side Kalshi disagrees with by more than 5c.

## Method

`research/mls_ingame_state.py` reconstructs, per match, a `minute → (score, wallclock)`
timeline. Match minute is anchored on ESPN's own `Kickoff` and `Start 2nd Half`
wallclock stamps rather than assuming a fixed 15-minute break, since stoppage time makes
any fixed offset wrong. Matches missing either anchor are skipped, never estimated.

Goals are attributed by **team id** with the running tally **validated against the
official final score**; any match whose reconstruction disagrees is dropped entirely.
This was added after a first pass produced matches with a 2-3 final score and zero
recorded goals — ESPN's goal description text does not always spell a team the same way
as its `displayName` ("Atlanta United" vs "Atlanta United FC"), so text parsing silently
failed. A silently wrong timeline reads Kalshi prices for the wrong game state, which is
worse than a smaller sample.

`research/mls_latency_insensitive.py` runs both strategies. Entry at the 1-minute candle
(adequate here precisely because these strategies are latency-insensitive), held to
settlement — realized P&L, not a mark.

## Results

Sample: **51 matches, 7 dates, 2026-07-16 to 2026-08-08.** Estimated goal intensity:
home 1.61, away 1.25, total 2.86.

Execution is viable: median spread at the decision points was **1c** (minutes 55, 65)
and **2c** (minute 45), with real traded volume.

| rule | n | win% | mean P&L | ROI | avg cost |
|---|---|---|---|---|---|
| CD-UNDER@45' | 16 | 94% | +20.26c | +28.1% | 0.72 |
| CD-UNDER@55' | 12 | 100% | +13.32c | +15.5% | 0.86 |
| CD-UNDER@65' | 11 | 100% | +5.95c | +6.4% | 0.94 |
| POIS-UNDER@75' | 16 | 69% | +10.74c | +19.0% | 0.56 |
| POIS-UNDER@45' | 40 | 45% | +4.37c | +11.1% | 0.39 |
| CD-TIE@75' | 18 | 56% | −1.44c | −2.6% | 0.55 |
| CD-TIE@65' | 19 | 42% | −4.05c | −9.1% | 0.44 |
| CD-TIE@80' | 24 | 58% | −4.26c | −7.0% | 0.61 |
| POIS-TIE@65'/75'/80' | 14/17/24 | 36/53/58% | −2.99/−3.72/−4.26c | −8.0/−6.8/−7.0% | — |

**The decisive analysis — Wilson 95% CI on the win rate vs the break-even rate implied
by the price paid:**

| rule | n | win% | break-even | 95% CI | verdict |
|---|---|---|---|---|---|
| CD-UNDER@45' | 16 | 94% | 72% | [72%, 99%] | **indistinguishable** |
| CD-UNDER@55' | 12 | 100% | 86% | [76%, 100%] | **indistinguishable** |
| CD-UNDER@65' | 11 | 100% | 94% | [74%, 100%] | **indistinguishable** |
| POIS-UNDER@75' | 16 | 69% | 56% | [44%, 86%] | **indistinguishable** |
| …all 14 rules | | | | | **indistinguishable** |

**Not one of the 14 rules produces a confidence interval that clears its break-even
rate.** CD-UNDER@45' comes closest, and its lower bound sits exactly *on* break-even.

## Conclusion

**Inconclusive — and the data required to resolve it does not exist yet.**

Nothing here is proven. The headline ROIs are real arithmetic on the sample but the
sample cannot support them.

**CD-UNDER@45' (0-0 at halftime → buy UNDER 2.5) is the one candidate worth pursuing.**
It has the largest margin over break-even (94% vs 72%) and the most room per trade
(pay 72c to win 100c, so a loss costs ~2.6 wins rather than 16). Poisson puts fair value
at ~82.5% while the market priced ~72%, a ~10c gap; observed 15/16. A plausible
mechanism exists — recreational money favours "over" in-play, depressing the under side —
but mechanism plus n=16 is not evidence.

**Resolving it needs ~40 qualifying trades.** The rule fires on ~31% of matches
(16 of 51), so that is ~130 matches — **about 2.5× the entire Kalshi MLS history that
exists**, or roughly 9 more weeks of MLS collected forward.

**CD-TIE and POIS-TIE are dead.** Negative ROI at every minute tested, CIs centred at or
below break-even. No reason to revisit.

**The high-price CD-UNDER variants (@55, @65) are structurally dangerous and should not
be built even though they show 100% win rates.** At 94c you win 6c and lose 94c — one
loss erases 16 wins, so break-even is 94% and n=11 says essentially nothing about
whether the true rate is above or below that. This is the classic picking-up-pennies
profile, and the small sample is exactly what makes it look safe.

**No trading code was written.**

## Caveats

- **NOT pre-registered.** Unlike the lead-change study, these rules were run first and
  written up after. 14 rules were tested; at 5% significance ~0.7 false positives would
  be expected by chance. None cleared, so nothing is being claimed — but had one cleared,
  multiple testing would have to be discounted for. Any forward test of CD-UNDER@45'
  should fix the parameters **now**, before collecting more data.
- **λ is estimated in-sample.** The Poisson fair value is fitted on the same 51 matches
  whose outcomes it predicts, which flatters every `POIS-*` rule. The `CD-*` rules are
  unaffected — their trigger does not use λ at all.
- **One 4-week window, one league, mid-season.** No regime variation whatsoever.
- **Depth not modelled.** Spreads were measured (1-2c) but not resting size.
- **Survivorship in market matching** — matches whose Kalshi event suffix could not be
  resolved are silently dropped.

## UPDATE 2026-08-13 (same day): the data ceiling was WRONG — pooled to 230 matches

The claim above that 51 matches is "the ENTIRE Kalshi MLS market history" was correct
for MLS but **badly wrong as a limit on the research**, and the reasoning that produced
it was sloppy in two ways:

1. **The original probe used the wrong filter.** It searched `close_time` within ±8h of
   kickoff. This repo documents `close_time` as a settlement deadline that can sit well
   after the game, so that probe could have missed markets entirely. Re-querying
   `/markets?status=settled` with **no** time filter is the correct method.
2. **A retention limit was mistaken for a coverage limit.** Kalshi's settled-market
   endpoint retains roughly 66 days (MLB reaches back to 2026-06-08, ~10 weeks, 1690
   game markets). Within that window MLS genuinely starts 2026-07-16 — but so does
   *every* soccer league, which is the tell: **Kalshi launched broad soccer coverage in
   mid-July**, it was never MLS-specific.

**Kalshi carries 133 soccer TOTAL series** (3354 sports series overall). This repo maps
three soccer leagues; there are dozens. Eight were verified end-to-end against ESPN and
pooled:

| league | ESPN slug | matches |
|---|---|---|
| MLS | usa.1 | 51 |
| Argentina Primera | arg.1 | 60 |
| Leagues Cup | concacaf.leagues.cup | 49 |
| Brazil Serie A | bra.1 | 38 |
| Chinese Super League | chn.1 | 32 |
| Allsvenskan / Eliteserien / J-League | swe.1 / nor.1 / jpn.1 | **0** (see below) |

**230 matches — 4.5× the MLS-only sample.**

### Result on the pooled sample

| rule | n | win% | break-even | 95% CI | ROI | verdict |
|---|---|---|---|---|---|---|
| **CD-UNDER@45'** | **76** | **86.8%** | **79.2%** | **[77.4%, 92.7%]** | **+8.3%** | **indistinguishable** |
| CD-UNDER@55' | 52 | 94% | 89% | [84%, 98%] | +4.6% | indistinguishable |
| CD-UNDER@65' | 37 | 92% | 93% | [79%, 97%] | −2.1% | indistinguishable |
| POIS-UNDER@45' | 138 | 49% | 44% | [41%, 58%] | +8.8% | indistinguishable |
| CD-TIE (all) | 71–84 | 39–63% | 47–62% | — | −19.8% .. −0.3% | indistinguishable |

**The most important number: CD-UNDER@45' ROI fell from +28.1% (n=16) to +8.3% (n=76).**
That is textbook small-sample regression — the MLS-only figure was substantially luck.
The edge may be real but it is roughly a third of what the first pass suggested, and it
could regress further.

Still indistinguishable: the CI lower bound (77.4%) sits 1.8 points *below* break-even.
It is stable across months though (34/39 in July, 32/37 in August), so no regime split
is visible within the window.

### What would resolve it

Holding the observed 86.8% and 79.2% break-even:

| trades | CI lower | verdict |
|---|---|---|
| 76 (now) | 77.4% | ambiguous |
| 100 | 79.0% | ambiguous |
| **120** | **79.4%** | **resolved** |
| 150 | 80.3% | resolved |

The rule fires on 33% of matches, so **~120 trades ≈ 364 matches ≈ 134 more than we
have**. Three routes, all free:

1. **Recover the three dead leagues.** Allsvenskan, Eliteserien and J-League have
   `wallclockAvailable=True` but no `Kickoff` / `Start 2nd Half` keyEvents, so the strict
   anchor requirement drops them. Kickoff could be *implied* from any first-half event
   (`wallclock - clock_minutes`), which is far better than a fixed-offset assumption
   though still an estimate. Worth ~60–80 matches.
2. **Add the leagues with Kalshi markets but no verified ESPN slug** — K-League,
   Canadian Premier League, Ekstraklasa (their `kor.1` / `can.1` / `pol.1` guesses
   returned HTTP 400). Worth another ~130 Kalshi game markets.
3. **Collect forward.** These 8 leagues produce ~230 matches per 4 weeks, so ~3 more
   weeks reaches n≈120 on its own — and would be genuinely out-of-sample.

Route 3 is the only one that produces *out-of-sample* evidence, which matters here
because these rules were not pre-registered.

## UPDATE 2 (2026-08-13): pooled to 945 matches across 31 leagues — CD-UNDER@45 is dead

Expanded from 8 verified leagues to **31**, giving **945 matches** (vs 51 MLS-only).
Method notes in `research/mls_ingame_state.py`; per-rule verdicts via
`research/analyze_rules.py`; rows in `research/findings/soccer_31league.json`.

### The trajectory is the finding

| sample | n trades | ROI |
|---|---|---|
| MLS only | 16 | **+28.1%** |
| 8 leagues | 76 | **+8.3%** |
| 31 leagues | **247** | **+3.2%** |

**Monotonic decay toward zero as the sample grows.** That is the signature of an edge
that was never there — each expansion washed out more of the original luck. At n=247 the
CI is [78.3%, 87.5%] against a break-even of 79.8%, still not clearing, and now with
almost nothing left to clear it by.

The by-anchor split is a second warning: the residual sits entirely in ESPN-**stamped**
matches (+7.2%, n=138) while **inferred**-anchor matches are slightly negative (−1.8%,
n=109). If the effect were a genuine market misprice it should not care how we recovered
the clock; that split looks like timing noise, not edge.

Per-league results scatter widely around zero on small n — usa.1 +28.1% (n=16),
ned.1 +40.8% (n=3), sco.cis +35.1% (n=5) against uefa.wchampions_qual −47.1% (n=7),
par.1 −24.8% (n=8), mex.1 −21.8% (n=8). That is noise, not a pattern.

**CD-UNDER@45' is rejected.**

### One rule cleared the raw test — and does not survive correction

`CD-UNDER@55'` (still 0-0 at minute 55 → buy UNDER 2.5): n=158, 94.9% win vs 89.0%
break-even, raw 95% CI **[90.3%, 97.4%]** — clears. ROI +5.9%.

But **15 rules were tested**, so ~0.8 false positives are expected at 5%, and exactly
one cleared. Applying Bonferroni (z=2.94 for α=0.05/15):

> corrected CI **[87.1%, 98.1%]** vs break-even 89.0% → **NO LONGER CLEARS**

Two things genuinely argue in its favour, and should not be dismissed:
- Consistent across anchor source (inferred +5.5%, stamped +6.3%) — unlike CD-UNDER@45.
- Consistent across months (July +6.7%, August +5.1%).

Two things argue against, hard:
- **The risk profile is brutal.** At an 89¢ average cost: a win pays +10.2¢, a loss costs
  −87.5¢. **One loss erases 8.5 wins.** There were 8 losses in 158. A modest drift in the
  true rate flips this negative, and the small sample is exactly what makes it look safe.
- Its own family disagrees: `@45'` (+3.2%) and `@65'` (+0.8%) do not clear, and `@65'` at
  a 93% break-even is effectively a coin flip on ruin.

### What would settle it

To clear a Bonferroni-corrected bar, holding the observed 94.9%:

| n | corrected CI low | verdict |
|---|---|---|
| 158 (now) | 87.1% | no |
| 250 | 89.0% | no |
| **400** | **90.7%** | **clears** |

It fires on 16.7% of matches, so n=400 needs **~2,395 matches — 1,450 more than we
have**. At ~945 matches per 5 weeks across these 31 leagues, that is roughly **8 more
weeks** of forward collection.

Forward collection is also the *right* method here rather than merely the available one:
these rules were never pre-registered, and only out-of-sample data resolves a
multiple-testing problem. **Pre-register `CD-UNDER@55'` now** — minute 55, line 2.5, 0-0
trigger, entry at `1 - yes_bid` — and test it forward untouched.

### Economics, for scale

If the edge is real, at `MAX_PCT_BANKROLL = 0.05` on a $166 bankroll (~$8.32/bet, ~9
contracts): ~$0.49 expected per bet, ~158 bets per 5 weeks ≈ **~$62/month**. That is the
optimistic case, and it assumes an edge that has not survived correction.

## Structural finding worth recording separately

**Kalshi's settled-market endpoint retains ~66 days**, and **Kalshi launched broad
soccer coverage around 2026-07-16** — every soccer league checked starts within days of
that date, MLS is not special. MLB reaches back to 2026-06-08 (1690 game markets,
10363 totals markets), confirming the 66-day retention rather than a coverage gap.

Practical consequences:

- **Query settled markets with NO close_ts filter.** `close_time` is a settlement
  deadline, not game time, so a kickoff-anchored window can silently miss markets. This
  is what produced the incorrect "no data before July 16" conclusion in the first pass.
- **Pool leagues.** Kalshi carries 133 soccer TOTAL series; this repo maps 3
  (`_SPORT_TO_SERIES` in `data/kalshi_client.py`). Eight verified leagues give 230
  matches per ~4 weeks versus 51 for MLS alone.
- **MLB is by far the deepest sports dataset on Kalshi** — 10 weeks and ~6x the market
  count of all soccer combined. Any strategy expressible in innings rather than minutes
  should be tested there first, purely for statistical power.

**Correction to `research/experiments/2026-08-12-mls-lead-change-momentum.md`:** that
record states its dataset spans "17 completed match dates 2026-05-02 .. 2026-08-08".
Those May dates contributed **no** usable rows — Kalshi had no MLS markets then. Its
effective sample was 2026-07-16 to 2026-08-08, the same 7 dates used here. The n=41
figure is unaffected (the goals all came from the July/August dates), but the claimed
span is wrong and is corrected in that file.
