"""
The decay curve: residual edge as a function of how late you are RELATIVE TO THE
MARKET'S OWN FIRST TICK -- not relative to any external feed's timestamp.

WHY THIS EXISTS
---------------
The first pass at this question (research/mls_lead_change_residual.py) measured entry
off the 1-minute candlestick ask at ESPN's goal timestamp, and concluded the strategy
was dead. That conclusion was WRONG, for a methodological reason worth remembering:

  * ESPN's stamp lags the market's first tick by a median of 31s (40/40 events).
  * `_ask_at()` returns the ask close of the first candle ENDING at/after the target,
    which adds up to another ~60s.
  * The residual decays steeply across exactly that range.

So that measurement was really pricing entry ~90s late while attributing the result to
~31s. Candle granularity is not adequate for a question whose answer moves on a
sub-second-to-one-minute scale. This script uses the raw trade tape instead.

WHAT IT MEASURES
----------------
t0 = the first trade reaching 20% of the eventual move = the fastest existing
participant's reaction. `d` is therefore YOUR LATENESS VERSUS THE FASTEST TRADER
ALREADY IN THIS MARKET -- not versus the on-field event. Beating d=0 requires a feed
faster than the quickest human or bot watching live video.

Entry is modelled as the trade price at t0+d plus a 1c crossing cost (in-match spreads
measured at 1-2c), net of the Kalshi taker fee.

RESULT (2026-08-12, n=40 lead-change goals with >=5c move)
----------------------------------------------------------
    late by    median residual   profitable
      0.0s          +17.37c        38/40 (95%)
      1.0s          +11.76c        37/40 (92%)
      5.0s          +10.82c        33/40 (82%)
     30.0s           +3.30c        26/40 (65%)
     60.0s           +1.88c        23/40 (57%)
    120.0s           -1.20c        15/40 (38%)

Break-even sits between 60s and 120s. ESPN REST polling lands at roughly 31s (stamp)
+ poll interval + execution, i.e. thin-but-positive territory. A feed in the 1-5s range
is worth roughly +10c/contract.

IMPORTANT CAVEATS ON THAT NUMBER
--------------------------------
  * `settled` is the price 300s after the event, i.e. a MARK, not a realized exit.
    Realizing it costs a second crossing + a second fee, so round-trip is ~2-3c worse
    than modelled here. Or you hold to resolution and take the variance.
  * Depth is not modelled. Entry assumes the clip fills at top-of-book immediately
    after a goal, which is exactly when the book is thinnest. Real slippage on size
    will be worse, possibly much worse.
  * d is measured against the fastest participant, so d=0 is not "act instantly" --
    it is "tie the fastest bot already trading this market."

Free data only: Kalshi trades. Never touches the Odds API.

Run:
    python3 research/repricing_decay_curve.py \
        --rows research/findings/mls_lead_change_residual.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from data.kalshi_client import KalshiClient
from kalshi_reaction_latency import _parse_kalshi_ts

DEFAULT_DELAYS = [0, 0.5, 1, 2, 5, 10, 20, 30, 60, 120]
MOVE_START_FRAC = 0.20
MIN_MOVE = 0.05


def collect(rows: list[dict], delays: list[float], cross_cost: float) -> list[dict]:
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

        def price_at(when: datetime) -> float | None:
            best = None
            for t in trades:
                ts = _parse_kalshi_ts(t["created_time"])
                if ts <= when and (best is None or ts > best[0]):
                    best = (ts, float(t["yes_price_dollars"]))
            return best[1] if best else None

        net = {}
        for d in delays:
            p = price_at(t0 + timedelta(seconds=d))
            if p is None:
                continue
            entry = min(0.99, p + cross_cost)
            fee_c = config.KALSHI_TAKER_FEE_RATE_ESTIMATE * entry * (1 - entry) * 100
            net[d] = (settled - entry) * 100 - fee_c

        out.append({
            "match": r["match"], "rule": r["rule"], "move_c": move * 100,
            "espn_lag_s": (ev_ts - t0).total_seconds(), "net": net,
        })
    return out


def report(events: list[dict], delays: list[float], cross_cost: float) -> None:
    if not events:
        print("No qualifying events.")
        return
    print(f"\nDECAY CURVE -- net residual (cents/contract) vs lateness after the market's")
    print(f"own first tick.  n={len(events)} lead-change goals, >={MIN_MOVE*100:.0f}c move.")
    print(f"Entry = trade price + {cross_cost*100:.0f}c crossing cost, net of taker fee.")
    print(f"d is lateness vs the FASTEST EXISTING PARTICIPANT, not vs the on-field event.\n")
    print(f"{'late by':>10}{'n':>5}{'median':>10}{'mean':>10}{'profitable':>14}"
          f"{'A1 med':>9}{'A2 med':>9}")
    for d in delays:
        v = [e["net"][d] for e in events if d in e["net"]]
        if not v:
            continue
        a1 = [e["net"][d] for e in events if d in e["net"] and e["rule"] == "A1"]
        a2 = [e["net"][d] for e in events if d in e["net"] and e["rule"] == "A2"]
        w = sum(1 for x in v if x > 0)
        print(f"{d:>8.1f}s{len(v):>5}{median(v):>+10.2f}{sum(v)/len(v):>+10.2f}"
              f"{f'{w}/{len(v)} ({w/len(v):.0%})':>14}"
              f"{(median(a1) if a1 else 0):>+9.2f}{(median(a2) if a2 else 0):>+9.2f}")

    lag = sorted(e["espn_lag_s"] for e in events)
    n = len(lag)
    print(f"\nESPN stamp minus market's first tick (positive = the MARKET moved first):")
    print(f"   median {median(lag):+.1f}s   p25 {lag[n//4]:+.1f}s   p75 {lag[3*n//4]:+.1f}s")
    print(f"   market moved before ESPN's stamp in {sum(1 for x in lag if x > 0)}/{n} events")

    prev = None
    print("\nBreak-even lateness (median residual crosses zero):")
    for d in delays:
        v = [e["net"][d] for e in events if d in e["net"]]
        if not v:
            continue
        m = median(v)
        if prev is not None and prev[1] > 0 >= m:
            print(f"   between {prev[0]}s ({prev[1]:+.2f}c) and {d}s ({m:+.2f}c)")
        prev = (d, m)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--rows", default="research/findings/mls_lead_change_residual.json",
                   help="JSON produced by research/mls_lead_change_residual.py --out")
    p.add_argument("--delays", type=float, nargs="+", default=DEFAULT_DELAYS)
    p.add_argument("--cross-cost", type=float, default=0.01,
                   help="Assumed cost of crossing the spread, in dollars.")
    p.add_argument("--out", help="Write per-event rows to this JSON path.")
    args = p.parse_args()

    rows = json.load(open(args.rows))["rows"]
    events = collect(rows, args.delays, args.cross_cost)
    report(events, args.delays, args.cross_cost)

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"generated_at": datetime.utcnow().isoformat() + "Z",
                       "cross_cost": args.cross_cost, "events": events}, f, indent=1)
        print(f"\nWrote {len(events)} rows to {args.out}")


if __name__ == "__main__":
    main()
