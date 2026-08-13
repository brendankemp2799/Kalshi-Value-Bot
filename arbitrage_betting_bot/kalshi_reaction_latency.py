"""
Reaction-latency study: how long after a real-world scoring event (MLB home runs,
MLS goals) does the corresponding Kalshi market price actually move? Answers the
question of whether sub-second/sub-minute execution speed is worth building for a
"live betting" feature, before any of that gets built. Independent of the live
trading bot -- reads no local DB, only free/uncredited external history, and can
scan any historical games regardless of whether the bot ever traded them.

Method:
  1. For each game in the date range, pull real event timestamps:
       - MLB: statsapi.mlb.com (free, no auth) -- home runs, timestamped to the
         pitch that was actually hit (data/mlb_events_fetcher.py::_hit_pitch_ts).
       - MLS: ESPN's site API (free, no auth, must use site.web.api.espn.com --
         site.api.espn.com is edge-blocked) -- goals, timestamped via ESPN's own
         `wallclock` field (real UTC, confirmed live 2026-08-11, not derived from
         match-minute -- stoppage time makes minute-based estimates unreliable).
  2. Find the scoring team's own Kalshi moneyline market (KXMLBGAME / KXMLSGAME)
     by searching settled markets in a close_ts window around the scheduled start,
     then fuzzy-matching team name -- reusing core/market_matcher.py::_team_score
     rather than reimplementing fuzzy matching.
  3. Pull that market's real trade-by-trade history (Kalshi's /markets/trades --
     sub-second timestamps, NOT the 1-minute candlestick floor this repo's live
     trading code is normally limited to) around the event.
  4. Measure: time from the event to the first trade that both crosses a price-move
     threshold AND persists a few minutes later (checked via the existing
     fetch_candlesticks -- same fixed-horizon persistence idea as
     mm_backtest.py::adverse_selection_report), plus a magnitude-normalized
     time-to-50%-of-eventual-move metric that still produces a number for small moves.

Simplifying assumptions:
  - Matching the scoring team's OWN market means "did YES price rise" is always the
    right question -- no sign-flipping logic needed. Moneyline only (KXMLBGAME /
    KXMLSGAME); totals/spreads also react but aren't matched here.
  - The reaction window is capped at the game's next scoring play (MLB: any play
    with about.isScoringPlay=True, not just home runs; MLS: any other goal) so a
    later event can't contaminate an earlier one's "settled" reference price.
  - Garbage-time/blowout events that barely move the market are never filtered --
    they're bucketed by eventual move size (High/Medium/Low) so they're visible
    without polluting the headline latency numbers.
  - All four measurement constants (--move-threshold-cents, --reaction-window-minutes,
    --persistence-horizon-minutes, --baseline-lookback-minutes) are starting defaults,
    not fixed conclusions -- both APIs are free, so re-running with different values
    costs nothing but time.

Cost note: MLB's Stats API, ESPN's site API, and Kalshi's market/trade/candlestick
endpoints are all free and not credit-metered (this script never touches the Odds
API). Use --cache-file to fetch each game's events + trades + candles once; a
finished game's data is immutable, so --refresh-cache only fetches new dates/games
or heals games that were cached unfinished.

Run: python3 kalshi_reaction_latency.py --sport both --date-start 2026-07-15 \
     --date-end 2026-08-10 --max-games 200 --cache-file reaction_latency_cache.json \
     --refresh-cache
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

sys.path.insert(0, ".")
import config

_SERIES_TICKER = {"mlb": "KXMLBGAME", "mls": "KXMLSGAME"}


def _parse_kalshi_ts(s: str) -> datetime:
    """Kalshi trade timestamps have a variable number of fractional-second digits
    (observed: both 5 and 6) -- Python's fromisoformat before 3.11 requires exactly
    3 or 6, so normalize rather than assume."""
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*?)(\.\d+)?([+-]\d{2}:\d{2})$", s)
    if m and m.group(2):
        frac = (m.group(2)[1:] + "000000")[:6]
        s = f"{m.group(1)}.{frac}{m.group(3)}"
    return datetime.fromisoformat(s)


def _trade_ts(t: dict) -> datetime:
    return _parse_kalshi_ts(t["created_time"])


def _trade_price(t: dict) -> float:
    return float(t["yes_price_dollars"])


def _candle_trade_close_near(candles: list[dict], target_ts: float, tolerance_s: int = 180) -> float | None:
    """Trade-price close of the candle nearest at-or-after target_ts, within
    tolerance -- same nearest-neighbor-within-tolerance idea as
    mm_backtest.py::adverse_selection_report's ts_price lookup."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts < target_ts or ts > target_ts + tolerance_s:
            continue
        price = (c.get("price") or {}).get("close_dollars")
        if price in (None, ""):
            continue
        if best is None or ts < best[0]:
            best = (ts, float(price))
    return best[1] if best else None


@dataclass
class ScoringEvent:
    """Sport-agnostic shape every event is normalized into before measurement --
    MLB home runs and MLS goals both reduce to this."""
    sport: str
    game_id: str
    team: str
    event_ts: datetime
    context: str        # e.g. "B3" (MLB half+inning) or "45'+1'" (MLS clock)
    scoreline: str
    timestamp_confidence: str


@dataclass
class ReactionResult:
    sport: str
    game_id: str
    date: str
    matchup: str
    context: str
    team: str
    scoreline: str
    ticker: str
    timestamp_confidence: str
    status: str  # OK | NO_REACTION | NO_LIQUIDITY | NO_BASELINE
    baseline: float | None = None
    eventual_move_c: float | None = None
    time_to_first_move_s: float | None = None
    time_to_50pct_s: float | None = None


def _leverage_tier(eventual_move_c: float | None) -> str:
    if eventual_move_c is None:
        return "?"
    m = abs(eventual_move_c)
    if m > 10:
        return "High"
    if m >= 3:
        return "Medium"
    return "Low"


def measure_reaction(
    ev: ScoringEvent, matchup: str, ticker: str, trades: list[dict], candles: list[dict],
    window_end: datetime, args,
) -> ReactionResult:
    base = dict(
        sport=ev.sport, game_id=ev.game_id, date=ev.event_ts.date().isoformat(), matchup=matchup,
        context=ev.context, team=ev.team, scoreline=ev.scoreline, ticker=ticker,
        timestamp_confidence=ev.timestamp_confidence,
    )

    lookback_cutoff = ev.event_ts - timedelta(minutes=args.baseline_lookback_minutes)
    before = [t for t in trades if lookback_cutoff <= _trade_ts(t) < ev.event_ts]
    if not before:
        return ReactionResult(status="NO_BASELINE", **base)
    baseline = _trade_price(max(before, key=_trade_ts))

    after = sorted(
        [t for t in trades if ev.event_ts < _trade_ts(t) <= window_end],
        key=_trade_ts,
    )
    if not after:
        return ReactionResult(status="NO_LIQUIDITY", baseline=baseline, **base)

    window_end_ts = window_end.timestamp()
    eventual = _candle_trade_close_near(candles, window_end_ts)
    if eventual is None:
        eventual = _trade_price(after[-1])
    eventual_move_c = round((eventual - baseline) * 100, 2)

    threshold = args.move_threshold_cents / 100.0
    persistence_target = min(
        ev.event_ts + timedelta(minutes=args.persistence_horizon_minutes), window_end
    )
    persistence_price = _candle_trade_close_near(candles, persistence_target.timestamp())

    time_to_first_move_s = None
    for t in after:
        if _trade_price(t) < baseline + threshold:
            continue
        if persistence_price is not None and persistence_price < baseline + threshold / 2:
            continue  # crossed but didn't stick -- keep looking for one that does
        time_to_first_move_s = (_trade_ts(t) - ev.event_ts).total_seconds()
        break

    time_to_50pct_s = None
    if eventual_move_c > 0:
        half_target = baseline + 0.5 * (eventual_move_c / 100.0)
        for t in after:
            if _trade_price(t) >= half_target:
                time_to_50pct_s = (_trade_ts(t) - ev.event_ts).total_seconds()
                break

    status = "OK" if time_to_first_move_s is not None else "NO_REACTION"
    return ReactionResult(
        status=status, baseline=baseline, eventual_move_c=eventual_move_c,
        time_to_first_move_s=time_to_first_move_s, time_to_50pct_s=time_to_50pct_s,
        **base,
    )


def _fetch_kalshi_side(kalshi_client, sport: str, teams_needed: set[str],
                        game_start: datetime, args) -> tuple[dict, dict, dict]:
    """Shared across both sports: find each team's own Kalshi market via a
    close_ts window search + fuzzy match, then pull its trades and candles for
    the whole game window. Returns (kalshi_match, trades_by_ticker, candles_by_ticker)."""
    from core.market_matcher import _team_score

    min_close_ts = int(game_start.timestamp())
    max_close_ts = min_close_ts + 8 * 3600
    settled = kalshi_client.fetch_settled_markets_in_window(
        _SERIES_TICKER[sport], min_close_ts, max_close_ts
    )

    window_start_ts = min_close_ts - 600
    window_end_ts = max_close_ts

    kalshi_match, trades_by_ticker, candles_by_ticker = {}, {}, {}
    for team in teams_needed:
        best, best_score = None, 0
        for m in settled:
            score = _team_score(team, m.get("yes_sub_title", ""))
            if score >= args.min_fuzzy_score and score > best_score:
                best, best_score = m, score
        if best is None:
            kalshi_match[team] = None
            continue
        ticker = best["ticker"]
        kalshi_match[team] = {"ticker": ticker, "score": best_score}

        trades_by_ticker[ticker] = kalshi_client.fetch_trades(ticker, window_start_ts, window_end_ts)
        series = ticker.split("-")[0]
        candle_data = kalshi_client.fetch_candlesticks(
            series, ticker, window_start_ts, window_end_ts, period_interval=1
        )
        candles_by_ticker[ticker] = candle_data.get("candlesticks", [])

    return kalshi_match, trades_by_ticker, candles_by_ticker


def _process_mlb_game(kalshi_client, game: dict, args) -> dict:
    from data.mlb_events_fetcher import fetch_live_feed, extract_home_runs

    game_pk = game["gamePk"]
    final = game.get("status", {}).get("detailedState", "") == "Final"
    home_team = game["teams"]["home"]["team"]["name"]
    away_team = game["teams"]["away"]["team"]["name"]
    game_start = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00"))

    entry = {
        "sport": "mlb", "date": game_start.date().isoformat(), "home_team": home_team,
        "away_team": away_team, "final": final, "scoring_events": [], "capping_ts": [],
        "kalshi_match": {}, "trades_by_ticker": {}, "candles_by_ticker": {},
    }
    if not final:
        return entry

    feed = fetch_live_feed(game_pk)
    home_runs = extract_home_runs(feed)

    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    capping = []
    for p in plays:
        about = p.get("about", {})
        if about.get("isScoringPlay") and about.get("startTime"):
            capping.append(datetime.fromisoformat(about["startTime"].replace("Z", "+00:00")).isoformat())
    entry["capping_ts"] = sorted(capping)

    entry["scoring_events"] = [
        {
            "team": hr.team, "event_ts": hr.event_ts.isoformat(),
            "context": f"{'T' if hr.half_inning == 'top' else 'B'}{hr.inning}",
            "scoreline": f"{hr.away_score}-{hr.home_score}",
            "home_score_after": hr.home_score, "away_score_after": hr.away_score,
            "timestamp_confidence": hr.timestamp_confidence,
        }
        for hr in home_runs
    ]

    if not home_runs:
        return entry

    teams_needed = {hr.team for hr in home_runs}
    entry["kalshi_match"], entry["trades_by_ticker"], entry["candles_by_ticker"] = \
        _fetch_kalshi_side(kalshi_client, "mlb", teams_needed, game_start, args)
    return entry


def _process_mls_game(kalshi_client, event: dict, args) -> dict:
    from data.mls_events_fetcher import fetch_summary, extract_goals

    event_id = event["id"]
    final = event.get("status", {}).get("type", {}).get("completed", False)
    game_start = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    home_team = away_team = None
    for c in event["competitions"][0]["competitors"]:
        if c["homeAway"] == "home":
            home_team = c["team"]["displayName"]
        else:
            away_team = c["team"]["displayName"]

    entry = {
        "sport": "mls", "date": game_start.date().isoformat(), "home_team": home_team,
        "away_team": away_team, "final": bool(final), "scoring_events": [], "capping_ts": [],
        "kalshi_match": {}, "trades_by_ticker": {}, "candles_by_ticker": {},
    }
    if not final:
        return entry

    summary = fetch_summary(event_id)
    goals = extract_goals(summary, event_id, home_team, away_team)

    entry["capping_ts"] = sorted(g.event_ts.isoformat() for g in goals)
    entry["scoring_events"] = [
        {
            "team": g.team, "event_ts": g.event_ts.isoformat(), "context": g.clock_display,
            "scoreline": g.description.split(".")[0].replace("Goal! ", "").replace("Goal - Header! ", ""),
            "home_score_after": g.home_score, "away_score_after": g.away_score,
            "timestamp_confidence": g.timestamp_confidence,
        }
        for g in goals
    ]

    if not goals:
        return entry

    teams_needed = {g.team for g in goals}
    entry["kalshi_match"], entry["trades_by_ticker"], entry["candles_by_ticker"] = \
        _fetch_kalshi_side(kalshi_client, "mls", teams_needed, game_start, args)
    return entry


def _load_cache(path: str | None) -> dict:
    if path and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"games": {}}


def _save_cache(path: str, cache: dict) -> None:
    with open(path, "w") as f:
        json.dump(cache, f)


def _iter_dates(start: str, end: str):
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while d <= last:
        yield d.isoformat()
        d += timedelta(days=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["mlb", "mls", "both"], default="both")
    parser.add_argument("--date-start", type=str,
                         default=(datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    parser.add_argument("--date-end", type=str, default=None,
                         help="Defaults to --date-start (single day).")
    parser.add_argument("--max-games", type=int, default=15,
                         help="Cap per sport, not total.")
    parser.add_argument("--reaction-window-minutes", type=int, default=20)
    parser.add_argument("--persistence-horizon-minutes", type=int, default=5)
    parser.add_argument("--move-threshold-cents", type=float, default=3.0)
    parser.add_argument("--baseline-lookback-minutes", type=int, default=30)
    parser.add_argument("--min-fuzzy-score", type=int, default=config.FUZZY_MATCH_THRESHOLD)
    parser.add_argument("--cache-file", type=str, default=None)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--lead-only", action="store_true",
                         help="Only measure goals/HRs that put the scoring team into "
                              "the lead (score was tied immediately before). Excludes "
                              "lead-extending, equalizing, and still-behind scores.")
    args = parser.parse_args()
    if args.date_end is None:
        args.date_end = args.date_start
    sports = ["mlb", "mls"] if args.sport == "both" else [args.sport]

    cache = _load_cache(args.cache_file)
    games_cache = cache["games"]

    if not args.cache_file or args.refresh_cache or not games_cache:
        from data.kalshi_client import KalshiClient
        kalshi_client = KalshiClient()

        for sport in sports:
            if sport == "mlb":
                from data.mlb_events_fetcher import fetch_schedule as fetch_games
            else:
                from data.mls_events_fetcher import fetch_scoreboard as fetch_games

            processed = 0
            for date_str in _iter_dates(args.date_start, args.date_end):
                if processed >= args.max_games:
                    break
                for game in fetch_games(date_str):
                    if processed >= args.max_games:
                        break
                    raw_id = str(game["gamePk"] if sport == "mlb" else game["id"])
                    cache_key = f"{sport}:{raw_id}"
                    cached_entry = games_cache.get(cache_key)
                    if cached_entry and cached_entry.get("final"):
                        processed += 1
                        continue  # already finished and immutable -- skip re-fetch
                    print(f"Fetching {sport} game {raw_id} ({date_str})...")
                    try:
                        if sport == "mlb":
                            games_cache[cache_key] = _process_mlb_game(kalshi_client, game, args)
                        else:
                            games_cache[cache_key] = _process_mls_game(kalshi_client, game, args)
                    except Exception as e:
                        print(f"  skip {raw_id}: {e}")
                        continue
                    processed += 1
                    time.sleep(0.2)

        if args.cache_file:
            cache["games"] = games_cache
            _save_cache(args.cache_file, cache)
            print(f"Cached {len(games_cache)} games to {args.cache_file}.")
    else:
        print(f"Loading cached games from {args.cache_file} (pass --refresh-cache for new data)...")

    results: list[ReactionResult] = []
    no_match_games: list[str] = []
    excluded_not_lead = 0

    for cache_key, entry in games_cache.items():
        sport = entry.get("sport", cache_key.split(":")[0])
        if sport not in sports or not entry.get("final") or not entry.get("scoring_events"):
            continue
        matchup = f"{entry['away_team']} @ {entry['home_team']}"
        capping_ts = [datetime.fromisoformat(t) for t in entry.get("capping_ts", [])]
        game_id = cache_key.split(":", 1)[1]

        for sev in entry["scoring_events"]:
            sev = dict(sev)

            if args.lead_only:
                home_after = sev.get("home_score_after")
                away_after = sev.get("away_score_after")
                if home_after is None or away_after is None:
                    excluded_not_lead += 1
                    continue
                if sev["team"] == entry["home_team"]:
                    scorer_after, opp_after = home_after, away_after
                elif sev["team"] == entry["away_team"]:
                    scorer_after, opp_after = away_after, home_after
                else:
                    excluded_not_lead += 1
                    continue
                if scorer_after - 1 != opp_after:  # not tied immediately before this goal
                    excluded_not_lead += 1
                    continue

            event_ts = datetime.fromisoformat(sev["event_ts"])
            ev = ScoringEvent(
                sport=sport, game_id=game_id, team=sev["team"], event_ts=event_ts,
                context=sev["context"], scoreline=sev["scoreline"],
                timestamp_confidence=sev["timestamp_confidence"],
            )

            match = entry["kalshi_match"].get(ev.team)
            if not match:
                no_match_games.append(f"[{sport}] {entry['date']} {matchup} ({ev.team})")
                continue
            ticker = match["ticker"]
            trades = entry["trades_by_ticker"].get(ticker, [])
            candles = entry["candles_by_ticker"].get(ticker, [])

            later = [t for t in capping_ts if t > ev.event_ts]
            next_scoring_ts = min(later) if later else None
            window_end = ev.event_ts + timedelta(minutes=args.reaction_window_minutes)
            if next_scoring_ts is not None:
                window_end = min(window_end, next_scoring_ts)

            results.append(measure_reaction(ev, matchup, ticker, trades, candles, window_end, args))

    if not results and not no_match_games:
        print("No scoring events found in the given range/cache -- try a wider date "
              "range or check --cache-file if one was passed.")
        return

    print(f"\n{'sport':5} {'date':10} {'matchup':28} {'ctx':8} {'team':16} {'move_c':>7} "
          f"{'tier':6} {'status':12} {'t_first':>9} {'t_50pct':>9}")
    for r in results:
        t_first = f"{r.time_to_first_move_s:.1f}s" if r.time_to_first_move_s is not None else "--"
        t_50 = f"{r.time_to_50pct_s:.1f}s" if r.time_to_50pct_s is not None else "--"
        move = f"{r.eventual_move_c:+.1f}" if r.eventual_move_c is not None else "--"
        tier = _leverage_tier(r.eventual_move_c)
        print(f"{r.sport:5} {r.date:10} {r.matchup[:28]:28} {r.context[:8]:8} {r.team[:16]:16} "
              f"{move:>7} {tier:6} {r.status:12} {t_first:>9} {t_50:>9}")

    print(f"\n{'='*70}\nAGGREGATE ({len(results)} scoring events)\n{'='*70}")
    for sport in sports:
        sport_results = [r for r in results if r.sport == sport]
        if not sport_results:
            continue
        print(f"\n  -- {sport.upper()} (n={len(sport_results)}) --")
        for tier in ("High", "Medium", "Low"):
            tier_results = [r for r in sport_results if _leverage_tier(r.eventual_move_c) == tier]
            firsts = [r.time_to_first_move_s for r in tier_results if r.time_to_first_move_s is not None]
            if not firsts:
                print(f"    {tier:7} tier: n={len(tier_results)}, no confirmed reactions")
                continue
            firsts.sort()
            p50 = median(firsts)
            p90 = firsts[min(len(firsts) - 1, int(0.9 * len(firsts)))]
            print(f"    {tier:7} tier: n={len(tier_results)}, reacted={len(firsts)}, "
                  f"median={p50:.1f}s, p90={p90:.1f}s")
        for status in ("NO_REACTION", "NO_LIQUIDITY", "NO_BASELINE"):
            n = sum(1 for r in sport_results if r.status == status)
            if n:
                print(f"    {status}: {n}")

    if no_match_games:
        print(f"\n{len(no_match_games)} scoring event(s) with no confident Kalshi market match:")
        for g in no_match_games:
            print(f"  {g}")

    if args.lead_only and excluded_not_lead:
        print(f"\n{excluded_not_lead} scoring event(s) excluded by --lead-only "
              f"(not a tied-to-ahead score change).")


if __name__ == "__main__":
    main()
