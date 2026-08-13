"""
Applies the PRE-REGISTERED pass bar to the MLB clock-decay results.

Bar, fixed in research/hypotheses/2026-08-13-mlb-clock-decay-totals.md before any
number was computed:

  1. n >= 200 qualifying trades on the July-August test period, AND
  2. the BONFERRONI-corrected Wilson CI (alpha = 0.05/6, z ~ 2.64) on the win rate
     clears the break-even implied by the average price paid, AND
  3. average cost <= 0.90 (a rule paying 90c to win 100c loses nine wins per loss;
     the soccer study showed such rules look safest exactly when the sample is too
     small to see the tail).

Raw 95% CI is reported for comparison but is explicitly NOT sufficient -- the soccer
study had one rule clear raw and fail corrected, and that is the whole reason this
experiment was pre-registered.

Segments checked (a pooled edge driven by one segment is not a strategy):
  - extras: games that went past 9 innings. The pre-registration flagged that treating
    regulation as the horizon flatters UNDER, so this split is a bias check, not a
    curiosity.
  - line, month.

Run: python3 research/analyze_mlb.py research/findings/mlb_clock_decay.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict

N_TESTS = 6
Z_RAW = 1.96
Z_CORR = 2.64          # two-sided alpha = 0.05/6
MIN_N = 200
MAX_COST = 0.90


def wilson(k: int, n: int, z: float) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def stats(t: list[dict]):
    n = len(t)
    k = sum(1 for x in t if x["won"])
    be = sum(x["cost"] for x in t) / n
    pnl = sum(x["pnl_c"] for x in t)
    cost = sum(x["cost"] for x in t) * 100
    return n, k, k / n, be, pnl / cost if cost else 0.0, pnl / n


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "research/findings/mlb_clock_decay.json"
    d = json.load(open(path))
    tr = d["trades"]
    print(f"lambda period: {d['n_lambda_games']} games (June, out of sample)")
    print(f"  lambda = {d['lam_total']:.2f} runs/game, reference line {d['ref_line']:.1f}")
    print(f"test period: {d['n_test_games']} games (July-August)")
    print(f"skipped: {d['skipped']}\n")

    print("=" * 104)
    print("PRE-REGISTERED BAR: n>=200 AND Bonferroni CI clears break-even AND avg cost <= 0.90")
    print("=" * 104)
    print(f"{'rule':16}{'n':>6}{'win%':>7}{'BE':>6}{'raw 95% CI':>16}"
          f"{'Bonferroni CI':>18}{'ROI':>8}{'cost':>7}  verdict")
    passed = []
    for rule in sorted(tr):
        t = tr[rule]
        if not t:
            continue
        n, k, w, be, roi, per = stats(t)
        rlo, rhi = wilson(k, n, Z_RAW)
        clo, chi = wilson(k, n, Z_CORR)
        checks = []
        if n < MIN_N:
            checks.append(f"n<{MIN_N}")
        if clo <= be:
            checks.append("CI fails")
        if be > MAX_COST:
            checks.append("cost>0.90")
        v = "** PASSES **" if not checks else "fails: " + ", ".join(checks)
        if not checks:
            passed.append(rule)
        print(f"{rule:16}{n:>6}{w:>7.0%}{be:>6.0%}{f'[{rlo:.0%},{rhi:.0%}]':>16}"
              f"{f'[{clo:.0%},{chi:.0%}]':>18}{roi:>+8.1%}{be:>7.2f}  {v}")

    print(f"\nrules passing the pre-registered bar: {len(passed)}"
          + (f" -> {passed}" if passed else ""))

    # segment the best-performing rule by ROI regardless of pass/fail
    best = max(tr, key=lambda r: stats(tr[r])[4] if tr[r] else -9)
    t = tr[best]
    n, k, w, be, roi, per = stats(t)
    print(f"\n{'='*104}\nSEGMENTS for best-ROI rule: {best} "
          f"(n={n}, win={w:.0%}, BE={be:.0%}, ROI={roi:+.1%})\n{'='*104}")
    for label, fn in (("extras (bias check)", lambda x: "extras" if x.get("extras") else "regulation"),
                      ("line", lambda x: f"{x.get('line')}"),
                      ("month", lambda x: x["date"][:7])):
        g = defaultdict(list)
        for x in t:
            g[fn(x)].append(x)
        print(f"  -- by {label} --")
        for key in sorted(g, key=lambda kk: -len(g[kk])):
            s = g[key]
            if len(s) < 5:
                continue
            sn, sk, sw, sbe, sroi, _ = stats(s)
            print(f"     {str(key):14} n={sn:>5} win={sw:>5.0%} BE={sbe:>5.0%} ROI={sroi:>+7.1%}")

    losses = [x for x in t if not x["won"]]
    wins = [x for x in t if x["won"]]
    if losses and wins:
        al = sum(x["pnl_c"] for x in losses) / len(losses)
        aw = sum(x["pnl_c"] for x in wins) / len(wins)
        print(f"\n  loss asymmetry: {len(losses)} losses avg {al:.1f}c vs "
              f"{len(wins)} wins avg {aw:.1f}c -> one loss erases {abs(al)/aw:.1f} wins")


if __name__ == "__main__":
    main()
