---
experiment_id: 2026-08-13-mlb-clock-decay-totals
date: 2026-08-13
hypothesis_ref: research/hypotheses/2026-08-13-mlb-clock-decay-totals.md
status: rejected          # 0 of 6 rules passed the pre-registered bar
baseline: n/a -- proposed new strategy
dataset: "MLB StatsAPI play-by-play + Kalshi KXMLBTOTAL. Lambda: 295 June games (out of sample). Test: 535 games, 44 dates, 2026-07-01 .. 2026-08-13"
training_period: "2026-06-09 .. 2026-06-30 (lambda + reference line only)"
validation_period: n/a
out_of_sample_period: "2026-07-01 .. 2026-08-13 -- all six rules evaluated here only"
n_trades: 280             # CD-UNDER@5, the largest arm
roi: 0.035                # CD-UNDER@5; best arm POIS-UNDER@5 was +9.6% but n=120
pnl: null
sharpe: null
max_drawdown: null
win_rate: 0.81            # CD-UNDER@5
fees_included: true
slippage_assumptions: "Entry at 1-min candle 1 - yes_bid (buying NO/under), plus KALSHI_TAKER_FEE_RATE_ESTIMATE. No depth modelling."
execution_assumptions: "Latency-insensitive -- trigger is end-of-inning game state read from StatsAPI about.endTime. Held to settlement."
preregistered: true
---

## Hypothesis under test

> As innings elapse in a low-scoring MLB game, the probability of exceeding the game's
> total falls faster than the Kalshi price reflects, so buying UNDER at a fixed inning
> is profitable after fees.

Pre-registered in full — rules, parameters, λ split and pass bar — **before any MLB
number was computed**. The soccer predecessor
(`2026-08-13-mls-latency-insensitive.md`) was not pre-registered, tested 15 rules, and
ended unresolvable because its single raw-CI winner could not be separated from the ~0.8
false positives expected by chance. This experiment exists partly to not repeat that.

## Method

- **State**: `research/mlb_ingame_state.py`. End-of-inning runs and wallclock read
  directly from StatsAPI `about.endTime` — no inference, unlike the soccer layer which
  had to derive anchors for leagues ESPN does not stamp.
- **λ out of sample**: estimated on **295 June games** (mean 9.29 runs/game); all six
  rules evaluated only on **535 July–August games**. No parameter fitted on the test period.
- **Line selection** (as registered): the ladder rung whose YES price at first pitch is
  closest to 0.50 — the market's own centre, using no outcome information.
- **Six rules only**, to keep the multiple-testing bar low.
- Backtest: `research/mlb_latency_insensitive.py`; verdict: `research/analyze_mlb.py`;
  rows: `research/findings/mlb_clock_decay.json`.

## Results

**0 of 6 rules passed the pre-registered bar** (n ≥ 200 **and** Bonferroni-corrected CI
clears break-even **and** avg cost ≤ 0.90).

| rule | n | win% | BE | raw 95% CI | Bonferroni CI | ROI | cost | verdict |
|---|---|---|---|---|---|---|---|---|
| CD-UNDER@5 | 280 | 81% | 78% | [76%, 86%] | [75%, 87%] | +3.5% | 0.78 | fails CI |
| CD-UNDER@6 | 270 | 83% | 82% | [78%, 87%] | [77%, 88%] | **+0.3%** | 0.82 | fails CI |
| CD-UNDER@7 | 252 | 86% | 85% | [81%, 89%] | [79%, 91%] | **+0.4%** | 0.85 | fails CI |
| POIS-UNDER@5 | 120 | 88% | 79% | **[80%, 92%]** | [77%, 93%] | +9.6% | 0.79 | fails n<200, CI |
| POIS-UNDER@6 | 105 | 90% | 82% | **[83%, 95%]** | [80%, 96%] | +8.7% | 0.82 | fails n<200, CI |
| POIS-UNDER@7 | 97 | 86% | 83% | [77%, 91%] | [74%, 93%] | +2.5% | 0.83 | fails n<200, CI |

**The clock-decay family is flat.** CD-UNDER@6 and @7 return +0.3% and +0.4% — that is
zero, on the largest samples in the study. Only CD-UNDER@5 shows anything (+3.5%) and its
raw CI lower bound (76%) sits *below* its break-even (78%).

**The Poisson family looks better and should not be believed**, for the reason written
into the pre-registration before the run:

> "Runs are not Poisson. Baseball scoring is over-dispersed (big innings cluster), so the
> Poisson fair value will understate the tails and thus overstate P(under). This biases
> the `POIS-UNDER` rules toward *false positives*."

That is exactly the pattern observed. The three arms that look best are the three the
model was predicted to inflate.

### Segment checks on the best arm (POIS-UNDER@5, +9.6%)

| split | n | win% | BE | ROI |
|---|---|---|---|---|
| **regulation** | 106 | 90% | 79% | **+12.6%** |
| **extras** | 14 | 71% | 80% | **−12.3%** |
| July | 91 | 90% | 78% | **+14.3%** |
| August | 29 | 79% | 82% | **−4.2%** |

Both registered bias checks fire:

- **Extras.** The pre-registration flagged that treating regulation as the horizon
  flatters UNDER. It does: regulation games carry the entire edge and extra-inning games
  lose 12.3%. Part of the headline is that bias, not a misprice.
- **Time instability.** July +14.3% vs August −4.2%. An edge that reverses sign between
  adjacent months on n=91/29 is not an edge.

Loss asymmetry: 15 losses averaging −73.2¢ against 105 wins averaging +19.1¢ — **one loss
erases 3.8 wins**. Better than the soccer rules (8.5) but still the same shape.

## Conclusion

**Rejected.** No rule met the bar, and the two families fail for different, informative
reasons:

- **Clock decay (CD-\*) is genuinely flat.** With n=252–280 per arm — larger than
  anything in the soccer study — the answer is +0.3% to +3.5%, i.e. the market prices
  the decay of a total about right. This is the cleanest negative result in the whole
  live-betting line of work, because the sample is adequate and the answer is simply "no".
- **Poisson divergence (POIS-\*) is a modelling artifact.** It looks profitable exactly
  where its own model is known to be wrong, its edge concentrates in the segment the
  registered bias favours, and it flips negative in the second month.

Arithmetically, POIS-UNDER@5 would clear a Bonferroni bar at **n=200** (currently 120),
and its regulation-only subset already does (n=106, corrected CI [79.2%, 95.1%] vs BE
78.6%). **That is not a reason to pursue it.** Conditioning on "regulation only" is a
post-hoc filter that cannot be applied live — you do not know at inning 5 whether a game
will go to extras — and chasing the n=200 threshold on an arm already flagged for model
bias and month instability is the exact pattern that produced the soccer dead end.

The same sample-size decay seen in soccer repeated here: CD-UNDER@6 went **+2.8% (n=15) →
+0.3% (n=270)**.

**No trading code was written.**

## What this closes out

Across four experiments the live-betting programme now stands at:

| approach | verdict | why |
|---|---|---|
| Lead-change momentum (soccer) | shelved | real edge, but capacity-constrained and cannot fund a feed at this bankroll |
| Tape-only burst detection | rejected | 0/34 configs; a tape detector buys after the move by construction |
| Clock decay / Poisson (soccer) | unresolved | not pre-registered; 1/15 cleared raw, failed Bonferroni |
| Clock decay / Poisson (MLB) | **rejected** | 0/6 pre-registered; CD family flat at adequate n |

The MLB result is the most decisive because it is the only one with both a
pre-registered bar and a sample large enough to answer the question. **In-game totals
mispricing, in the form tested, does not exist at a scale worth trading.**

## Skeptic review

Self-review; not run through the formal Skeptic Agent.

- **Pre-registration held.** One deviation occurred and was corrected before the full
  run: line selection was initially implemented as "rung nearest the June median total"
  rather than the registered "rung whose first-pitch price is nearest 0.50". The
  substitute produced entries at 0.88–0.90 (at the registered disqualification
  threshold); the registered method gives 0.78–0.85. All reported numbers use the
  registered method.
- **λ genuinely out of sample** — June only, test on July–August. This removes the
  in-sample flaw that inflated the soccer `POIS-*` arms.
- **Look-ahead** — line selection uses first-pitch prices (no outcome information);
  entry uses the candle covering the end of the inning; settlement is strictly later.
- **Multiple testing** — six rules, corrected at α=0.05/6. Reported both raw and corrected.
- **Depth not modelled**, only prices. Same limitation as all prior work here.
- **`no_price: 37`** entries were skipped for missing candles. Small relative to ~800
  evaluations but not audited for bias; if illiquid games are systematically dropped the
  sample tilts toward liquid ones.
- **One season, one 10-week window.** No regime variation, though the July/August split
  already shows instability within it.
