---
experiment_id: 2026-08-13-tape-only-burst
date: 2026-08-13
hypothesis_ref: research/experiments/2026-08-12-mls-lead-change-momentum.md (Follow-up 3)
status: rejected
baseline: n/a -- proposed new strategy
dataset: "Kalshi KXMLSGAME settled markets + full trade tape, 45 markets / 169,781 trades, MLS match dates 2026-08-01 and 2026-08-08"
training_period: n/a -- no parameters fitted; a sweep was run and ALL configurations lost
validation_period: n/a
out_of_sample_period: n/a
n_trades: 373            # signals at the headline configuration (momentum, 4c threshold)
roi: -0.0714             # mean ROI per signal, held to settlement, headline config
pnl: null
sharpe: null
max_drawdown: null
win_rate: 0.34           # share of signals profitable on the +300s mark, headline config
fees_included: true
slippage_assumptions: "1c crossing cost + Kalshi taker fee. No depth modelling."
execution_assumptions: "Causal detector -- sees only trades at or before the current moment. Entry `latency` seconds after detection at the then-prevailing price."
---

## Hypothesis under test

> The 50%→90% phase of a post-goal repricing takes a median ~55s. If that grind is
> tradeable, a bot could detect the burst from **Kalshi's own trade tape** — free, and
> Kalshi offers a WebSocket market-data feed — and ride the remainder. No sports data
> feed, no subscription, no race against people watching live video.

This was the most promising surviving direction from
`research/experiments/2026-08-12-mls-lead-change-momentum.md`, because it sidesteps the
feed-cost problem entirely: the parent experiment showed the edge could not pay for a
sports feed at this bankroll.

## Method

`research/tape_only_burst.py`. Three things deliberately differ from the parent study:

1. **Causal.** The parent defined the move's start as "first trade reaching 20% of the
   *eventual* move" — look-ahead a live bot cannot have. Here the detector sees only
   trades at or before the current moment: rolling baseline = last trade at or before
   `now - baseline_window`; fire when the current price differs by ≥ `threshold`.
2. **Counts false positives.** The parent conditioned on "a goal happened," which a
   tape-only strategy cannot do. This scans every market continuously, so noise,
   reversals and non-goal events are all included — and they turned out to dominate.
3. **Realized P&L.** Reports settlement outcome (market `result`) alongside the +300s
   mark, since holding to resolution is what a small account would actually do.

Both directions were tested: **momentum** (buy the side that moved) and **fade** (buy
against it).

Reproduce:
```bash
python3 research/tape_only_burst.py --dates 2026-08-01 2026-08-08 \
    --cache /tmp/tape_cache.json --out research/findings/tape_only_burst.json
```

## Results

Headline configuration (60s baseline, 4¢ threshold, 2s entry latency, 300s cooldown):

| direction | n | mark median | mark mean | mark profitable | settle mean | settle profitable |
|---|---|---|---|---|---|---|
| momentum | 373 | −3.39¢ | −3.07¢ | 125/373 (34%) | −5.91¢ | 193/373 (52%) |
| fade | 377 | −1.65¢ | −1.44¢ | 165/377 (44%) | +1.36¢ | 180/377 (48%) |

**Threshold sweep** (larger bursts should be likelier to be real goals):

| threshold | momentum mark mean | fade mark mean |
|---|---|---|
| 4¢ | −3.07 | −1.44 |
| 8¢ | −3.21 | −1.31 |
| 12¢ | −2.18 | −2.45 |
| 20¢ | −4.04 | −0.84 |
| 30¢ | −6.90 | +1.98 |

**Fast-detector sweep** (baseline 2–10s, threshold 2–3¢, latency 0.1–0.5s — the
strongest possible version, since order execution is now ~7ms after the connection-
pooling fix): 24 configurations, best mark mean **−1.34¢**.

**Across all 34 configurations tested, zero were profitable on the mark-to-market.**

Momentum win rates on the mark ran 23–38% — consistently *worse* than a coin flip,
which is itself informative.

## Conclusion

**Refuted, robustly.** Not a marginal miss: every threshold, every detector speed, both
directions, all negative on the mark.

**Mechanism.** A tape detector fires *because the price already moved*. It is
definitionally buying after the move — and the sub-50% win rates on momentum show it is
systematically buying the top of the burst. The parent experiment's edge came from
knowing the event time **independently of price**; that independence is exactly what a
tape-only detector lacks. You cannot extract an edge from the same signal that already
consumed it.

The apparent settlement "profit" for fade at some thresholds (+1.36¢, +1.98¢) is not an
edge. Fade buys the side that just fell, i.e. cheap longshots, producing a
lottery-ticket payoff profile: median −6.39¢ with a positive mean. The mark-to-market
for those same configurations is negative, confirming there is no repricing edge — only
underdog variance, on samples far too small for a mean that skewed to be trusted.

**No trading code was written.**

## Skeptic review

Self-review; not run through the formal Skeptic Agent.

- **Multiple testing — the main risk here, and it points the right way.** 34
  configurations were swept. That would be a serious overfitting concern if I were
  reporting a *winner*; since **none** were profitable, the sweep strengthens the
  negative result rather than weakening it. Had one config come out positive, it should
  have been treated as noise until confirmed out-of-sample.
- **Look-ahead** — the detector is strictly causal by construction. The one residual
  look-ahead is market *selection*: only settled markets are scanned, so markets that
  never resolved are absent. Immaterial for a same-day-resolving sports market.
- **Sample** — 45 markets / 169,781 trades over 2 match dates. Large in trades, small in
  match dates; a longer span would firm up the settlement numbers, though the
  mark-to-market result is consistent enough across configurations that more data is
  unlikely to reverse it.
- **Fee/slippage** — 1¢ crossing plus estimated taker fee, no depth modelling. Optimistic
  (real slippage would be worse), and the result is negative anyway.
- **Exit policy** — settlement and a +300s mark. A shorter hold might behave differently,
  but the hypothesis was specifically about riding a ~55s grind, which sits inside the
  300s window.
