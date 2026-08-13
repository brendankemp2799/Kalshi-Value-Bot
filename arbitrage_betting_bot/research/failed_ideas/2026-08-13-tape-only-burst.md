---
id: 2026-08-13-tape-only-burst
date: 2026-08-13
status: rejected
source: manual
experiment_ref: research/experiments/2026-08-13-tape-only-burst.md
parent_ref: research/experiments/2026-08-12-mls-lead-change-momentum.md
---

## Original idea

Post-goal repricing on Kalshi MLS moneylines happens in two phases: 20%→50% of the move
in a median 0.9s, then 50%→90% over a median ~55s. If that slow second phase is
tradeable, detect the burst from **Kalshi's own trade tape** and ride the remainder —
no sports data feed, no subscription, no race against people watching live video.

Attractive because the parent experiment established that the post-goal edge is real but
cannot pay for a sports feed at a ~$165 bankroll. This would have captured it for free.

## Why it was rejected

`research/tape_only_burst.py`, run over 45 Kalshi MLS markets / 169,781 trades:

- Headline config (60s baseline, 4¢ threshold, 2s latency): momentum **−3.07¢** mean on
  the mark, profitable 125/373 (34%).
- Threshold sweep 4¢→30¢: every value negative for momentum.
- Fast-detector sweep (2–10s baseline, 2–3¢ threshold, 0.1–0.5s latency — the strongest
  version possible, since execution is now ~7ms): best of 24 configs was **−1.34¢**.
- **34 configurations tested, zero profitable on the mark-to-market.**

Momentum win rates ran 23–38% — consistently worse than a coin flip.

## Root cause — and the generalizable lesson

A tape detector fires *because the price already moved*. It is definitionally buying
after the move, and the sub-50% win rates show it is systematically buying the top of
the burst.

The parent experiment's edge came from knowing the event time **independently of
price** — ESPN told us a goal happened, and we compared that to where the price was.
That independence is precisely what a tape-only detector lacks.

> **You cannot extract an edge from the same signal that already consumed it.**

Worth remembering before proposing any future "detect the move from the tape" strategy:
the tape is the thing that already priced it in.

## One trap this nearly set

Fade showed a *positive settlement mean* at some thresholds (+1.36¢ at 4¢, +1.98¢ at
30¢). That is not an edge. Fade buys the side that just fell — cheap longshots — giving
a lottery-ticket profile: median **−6.39¢** with a positive mean, driven by a handful of
large winners. The mark-to-market for those same configurations is negative, which is
the tell: no repricing edge, only underdog variance on a sample far too small for a mean
that skewed.

If a future pass revisits this, judge it on the mark-to-market and the median, not the
settlement mean.

## What this does NOT rule out

The parent strategy (ESPN-triggered, goal-conditioned) still has a real measured edge —
+10 to +12¢ at 1–5s lateness. It is shelved for economic reasons (capacity-constrained,
and the edge cannot fund a feed at this bankroll), not because the signal is absent. See
the parent experiment record.

Also unaffected: the **latency-insensitive** families never tested — clock decay (0-0 at
≥55' → UNDER 2.5; level at ≥75' → TIE) and in-game Poisson divergence. Their premise is
that the market misprices a *known, publicly visible state*, which requires no event
detection at all and so is immune to the failure mode above. Both target totals, the only
segment of the existing bot with positive realized ROI (+6.82%, n=25, vs h2h −65.88%).
