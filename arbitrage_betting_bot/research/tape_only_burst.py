"""
Can a repricing burst be traded using ONLY Kalshi's own trade tape -- no sports feed?

MOTIVATION
----------
research/experiments/2026-08-12-mls-lead-change-momentum.md found a real edge after
MLS goals, but it is latency-gated and the economics do not support paying for a fast
sports feed at a ~$165 bankroll. It also found the repricing has two phases: 20%->50%
of the move in a median 0.9s, then 50%->90% over a median ~55s.

That second phase is the opening. If the slow grind is tradeable, you do not need to
know a goal happened -- you can detect the burst from Kalshi's tape (free, and Kalshi
offers a WebSocket market-data feed) and ride the remainder. No sports data feed, no
subscription, no race against people watching video.

WHAT MAKES THIS TEST DIFFERENT FROM THE EARLIER ONE
---------------------------------------------------
1. CAUSAL. The earlier study defined the move's start as "first trade reaching 20% of
   the eventual move" -- that is look-ahead; a live bot cannot know the eventual move.
   Here the detector sees only trades at or before the current moment.

2. COUNTS FALSE POSITIVES. The earlier study conditioned on "a goal happened," which a
   tape-only strategy cannot do. This one scans every market continuously, so bursts
   that were noise, reversals, or non-goal events are all included -- and they are the
   main way this idea can fail.

3. REALIZED P&L, NOT A MARK. Reports settlement outcome (market `result`) as the
   primary number, since holding to resolution is what a small account would actually
   do. The +300s mark is reported alongside for comparability with the earlier work.

4. TESTS BOTH DIRECTIONS. Buying the side that just moved UP (momentum) assumes
   continuation. Buying the side that moved DOWN (fade) assumes overreaction. Only
   measurement distinguishes them, so both are reported.

DETECTOR
--------
Rolling baseline = last trade at or before (now - baseline_window). Fire when the
current trade differs from that baseline by >= threshold, then enter `latency` seconds
later at the prevailing price plus a crossing cost. Per-ticker cooldown prevents one
move from generating a burst of overlapping signals.

Free data only: Kalshi settled markets + trades. Never touches the Odds API.

Run:
    python3 research/tape_only_burst.py --dates 2026-08-01 2026-08-08 \
        --cache /tmp/tape_cache.json --out research/findings/tape_only_burst.json
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
from data.mls_events_fetcher import fetch_scoreboard
from kalshi_reaction_latency import _parse_kalshi_ts

SERIES = "KXMLSGAME"


def taker_fee_cents(price: float) -> float:
    return config.KALSHI_TAKER_FEE_RATE_ESTIMATE * price * (1 - price) * 100


def load_tape(dates, cache_path, kc):
    """Fetch (and cache) every settled MLS market's trade tape for the given dates.
    Finished games are immutable, so a cached entry is never re-fetched."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)

    for date_str in dates:
        try:
            events = fetch_scoreboard(date_str)
        except Exception as e:
            print(f"scoreboard {date_str}: {e}", file=sys.stderr)
            continue
        for ev in events:
            if not ev.get("status", {}).get("type", {}).get("completed"):
                continue
            start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            mn, mx = int(start.timestamp()), int(start.timestamp()) + 4 * 3600
            try:
                mkts = kc.fetch_settled_markets_in_window(SERIES, mn, int(start.timestamp()) + 8 * 3600)
            except Exception as e:
                print(f"  markets {ev['id']}: {e}", file=sys.stderr)
                continue
            for m in mkts:
                tk = m["ticker"]
                if tk in cache:
                    continue
                try:
                    trades = kc.fetch_trades(tk, mn - 600, mx)
                except Exception as e:
                    print(f"  trades {tk}: {e}", file=sys.stderr)
                    continue
                cache[tk] = {
                    "result": m.get("result"),
                    "sub_title": m.get("yes_sub_title"),
                    "date": date_str,
                    "trades": [
                        {"t": t["created_time"], "p": float(t["yes_price_dollars"]),
                         "c": float(t.get("count_fp") or 0)}
                        for t in trades
                    ],
                }
                print(f"  cached {tk}: {len(cache[tk]['trades'])} trades", file=sys.stderr)

    if cache_path:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
    return cache


def scan(cache, baseline_window, threshold, latency, cooldown, horizon,
         cross_cost, direction):
    """Run the causal detector over every cached tape. `direction` is 'momentum'
    (buy the side that moved in the burst's direction) or 'fade' (buy against it)."""
    signals = []
    for ticker, entry in cache.items():
        trades = sorted(entry["trades"], key=lambda x: x["t"])
        if len(trades) < 20:
            continue
        parsed = [(_parse_kalshi_ts(t["t"]), t["p"]) for t in trades]
        result = entry.get("result")
        last_fire = None

        for i, (ts, price) in enumerate(parsed):
            if last_fire is not None and (ts - last_fire).total_seconds() < cooldown:
                continue
            # Causal baseline: last trade at or before (ts - baseline_window).
            cutoff = ts - timedelta(seconds=baseline_window)
            base = None
            for j in range(i, -1, -1):
                if parsed[j][0] <= cutoff:
                    base = parsed[j][1]
                    break
            if base is None:
                continue
            delta = price - base
            if abs(delta) < threshold:
                continue

            last_fire = ts
            # Entry `latency` seconds after detection, at the then-prevailing price.
            entry_ts = ts + timedelta(seconds=latency)
            entry_px = None
            for t2, p2 in parsed:
                if t2 <= entry_ts:
                    entry_px = p2
                else:
                    break
            if entry_px is None:
                continue

            up = delta > 0
            buy_yes = up if direction == "momentum" else (not up)
            # Buying NO at price q is buying the complement: cost = 1 - yes_price.
            cost = (entry_px if buy_yes else 1 - entry_px) + cross_cost
            if not (0.02 < cost < 0.98):
                continue

            mark = None
            for t2, p2 in parsed:
                if t2 <= entry_ts + timedelta(seconds=horizon):
                    mark = p2
                else:
                    break
            mark_val = (mark if buy_yes else 1 - mark) if mark is not None else None

            won = None
            if result in ("yes", "no"):
                won = (result == "yes") if buy_yes else (result == "no")

            fee = taker_fee_cents(cost)
            signals.append({
                "ticker": ticker, "date": entry.get("date"),
                "sub_title": entry.get("sub_title"),
                "ts": ts.isoformat(), "delta_c": delta * 100,
                "cost": cost,
                "mark_pnl_c": (mark_val - cost) * 100 - fee if mark_val is not None else None,
                "settle_pnl_c": ((100 if won else 0) - cost * 100 - fee) if won is not None else None,
                "won": won,
            })
    return signals


def report(signals, label):
    n = len(signals)
    print(f"\n{'='*74}\n{label}   n={n} signals\n{'='*74}")
    if not n:
        print("  no signals")
        return
    for key, name in (("settle_pnl_c", "REALIZED (held to settlement)"),
                      ("mark_pnl_c", f"MARK-TO-MARKET (+horizon)")):
        v = sorted(s[key] for s in signals if s[key] is not None)
        if not v:
            continue
        wins = sum(1 for x in v if x > 0)
        tot = sum(v)
        # ROI per signal = pnl_cents / cost_cents
        rois = [s[key] / (s["cost"] * 100) for s in signals if s[key] is not None]
        print(f"  {name}:  n={len(v)}")
        print(f"     median {median(v):+7.2f}c   mean {tot/len(v):+7.2f}c   "
              f"total {tot/100:+8.2f}$/contract   profitable {wins}/{len(v)} ({wins/len(v):.0%})")
        print(f"     mean ROI per signal: {sum(rois)/len(rois):+.2%}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dates", nargs="+", required=True)
    p.add_argument("--cache")
    p.add_argument("--baseline-window", type=float, default=60.0)
    p.add_argument("--threshold", type=float, default=0.04)
    p.add_argument("--latency", type=float, default=2.0)
    p.add_argument("--cooldown", type=float, default=300.0)
    p.add_argument("--horizon", type=float, default=300.0)
    p.add_argument("--cross-cost", type=float, default=0.01)
    p.add_argument("--out")
    args = p.parse_args()

    kc = KalshiClient()
    cache = load_tape(args.dates, args.cache, kc)
    print(f"\nTapes: {len(cache)} markets, "
          f"{sum(len(v['trades']) for v in cache.values()):,} trades")
    print(f"Detector: fire when price moves >={args.threshold*100:.0f}c vs "
          f"{args.baseline_window:.0f}s ago; enter {args.latency:.0f}s later "
          f"(+{args.cross_cost*100:.0f}c cross); cooldown {args.cooldown:.0f}s")

    out = {}
    for direction in ("momentum", "fade"):
        sig = scan(cache, args.baseline_window, args.threshold, args.latency,
                   args.cooldown, args.horizon, args.cross_cost, direction)
        report(sig, direction.upper())
        out[direction] = sig

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"generated_at": datetime.utcnow().isoformat() + "Z",
                       "params": vars(args), "signals": out}, f, indent=1)
        print(f"\nWrote to {args.out}")


if __name__ == "__main__":
    main()
