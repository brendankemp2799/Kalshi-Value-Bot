"""
Can a live in-game order actually be FILLED after an MLS goal, and at what size?

This is the largest unknown left open by research/experiments/2026-08-12-mls-lead-change-momentum.md.
The decay curve there shows a real edge (+10-12c at 1-5s lateness), but it assumes the
clip fills at top-of-book in the seconds after a goal -- exactly when the book is
thinnest and every other participant is hitting it.

WHY TRADED VOLUME, NOT ORDERBOOK DEPTH
--------------------------------------
Kalshi exposes no historical orderbook, and this repo has no orderbook endpoint at all
(`grep -r orderbook` -> zero hits). But realized trades are a sound *lower bound* on
what was takeable: every trade that printed is size someone actually got. If N contracts
printed at or below price P in the window, then at least N were available at P.

It is a lower bound in the honest direction -- resting size that nobody lifted is
invisible here, so true capacity is >= what this reports. It is NOT an upper bound on
what YOU could have taken, because you would have been competing for the same prints.

WHAT IT MEASURES
----------------
t0 = first trade reaching 20% of the eventual move (the fastest participant's reaction),
matching research/repricing_decay_curve.py. For a grid of entry lateness `d`:

  entry_price = trade price at t0+d, plus a 1c crossing cost
  fillable(d) = contracts printing in [t0+d, t0+d+window] at a price <= entry_price

i.e. how much size traded at a price you would have been happy to pay, in the seconds
after you decided. Reported in contracts and in dollars (contracts * price).

Free data only: Kalshi trades. Never touches the Odds API.

Run:
    python3 research/post_goal_depth.py --rows research/findings/mls_lead_change_residual.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.kalshi_client import KalshiClient
from kalshi_reaction_latency import _parse_kalshi_ts

DEFAULT_DELAYS = [1, 5, 30]
DEFAULT_WINDOW = 30
MOVE_START_FRAC = 0.20
MIN_MOVE = 0.05


def collect(rows, delays, window_s, cross_cost):
    kc = KalshiClient()
    out = []
    for r in rows:
        ev_ts = datetime.fromisoformat(r["event_ts"])
        mn = int((ev_ts - timedelta(seconds=900)).timestamp())
        mx = int((ev_ts + timedelta(seconds=900)).timestamp())
        try:
            trades = sorted(kc.fetch_trades(r["ticker"], mn, mx),
                            key=lambda t: _parse_kalshi_ts(t["created_time"]))
        except Exception as e:
            print(f"  skip {r['ticker']}: {e}", file=sys.stderr)
            continue

        base, settled = r["baseline"], r["settled"]
        move = settled - base
        if abs(move) < MIN_MOVE:
            continue

        target = base + MOVE_START_FRAC * move
        t0 = None
        for t in trades:
            ts = _parse_kalshi_ts(t["created_time"])
            if ts < ev_ts - timedelta(seconds=300):
                continue
            p = float(t["yes_price_dollars"])
            if (move > 0 and p >= target) or (move < 0 and p <= target):
                t0 = ts
                break
        if t0 is None:
            continue

        def price_at(when):
            best = None
            for t in trades:
                ts = _parse_kalshi_ts(t["created_time"])
                if ts <= when and (best is None or ts > best[0]):
                    best = (ts, float(t["yes_price_dollars"]))
            return best[1] if best else None

        per_delay = {}
        for d in delays:
            start = t0 + timedelta(seconds=d)
            p = price_at(start)
            if p is None:
                continue
            entry = min(0.99, p + cross_cost)
            contracts = 0.0
            for t in trades:
                ts = _parse_kalshi_ts(t["created_time"])
                if not (start <= ts <= start + timedelta(seconds=window_s)):
                    continue
                # A buyer of YES needs someone selling at or below our limit.
                if float(t["yes_price_dollars"]) <= entry:
                    contracts += float(t.get("count_fp") or 0)
            per_delay[d] = {"entry": entry, "contracts": contracts,
                            "dollars": contracts * entry}

        out.append({"match": r["match"], "rule": r["rule"],
                    "move_c": move * 100, "per_delay": per_delay})
    return out


def report(events, delays, window_s):
    if not events:
        print("No qualifying events.")
        return
    print(f"\nPOST-GOAL FILLABLE SIZE  (n={len(events)} lead-change goals)")
    print(f"Contracts printing at or below our entry price within {window_s}s of entering.")
    print(f"Lower bound on capacity: resting size nobody lifted is invisible here.\n")
    print(f"{'entry lateness':>15}{'median $':>12}{'p25 $':>10}{'p75 $':>10}"
          f"{'min $':>9}{'>= $25':>9}{'>= $100':>9}{'>= $500':>9}")
    for d in delays:
        vals = sorted(e["per_delay"][d]["dollars"] for e in events if d in e["per_delay"])
        if not vals:
            continue
        n = len(vals)
        print(f"{f'{d}s':>15}{median(vals):>12,.0f}{vals[n//4]:>10,.0f}{vals[3*n//4]:>10,.0f}"
              f"{vals[0]:>9,.0f}"
              f"{f'{sum(1 for v in vals if v >= 25)}/{n}':>9}"
              f"{f'{sum(1 for v in vals if v >= 100)}/{n}':>9}"
              f"{f'{sum(1 for v in vals if v >= 500)}/{n}':>9}")

    print(f"\nWorst-case events (smallest fillable size at {delays[0]}s):")
    ranked = sorted((e for e in events if delays[0] in e["per_delay"]),
                    key=lambda e: e["per_delay"][delays[0]]["dollars"])
    for e in ranked[:5]:
        pd = e["per_delay"][delays[0]]
        print(f"   {e['match'][:38]:38} {e['rule']}  move {e['move_c']:+6.1f}c  "
              f"entry {pd['entry']:.2f}  fillable ${pd['dollars']:,.0f} "
              f"({pd['contracts']:,.0f} contracts)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rows", default="research/findings/mls_lead_change_residual.json")
    p.add_argument("--delays", type=float, nargs="+", default=DEFAULT_DELAYS)
    p.add_argument("--window", type=float, default=DEFAULT_WINDOW,
                   help="Seconds after entry during which we could still be filled.")
    p.add_argument("--cross-cost", type=float, default=0.01)
    p.add_argument("--out")
    args = p.parse_args()

    rows = json.load(open(args.rows))["rows"]
    events = collect(rows, args.delays, args.window, args.cross_cost)
    report(events, args.delays, args.window)

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"generated_at": datetime.utcnow().isoformat() + "Z",
                       "window_s": args.window, "events": events}, f, indent=1)
        print(f"\nWrote {len(events)} rows to {args.out}")


if __name__ == "__main__":
    main()
