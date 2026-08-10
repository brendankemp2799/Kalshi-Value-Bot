"""
Backtest: would passive market-making (resting quotes inside the Kalshi spread) have
been profitable on the markets the bot's directional strategy currently rejects for
being too wide to cross (`spread_too_wide`)?

Method:
  1. Run the live matching + consensus pipeline exactly as the bot does
     (match_events + detect_value with scan_log capture), which produces a
     fair-value (de-vigged sportsbook consensus) estimate for every matched
     Kalshi market, including ones rejected for spread width.
  2. For each such market, pull its full historical candlestick series
     (Kalshi's own recorded trade prices — not credit-metered).
  3. Replay it minute-by-minute, calling the REAL execution/market_maker.py::
     evaluate_mm_candidate() at every candle (not a reimplementation of its
     logic) — this is the fix for a real bug found 2026-08-09: the original
     version of this backtest hand-rolled a symmetric, non-inventory-aware
     quoting policy that didn't match what the live code actually does
     (inventory skew, spread-scaled quote width, config-driven clip sizing),
     so it was validating a strategy that wasn't the one that would actually
     run. Calling the live function directly makes that drift impossible.
  4. Mark a fill whenever a real trade printed at or through our quote price
     during that candle, sum simulated P&L net of Kalshi's real maker-fee
     formula, and — new in this pass — record every fill's subsequent price
     path to check for adverse selection (see `adverse_selection_report`).

Simplifying assumptions (stated once, applies throughout):
  - Consensus fair value is held constant per market for the backtest window
    (no re-fetch of historical sportsbook odds) — reasonable for pre-game
    windows, optimistic for markets that were already in-play.
  - A quote is treated as filled if a trade printed at or beyond its price
    during that 1-minute candle, ignoring queue priority (another resting
    order could have been ahead of ours at the same price) — this makes the
    simulated fill rate an upper bound, not a guarantee.
  - Adverse-selection horizons are minutes, not seconds — 1-minute candles are
    the finest resolution Kalshi's candlestick API offers, so the sub-minute
    horizons a live tick-by-tick system could measure aren't reconstructable
    here.

Cost note: matching events costs real Odds API credits (one `fetch_all_sports()`
call, same as a normal scan). Candlestick fetches are NOT credit-metered. Use
`--cache-file` to fetch candidates+candlesticks once and re-run the simulation
against cached data for free while iterating on quoting/backtest logic.

Run: python3 mm_backtest.py [--half-spread-frac 0.5,0.35,0.25] [--min-spread 0.06]
     [--cache-file mm_backtest_cache.json] [--refresh-cache]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from statistics import mean, median

sys.path.insert(0, ".")
import config


def _maker_fee(price: float, contracts: float) -> float:
    """Kalshi maker fee = 25% of the taker fee formula (0.07 * p * (1-p) per contract)."""
    taker_rate = config.KALSHI_TAKER_FEE_RATE_ESTIMATE
    return 0.25 * taker_rate * price * (1.0 - price) * contracts


@dataclass
class SimResult:
    ticker: str
    label: str
    consensus: float
    kalshi_spread: float
    candles: int
    yes_fills: int = 0
    no_fills: int = 0
    fees_paid: float = 0.0
    net_pnl: float = 0.0
    fills: list[dict] = field(default_factory=list)  # for adverse-selection analysis


def _candle_mid(c: dict) -> float | None:
    """Sane (tight-spread) mid price for one candle, or None if the quote looks
    degenerate (e.g. no live market — see the same guard used in the trailing-stop
    backtest earlier this session, which found post-settlement candles can show a
    one-sided empty-book artifact like bid=0/ask=1.00)."""
    bid = (c.get("yes_bid") or {}).get("close_dollars")
    ask = (c.get("yes_ask") or {}).get("close_dollars")
    if bid in (None, "") or ask in (None, ""):
        return None
    bid, ask = float(bid), float(ask)
    if abs(ask - bid) > 0.15:
        return None
    return (bid + ask) / 2.0


def simulate_market(
    ticker: str,
    label: str,
    candles: list[dict],
    consensus: float,
) -> SimResult:
    """
    Replay one market's candlestick history against the real
    execution/market_maker.py::evaluate_mm_candidate() decision function,
    tracking net inventory exactly as the live code would (see
    execution/market_maker.py::_net_inventory_contracts for the live
    equivalent, reconstructed here from the fills generated during replay
    since there's no `positions` table in a backtest).
    """
    from execution.market_maker import evaluate_mm_candidate, MMActionKind

    res = SimResult(ticker=ticker, label=label, consensus=consensus, kalshi_spread=0.0, candles=len(candles))
    net_inventory = 0.0  # net YES-equivalent contracts held so far, same convention as live

    for c in candles:
        price_field = c.get("price") or {}
        vol = float(c.get("volume_fp", 0) or 0)
        if vol <= 0:
            continue  # no trade printed this candle -- nothing could have filled

        trade_low = price_field.get("low_dollars")
        trade_high = price_field.get("high_dollars")
        if trade_low is None or trade_high is None:
            continue
        trade_low, trade_high = float(trade_low), float(trade_high)

        yes_ask = c.get("yes_ask") or {}
        yes_bid = c.get("yes_bid") or {}
        cur_ask = float((yes_ask.get("close_dollars") or 0) or 0)
        cur_bid = float((yes_bid.get("close_dollars") or 0) or 0)
        if cur_ask <= 0 or cur_bid <= 0 or (cur_ask - cur_bid) > 0.5:
            continue  # same degenerate-quote guard as _candle_mid, inlined for the spread value
        spread = round(cur_ask - cur_bid, 4)
        res.kalshi_spread = max(res.kalshi_spread, spread)

        candidate = {"consensus_prob": consensus, "kalshi_spread": spread}
        action = evaluate_mm_candidate(candidate, net_inventory_contracts=net_inventory)
        if action.kind != MMActionKind.QUOTE:
            continue

        ts = c.get("end_period_ts")

        if action.yes_bid_price and trade_low <= action.yes_bid_price:
            yes_count = max(1, math.floor(action.clip_dollars / action.yes_bid_price))
            res.yes_fills += 1
            res.fees_paid += _maker_fee(action.yes_bid_price, yes_count)
            net_inventory += yes_count
            res.fills.append({
                "ts": ts, "side": "yes",
                "price_yes_terms": action.yes_bid_price, "contracts": yes_count,
            })

        if action.no_bid_price:
            synthetic_yes_ask = 1.0 - action.no_bid_price
            if trade_high >= synthetic_yes_ask:
                no_count = max(1, math.floor(action.clip_dollars / action.no_bid_price))
                res.no_fills += 1
                res.fees_paid += _maker_fee(action.no_bid_price, no_count)
                net_inventory -= no_count
                res.fills.append({
                    "ts": ts, "side": "no",
                    "price_yes_terms": synthetic_yes_ask, "contracts": no_count,
                })

    # Cash-flow P&L: a YES fill is a purchase (cash out, inventory up); a NO fill is
    # economically "sell YES at (1 - no_bid_price)" (cash in, inventory down). Leftover
    # net inventory at the end is marked to the last observed sane price (unrealized --
    # not a real exit, same approximation the original version used).
    cash = 0.0
    for f in res.fills:
        if f["side"] == "yes":
            cash -= f["price_yes_terms"] * f["contracts"]
        else:
            cash += f["price_yes_terms"] * f["contracts"]

    last_price = None
    for c in reversed(candles):
        m = _candle_mid(c)
        if m is not None:
            last_price = m
            break
    last_price = last_price if last_price is not None else consensus

    res.net_pnl = cash + net_inventory * last_price - res.fees_paid
    return res


def adverse_selection_report(all_results: list[SimResult], all_candles_by_ticker: dict[str, list[dict]],
                              horizons_min: list[int]) -> None:
    """
    For every simulated fill across every market, look up the price at each horizon
    after the fill and compute the signed favorable/adverse move: positive means price
    moved in our position's favor after we got filled, negative means we were picked
    off right before an adverse move (classic adverse selection).
    """
    moves_by_horizon: dict[int, list[float]] = {h: [] for h in horizons_min}
    total_fills = sum(len(r.fills) for r in all_results)
    if total_fills == 0:
        print("\nNo fills across any simulated market -- nothing to check for adverse selection.")
        return

    for res in all_results:
        candles = all_candles_by_ticker[res.ticker]
        # ts -> mid price, built once per market for fast nearest-neighbor lookup
        ts_price = {c["end_period_ts"]: _candle_mid(c) for c in candles if _candle_mid(c) is not None}
        sorted_ts = sorted(ts_price.keys())

        for f in res.fills:
            fill_ts = f["ts"]
            fill_price = f["price_yes_terms"]
            side = f["side"]
            for h in horizons_min:
                target = fill_ts + h * 60
                # nearest available minute at/after target, within a 3-minute tolerance
                candidate_ts = next((t for t in sorted_ts if t >= target and t <= target + 180), None)
                if candidate_ts is None:
                    continue
                price_then = ts_price[candidate_ts]
                if side == "yes":
                    signed_move = price_then - fill_price
                else:
                    signed_move = fill_price - price_then
                moves_by_horizon[h].append(signed_move)

    print(f"\n{'='*70}\nADVERSE SELECTION CHECK ({total_fills} total fills across {len(all_results)} markets)\n{'='*70}")
    print("Positive = price moved in our favor after the fill. Negative = adverse\n"
          "(we got filled right before the market moved against us).\n")
    print(f"{'horizon':>10} {'n':>6} {'mean':>10} {'median':>10} {'%adverse':>10}")
    for h in horizons_min:
        vals = moves_by_horizon[h]
        if not vals:
            print(f"{h:>8}min {0:>6} {'--':>10} {'--':>10} {'--':>10}")
            continue
        pct_adverse = 100.0 * sum(1 for v in vals if v < 0) / len(vals)
        print(f"{h:>8}min {len(vals):>6} {mean(vals):>+10.4f} {median(vals):>+10.4f} {pct_adverse:>9.1f}%")


def _fetch_candidates(min_spread: float, max_markets: int):
    from core.market_matcher import match_events
    from core.value_detector import detect_value
    from data.kalshi_client import KalshiClient
    from data.odds_fetcher import OddsAPIClient

    print(f"Fetching live odds + Kalshi markets (min_spread candidate filter: {min_spread*100:.0f}c)...")
    odds_client = OddsAPIClient()
    kalshi_client = KalshiClient()

    odds_events = odds_client.fetch_all_sports()
    kalshi_markets = kalshi_client.fetch_sports_markets()
    matched = match_events(odds_events, kalshi_markets)
    print(f"Matched {len(matched)} events.")

    scan_log: list[dict] = []
    detect_value(matched, min_edge=config.MIN_EDGE, scan_log=scan_log)

    candidates = [
        e for e in scan_log
        if e.get("consensus_prob") is not None
        and e.get("kalshi_spread") is not None
        and e["kalshi_spread"] >= min_spread
        and e.get("kalshi_ticker")
    ]
    seen: dict[str, dict] = {}
    for e in candidates:
        seen.setdefault(e["kalshi_ticker"], e)
    return sorted(seen.values(), key=lambda e: -e["kalshi_spread"])[:max_markets]


def _fetch_candles(kalshi_client, ticker: str, lookback_hours: int, period_interval: int):
    series = ticker.split("-")[0]
    end_ts = int(time.time())
    start_ts = end_ts - lookback_hours * 3600
    data = kalshi_client.fetch_candlesticks(series, ticker, start_ts, end_ts, period_interval=period_interval)
    return data.get("candlesticks", [])


def _load_or_build_cache(args) -> dict:
    """
    A single live snapshot only ever sees whatever markets are *currently* matched
    and wide -- often dominated by fixtures days/weeks out with little real trading
    history yet (confirmed 2026-08-09: a live run found 21 wide-spread candidates,
    all for games 6-10 days away, yielding only 5 total fills -- far too few to say
    anything about adverse selection). scan_log doesn't help either; it only retains
    the most recent scan, not accumulated history.

    So the cache is additive across runs, not a one-shot snapshot: `--refresh-cache`
    merges newly-discovered candidates into whatever's already cached (by ticker) and
    re-fetches candlesticks for every ticker already in the cache too, since a market
    that was thin last time may have real in-play history by now that a plain re-fetch
    will pick up. The intended usage is to re-run this with --refresh-cache periodically
    (e.g. from cron, alongside the existing research-layer schedule) so the sample
    actually grows over days/weeks instead of being stuck at one day's snapshot.
    """
    existing = {"candidates": [], "candles_by_ticker": {}}
    if args.cache_file and os.path.exists(args.cache_file):
        with open(args.cache_file) as f:
            existing = json.load(f)

    if not args.refresh_cache:
        if existing["candidates"]:
            print(f"Loading cached candidates+candlesticks from {args.cache_file} "
                  f"(pass --refresh-cache to fetch/merge new live data)...")
            return existing
        args.refresh_cache = True  # no cache yet -- must fetch at least once

    candidates = _fetch_candidates(args.min_spread, args.max_markets)
    print(f"{len(candidates)} unique wide-spread candidate markets found this scan.")

    from data.kalshi_client import KalshiClient
    kalshi_client = KalshiClient()

    existing_by_ticker = {c["kalshi_ticker"]: c for c in existing["candidates"]}
    all_tickers = set(existing_by_ticker) | {c["kalshi_ticker"] for c in candidates}
    new_by_ticker = {c["kalshi_ticker"]: c for c in candidates}

    cache = {"candidates": [], "candles_by_ticker": {}}
    for ticker in all_tickers:
        cand = new_by_ticker.get(ticker) or existing_by_ticker[ticker]
        try:
            candles = _fetch_candles(kalshi_client, ticker, args.lookback_hours, args.period_interval)
        except Exception as e:
            print(f"  skip {ticker}: candlestick fetch failed ({e})")
            # fall back to whatever was cached before, if anything, rather than losing it
            if ticker in existing["candles_by_ticker"]:
                cache["candidates"].append(existing_by_ticker[ticker])
                cache["candles_by_ticker"][ticker] = existing["candles_by_ticker"][ticker]
            continue
        if not candles:
            continue
        cache["candidates"].append({
            "kalshi_ticker": ticker,
            "team_name": cand["team_name"],
            "consensus_prob": cand["consensus_prob"],
        })
        cache["candles_by_ticker"][ticker] = candles

    new_count = len(all_tickers) - len(existing_by_ticker)
    print(f"Merged: {len(existing_by_ticker)} previously cached + {new_count} newly "
          f"discovered = {len(cache['candidates'])} total markets.")

    if args.cache_file:
        with open(args.cache_file, "w") as f:
            json.dump(cache, f)
        print(f"Cached {len(cache['candidates'])} markets to {args.cache_file}.")

    return cache


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-spread", type=str, default="0.06")
    parser.add_argument("--lookback-hours", type=int, default=48)
    parser.add_argument("--max-markets", type=int, default=25)
    parser.add_argument("--period-interval", type=int, default=1)
    parser.add_argument("--cache-file", type=str, default=None,
                         help="Fetch candidates+candlesticks once and reuse across runs "
                              "(saves Odds API credits while iterating).")
    parser.add_argument("--refresh-cache", action="store_true",
                         help="Force a live re-fetch even if --cache-file exists.")
    parser.add_argument("--adverse-horizons", type=str, default="1,5,15,30,60",
                         help="Minutes after each fill to check for adverse selection.")
    args = parser.parse_args()

    min_spread = float(args.min_spread.split(",")[0])
    args.min_spread = min_spread

    cache = _load_or_build_cache(args)
    candidates = cache["candidates"]
    candles_by_ticker = cache["candles_by_ticker"]

    if not candidates:
        print("No candidates available -- try again during active game hours, or check "
              "the cache file if one was passed.")
        return

    total_pnl = 0.0
    total_fills = 0
    total_fees = 0.0
    results: list[SimResult] = []

    for cand in candidates:
        ticker = cand["kalshi_ticker"]
        candles = candles_by_ticker.get(ticker)
        if not candles:
            continue
        r = simulate_market(ticker, cand["team_name"], candles, cand["consensus_prob"])
        results.append(r)
        total_pnl += r.net_pnl
        total_fills += r.yes_fills + r.no_fills
        total_fees += r.fees_paid

        print(f"  {ticker:45s} spread={r.kalshi_spread*100:4.1f}c "
              f"candles={r.candles:4d} fills(Y/N)={r.yes_fills:3d}/{r.no_fills:3d} "
              f"net_pnl=${r.net_pnl:+.2f}")

    print(f"\n  TOTAL across {len(results)} markets: fills={total_fills} "
          f"fees=${total_fees:.2f} net_pnl=${total_pnl:+.2f}")

    horizons = [int(x) for x in args.adverse_horizons.split(",")]
    adverse_selection_report(results, candles_by_ticker, horizons)


if __name__ == "__main__":
    main()
