"""
Statistical verdict for the latency-insensitive soccer rules.

WHY THIS IS SEPARATE FROM THE BACKTEST
--------------------------------------
The backtest reports win rate and ROI, which are easy to over-read. On these markets
the price paid IS the break-even win rate: pay 79c to win 100c and you need 79% just to
tread water. A rule showing "94% win rate" at an 89c average cost is barely above water,
and a 100% win rate on n=11 at 94c says essentially nothing.

So the verdict here is always the same test: does the Wilson 95% CI on the win rate
clear the break-even rate implied by the average cost? Anything else is decoration.

SEGMENTATION
------------
The pooled sample mixes men's and women's leagues, first and second divisions, and cup
competitions -- all with different goal rates. An aggregate edge that exists only
because of one segment is not a strategy, so results are also split by:
  - league
  - whether the match's clock anchors were INFERRED rather than ESPN-stamped
    (research/mls_ingame_state.py falls back to inference for leagues that emit no
    Kickoff marker; a systematic difference here would indicate timing error, not edge)

Run:
    python3 research/analyze_rules.py research/findings/soccer_31league.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from statistics import median


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def verdict(trades: list[dict]) -> tuple:
    n = len(trades)
    k = sum(1 for t in trades if t["won"])
    be = sum(t["cost"] for t in trades) / n
    lo, hi = wilson(k, n)
    pnl = sum(t["pnl_c"] for t in trades)
    cost = sum(t["cost"] for t in trades) * 100
    roi = pnl / cost if cost else 0.0
    if lo > be:
        v = "PROFITABLE"
    elif hi < be:
        v = "losing"
    else:
        v = "indistinguishable"
    return n, k / n, be, lo, hi, roi, v


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "research/findings/soccer_31league.json"
    d = json.load(open(path))
    tr = d["trades"]
    print(f"{d.get('n_matches','?')} matches | lambda home {d.get('lam_home',0):.2f} "
          f"away {d.get('lam_away',0):.2f}\n")

    print("=" * 96)
    print("VERDICT: does the Wilson 95% CI on win rate clear the break-even implied by price paid?")
    print("=" * 96)
    print(f"{'rule':18}{'n':>6}{'win%':>7}{'BE':>6}{'95% CI':>16}{'ROI':>9}{'$/100 bets':>12}   verdict")
    for rule in sorted(tr, key=lambda r: -len(tr[r])):
        t = tr[rule]
        if len(t) < 20:
            continue
        n, w, be, lo, hi, roi, v = verdict(t)
        per100 = sum(x["pnl_c"] for x in t) / n
        print(f"{rule:18}{n:>6}{w:>7.0%}{be:>6.0%}{f'[{lo:.0%}, {hi:.0%}]':>16}"
              f"{roi:>+9.1%}{per100:>+12.2f}   {v}")

    # focus rule
    focus = next((r for r in tr if r.startswith("CD-UNDER@45")), None)
    if not focus:
        return
    t = tr[focus]
    n, w, be, lo, hi, roi, v = verdict(t)
    print(f"\n{'='*96}\nFOCUS: {focus}   n={n}  win={w:.1%}  BE={be:.1%}  "
          f"CI=[{lo:.1%},{hi:.1%}]  ROI={roi:+.1%}\n{'='*96}")
    print(f"  CI lower minus break-even: {lo-be:+.1%}   "
          f"({'RESOLVED' if lo > be else 'still ambiguous'})")

    for label, keyfn in (("BY LEAGUE", lambda x: x.get("league", "?")),
                         ("BY ANCHOR SOURCE", lambda x: "inferred" if x.get("inferred") else "stamped"),
                         ("BY MONTH", lambda x: x["date"][:7])):
        groups = defaultdict(list)
        for x in t:
            groups[keyfn(x)].append(x)
        if len(groups) <= 1 and label != "BY MONTH":
            continue
        print(f"\n  -- {label} --")
        print(f"     {'group':26}{'n':>5}{'win%':>7}{'BE':>6}{'ROI':>9}")
        for g in sorted(groups, key=lambda g: -len(groups[g])):
            sub = groups[g]
            if len(sub) < 3:
                continue
            gn, gw, gbe, _, _, groi, _ = verdict(sub)
            print(f"     {str(g):26}{gn:>5}{gw:>7.0%}{gbe:>6.0%}{groi:>+9.1%}")

    print("\n  -- sample needed to resolve, holding observed rates --")
    for target in (n, 150, 200, 300, 400, 600):
        lo2, _ = wilson(round(w * target), target)
        print(f"     n={target:>4}: CI low {lo2:.1%} vs BE {be:.1%}  "
              f"-> {'RESOLVED' if lo2 > be else 'ambiguous'}")


if __name__ == "__main__":
    main()
