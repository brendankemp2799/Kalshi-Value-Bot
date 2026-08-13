"""
Phase-0 feasibility measurement for the MLS live lead-change momentum hypothesis
(research/hypotheses/2026-08-12-live-lead-change-momentum.md).

THE QUESTION
------------
If a bot learns of an MLS goal from ESPN's public API and immediately buys the
scoring team's Kalshi moneyline, is there any move left to capture?

THE MEASUREMENT
---------------
For every goal that took the scoring team from level to ahead (the A1/A2 trigger):

    baseline    = last trade 300s BEFORE ESPN's goal wallclock  (pre-event fair value)
    settled     = last trade 300s AFTER  ESPN's goal wallclock  (post-event fair value)
    total_move  = settled - baseline        <- the whole prize the strategy is chasing
    entry(d)    = yes_ask at wallclock + d  <- what we would actually pay
    residual(d) = settled - entry(d) - fee  <- what is actually left for us

`d` is an assumed decision delay. A live bot's true delay is
(ESPN publication lag) + (poll interval) + (execution latency), all of which land
ON TOP of d=0. So **d=0 is a hard upper bound on achievable edge, not a target** --
it assumes the bot acts at the instant of ESPN's timestamp with zero lag anywhere.

THE RESULT (2026-08-12, n=41 lead-change goals, 17 match dates May-Aug 2026)
---------------------------------------------------------------------------
Median total move available: +25c per goal.
Median residual captured at d=0: **-0.28c** (mean -2.20c), profitable 18/41 (44%).
Every assumed delay is negative. See the experiment record for the full write-up:
research/experiments/2026-08-12-mls-lead-change-momentum.md

The move is essentially complete BEFORE ESPN's timestamp exists -- Kalshi traders
watching live video beat ESPN's data feed. Across all goals (not just lead changes,
n=44), a median of 94% of the total price move had already happened by t=0.

COST
----
Free data only: ESPN site API + Kalshi markets/trades/candlesticks. Never touches
the Odds API. Re-running costs nothing but time.

Run:
    python3 research/mls_lead_change_residual.py --dates 2026-08-01 2026-08-08 \
        --out research/findings/mls_lead_change_residual.json
    python3 research/mls_lead_change_residual.py --discover-dates 2026-05-01 2026-08-11
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from core.market_matcher import _team_score
from data.kalshi_client import KalshiClient
from data.mls_events_fetcher import extract_goals, fetch_scoreboard, fetch_summary
from kalshi_reaction_latency import _parse_kalshi_ts

SERIES = "KXMLSGAME"
BASELINE_LOOKBACK_S = 300
SETTLE_HORIZON_S = 300
DEFAULT_DELAYS = [0, 15, 30, 60]


def taker_fee_cents(price: float) -> float:
    """Kalshi's fee is ~rate * p * (1-p) per contract. Charged win or lose, so it
    comes straight off the residual -- see the fee-aware Kelly work in
    core/kelly_calculator.py for why this is never assumed away."""
    return config.KALSHI_TAKER_FEE_RATE_ESTIMATE * price * (1 - price) * 100


def _trade_price_at(trades: list[dict], target: datetime) -> float | None:
    """Last trade at or before target. Trades, not candles: we want the real tape."""
    best = None
    for t in trades:
        ts = _parse_kalshi_ts(t["created_time"])
        if ts <= target and (best is None or ts > best[0]):
            best = (ts, float(t["yes_price_dollars"]))
    return best[1] if best else None


def _ask_at(candles: list[dict], target_ts: float, tolerance_s: int = 180) -> float | None:
    """Ask close of the first candle ending at/after target -- what we'd pay to cross
    the spread. Using ask rather than trade price is the difference between measuring
    price movement and measuring an achievable entry."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts < target_ts or ts > target_ts + tolerance_s:
            continue
        ask = (c.get("yes_ask") or {}).get("close_dollars")
        if ask in (None, ""):
            continue
        if best is None or ts < best[0]:
            best = (ts, float(ask))
    return best[1] if best else None


def discover_dates(start: str, end: str) -> list[str]:
    """MLS plays in weekend clusters with long international-break gaps, so scanning
    every calendar day wastes most of its requests. Emit only dates with completed
    matches."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    while d <= last:
        try:
            evs = fetch_scoreboard(d.isoformat())
            if any(e.get("status", {}).get("type", {}).get("completed") for e in evs):
                out.append(d.isoformat())
        except Exception as e:
            print(f"  scoreboard {d}: {e}", file=sys.stderr)
        d += timedelta(days=1)
    return out


def collect(dates: list[str], delays: list[int], min_fuzzy: int) -> tuple[list[dict], dict]:
    kc = KalshiClient()
    rows: list[dict] = []
    skipped = {"not_lead_change": 0, "no_kalshi_match": 0, "no_price_data": 0}

    for date_str in dates:
        try:
            events = fetch_scoreboard(date_str)
        except Exception as e:
            print(f"scoreboard {date_str}: {e}", file=sys.stderr)
            continue

        for ev in events:
            if not ev.get("status", {}).get("type", {}).get("completed"):
                continue
            comps = ev["competitions"][0]["competitors"]
            home = next(c["team"]["displayName"] for c in comps if c["homeAway"] == "home")
            away = next(c["team"]["displayName"] for c in comps if c["homeAway"] == "away")
            start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))

            try:
                goals = extract_goals(fetch_summary(ev["id"]), ev["id"], home, away)
            except Exception as e:
                print(f"  summary {ev['id']}: {e}", file=sys.stderr)
                continue
            if not goals:
                continue

            mn = int(start.timestamp())
            mx = mn + 8 * 3600
            try:
                settled_mkts = kc.fetch_settled_markets_in_window(SERIES, mn, mx)
            except Exception as e:
                print(f"  kalshi {ev['id']}: {e}", file=sys.stderr)
                continue

            trades_cache: dict[str, list] = {}
            candles_cache: dict[str, list] = {}

            for g in goals:
                # A1/A2 gate: the scoring team must have been LEVEL immediately before,
                # i.e. this goal put them ahead. Excludes lead-extending, equalizing,
                # and still-behind goals.
                if g.home_score is None or g.away_score is None:
                    skipped["not_lead_change"] += 1
                    continue
                if g.team == home:
                    mine, theirs = g.home_score, g.away_score
                elif g.team == away:
                    mine, theirs = g.away_score, g.home_score
                else:
                    skipped["not_lead_change"] += 1
                    continue
                if mine - 1 != theirs:
                    skipped["not_lead_change"] += 1
                    continue

                best, best_score = None, 0
                for m in settled_mkts:
                    s = _team_score(g.team, m.get("yes_sub_title", ""))
                    if s >= min_fuzzy and s > best_score:
                        best, best_score = m, s
                if best is None:
                    skipped["no_kalshi_match"] += 1
                    continue

                ticker = best["ticker"]
                if ticker not in trades_cache:
                    try:
                        trades_cache[ticker] = kc.fetch_trades(ticker, mn - 600, mx)
                        candles_cache[ticker] = kc.fetch_candlesticks(
                            SERIES, ticker, mn - 600, mx, period_interval=1
                        ).get("candlesticks", [])
                    except Exception as e:
                        print(f"  trades {ticker}: {e}", file=sys.stderr)
                        trades_cache[ticker], candles_cache[ticker] = [], []

                baseline = _trade_price_at(
                    trades_cache[ticker], g.event_ts - timedelta(seconds=BASELINE_LOOKBACK_S)
                )
                settled_p = _trade_price_at(
                    trades_cache[ticker], g.event_ts + timedelta(seconds=SETTLE_HORIZON_S)
                )
                if baseline is None or settled_p is None:
                    skipped["no_price_data"] += 1
                    continue

                rows.append({
                    "date": date_str,
                    "match": f"{away} @ {home}",
                    "scorer": g.team,
                    "clock": g.clock_display,
                    "scoreline": f"{g.away_score}-{g.home_score}",
                    "ticker": ticker,
                    "market_result": best.get("result"),
                    "event_ts": g.event_ts.isoformat(),
                    "rule": "A1" if baseline > 0.5 else "A2",
                    "baseline": baseline,
                    "settled": settled_p,
                    "total_move_c": round((settled_p - baseline) * 100, 2),
                    "entry_ask": {
                        str(d): _ask_at(candles_cache[ticker], g.event_ts.timestamp() + d)
                        for d in delays
                    },
                })

    return rows, skipped


def report(rows: list[dict], skipped: dict, delays: list[int]) -> None:
    if not rows:
        print("No qualifying lead-change goals found.")
        return

    print(f"\nA1/A2 LEAD-CHANGE GOALS (level -> scoring team ahead), n={len(rows)}")
    print(f"skipped: {skipped}\n")
    hdr = "".join(f"{'ask+' + str(d):>9}" for d in delays)
    print(f"{'match':30}{'scorer':15}{'clk':8}{'rule':5}{'base':>6}{'settl':>7}{'move':>7}{hdr}")
    for r in rows:
        asks = "".join(
            f"{r['entry_ask'][str(d)]:>9.2f}" if r["entry_ask"][str(d)] is not None
            else f"{'--':>9}" for d in delays
        )
        print(f"{r['match'][:30]:30}{r['scorer'][:15]:15}{r['clock'][:8]:8}{r['rule']:5}"
              f"{r['baseline']:>6.2f}{r['settled']:>7.2f}{r['total_move_c']:>+7.1f}{asks}")

    moves = sorted(r["total_move_c"] for r in rows)
    n = len(moves)
    print(f"\n{'='*80}\nRESIDUAL EDGE AFTER ENTRY (cents/contract, net of taker fee)\n{'='*80}")
    print("A live bot's real delay = ESPN publication lag + poll interval + execution.")
    print("d=0 assumes ZERO of all three -- a hard upper bound, not achievable.\n")
    print(f"Total move available per goal: median {median(moves):+.1f}c  "
          f"(p25 {moves[n//4]:+.1f}c, p75 {moves[3*n//4]:+.1f}c)\n")

    def bucket(sub: list[dict], label: str) -> None:
        print(f"  -- {label}, n={len(sub)} --")
        for d in delays:
            net = []
            for r in sub:
                ep = r["entry_ask"][str(d)]
                if ep is None or not (0 < ep < 1):
                    continue
                net.append((r["settled"] - ep) * 100 - taker_fee_cents(ep))
            if not net:
                print(f"     d={d:>3}s: no usable entries")
                continue
            net.sort()
            wins = sum(1 for x in net if x > 0)
            print(f"     d={d:>3}s  n={len(net):3}  median {median(net):+6.2f}c   "
                  f"mean {sum(net)/len(net):+6.2f}c   profitable {wins}/{len(net)} "
                  f"({wins/len(net):.0%})")

    bucket(rows, "ALL LEAD-CHANGE GOALS")
    print()
    bucket([r for r in rows if r["rule"] == "A1"], "A1 FAVORITE (baseline > 0.50)")
    print()
    bucket([r for r in rows if r["rule"] == "A2"], "A2 UNDERDOG (baseline < 0.50)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dates", nargs="+", help="Match dates YYYY-MM-DD.")
    p.add_argument("--discover-dates", nargs=2, metavar=("START", "END"),
                   help="Print dates with completed MLS matches in range, then exit.")
    p.add_argument("--delays", type=int, nargs="+", default=DEFAULT_DELAYS,
                   help="Assumed decision delays in seconds after ESPN's wallclock.")
    p.add_argument("--min-fuzzy-score", type=int, default=config.FUZZY_MATCH_THRESHOLD)
    p.add_argument("--out", help="Write per-goal rows to this JSON path. Results that "
                                 "only ever reach stdout get lost -- this study's first "
                                 "run was lost exactly that way.")
    args = p.parse_args()

    if args.discover_dates:
        print(" ".join(discover_dates(*args.discover_dates)))
        return
    if not args.dates:
        p.error("--dates is required (or use --discover-dates)")

    rows, skipped = collect(args.dates, args.delays, args.min_fuzzy_score)
    report(rows, skipped, args.delays)

    if args.out:
        with open(args.out, "w") as f:
            json.dump({
                "generated_at": datetime.utcnow().isoformat() + "Z",
                "dates": args.dates,
                "delays": args.delays,
                "skipped": skipped,
                "rows": rows,
            }, f, indent=1)
        print(f"\nWrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
