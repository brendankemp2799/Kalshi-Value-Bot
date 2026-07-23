"""
Arbitrage Betting Bot — Main Orchestrator

Compares sharp sportsbook consensus (The Odds API) against Kalshi prices.
When edge is found, sizes via Kelly Criterion and places a Kalshi order.

Modes:
    python main.py                         # LIVE — place real Kalshi orders automatically
    python main.py --paper                 # paper trading: simulate bets, no real capital
    python main.py --dry-run               # observation only: print alerts, no DB writes
    python main.py --once                  # single scan then exit (combine with any mode)
    python main.py --bankroll 2000         # override bankroll from CLI

Mode comparison:
    live      Executes real Kalshi orders, logs positions to DB.
    --paper   Full simulation with DB writes (is_paper=1). No orders placed.
              Correlation/exposure logic identical to live — use to validate the bot.
    --dry-run Pure observation. Nothing written to DB. Quick spot-checks only.

Setup (first time):
    1. Fill in ODDS_API_KEY and KALSHI_API_KEY in .env
    3. pip install -r requirements.txt
    4. python main.py --once --dry-run      # sanity check
    5. python main.py --paper               # paper trade for a few weeks
    6. python main.py                       # go live when satisfied
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from rich.console import Console
from rich.logging import RichHandler

import config
from storage.db import init_db, log_opportunity, log_alert, add_position, get_daily_stake_total, count_open_positions, log_scan_results, mark_scan_start, get_api_credits, update_bot_heartbeat
from execution.trade_executor import execute_trade, resolve_side
from data.odds_fetcher import OddsAPIClient, _in_season
from data.kalshi_client import KalshiClient
from core.market_matcher import match_events
from core.value_detector import detect_value, Outcome
from core.kelly_calculator import calculate_kelly
from core.bankroll_manager import BankrollManager
from core.correlation_tracker import CorrelationTracker
from alerts.alert_manager import send_alert
from execution.auto_settle import auto_settle_positions

console = Console()


def _update_scan_log(scan_log: list[dict], opp, status: str, reason: str) -> None:
    """Update the scan_log entry for a ValueOpportunity after Kelly/blocking decisions."""
    ticker = opp.matched_event.kalshi_market.ticker
    team = opp.team_name
    for entry in reversed(scan_log):
        if entry.get("kalshi_ticker") == ticker and entry.get("team_name") == team:
            entry["status"] = status
            entry["reason"] = reason
            return


def _finalise_scan_log(scan_log: list[dict], scan_id: str) -> None:
    """Stamp scanned_at on all entries and write to DB."""
    now = datetime.utcnow().isoformat()
    for entry in scan_log:
        entry["scanned_at"] = now
    log_scan_results(scan_id, scan_log)


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def _log_api_credits(logger: logging.Logger) -> None:
    """Log Odds API credit usage after every scan (all exit paths)."""
    try:
        credits = get_api_credits()
        if credits and credits["used_total"] is not None:
            used_scan = credits["used_this_scan"]
            used_scan_str = f"{used_scan} this scan, " if used_scan is not None else ""
            logger.info(
                "Odds API credits — %s%s used total, %s remaining",
                used_scan_str,
                credits["used_total"],
                credits["remaining"] if credits["remaining"] is not None else "?",
            )
    except Exception:
        pass  # never crash a scan over credit logging



def run_scan(
    odds_client: OddsAPIClient,
    kalshi_client: KalshiClient,
    bm: BankrollManager,
    tracker: CorrelationTracker,
    dry_run: bool = False,
    paper: bool = False,
    _prefetched_odds: list | None = None,
    _prefetched_kalshi: list | None = None,
    refresh_balance: bool = False,
) -> None:
    """
    Run one scan cycle.

    If _prefetched_odds / _prefetched_kalshi are provided the API fetch step is
    skipped — the variable-frequency loop passes cached+fresh data here so we
    don't double-spend credits.
    """
    logger = logging.getLogger(__name__)
    mode = "PAPER" if paper else "DRY RUN" if dry_run else "LIVE"

    # Refresh live Kalshi balance so Kelly sizing tracks actual account changes
    if refresh_balance and not paper and not dry_run:
        try:
            live_balance = kalshi_client.fetch_balance()
            if live_balance > 0:
                bm.bankroll = live_balance
        except Exception:
            pass  # keep existing bankroll on failure

    logger.info("Starting scan... [%s] (bankroll: $%.2f)", mode, bm.bankroll)
    if not dry_run:
        update_bot_heartbeat()

    # 0. Check hard limits before spending any API credits
    if not dry_run:
        daily_staked = get_daily_stake_total(is_paper=paper)
        daily_risk_cap = bm.bankroll * config.MAX_DAILY_CAPITAL_RISK_PCT
        if daily_staked >= daily_risk_cap:
            logger.info(
                "Daily capital risk cap reached ($%.2f staked today / $%.2f cap) — "
                "skipping API fetch. Running auto-settle only.",
                daily_staked, daily_risk_cap,
            )
            auto_settle_positions(is_paper=paper, capture_closing_lines=True, manage_open_positions=config.ENABLE_TRAILING_STOP)
            bm.snapshot()
            _log_api_credits(logger)
            return

    exposure_pct = bm.total_at_risk / bm.bankroll if bm.bankroll > 0 else 0
    if exposure_pct >= config.MAX_TOTAL_EXPOSURE_PCT:
        logger.info(
            "Bankroll fully deployed (%.0f%% exposed, max %.0f%%) — "
            "skipping API fetch. Running auto-settle only.",
            exposure_pct * 100, config.MAX_TOTAL_EXPOSURE_PCT * 100,
        )
        if not dry_run:
            auto_settle_positions(is_paper=paper, capture_closing_lines=True, manage_open_positions=config.ENABLE_TRAILING_STOP)
            bm.snapshot()
        _log_api_credits(logger)
        return

    if not dry_run:
        open_count = count_open_positions(is_paper=paper)
        if open_count >= config.MAX_OPEN_POSITIONS:
            logger.info(
                "Max open positions reached (%d/%d) — skipping API fetch. "
                "Running auto-settle only.",
                open_count, config.MAX_OPEN_POSITIONS,
            )
            auto_settle_positions(is_paper=paper, capture_closing_lines=True, manage_open_positions=config.ENABLE_TRAILING_STOP)
            bm.snapshot()
            _log_api_credits(logger)
            return

    # 1. Fetch data (or use pre-fetched data from variable-frequency loop)
    if _prefetched_odds is not None and _prefetched_kalshi is not None:
        odds_events = _prefetched_odds
        kalshi_markets = _prefetched_kalshi
    else:
        # --once path: mark start before the fetch so credits are tracked correctly
        mark_scan_start()
        odds_events = odds_client.fetch_all_sports()
        kalshi_markets = kalshi_client.fetch_sports_markets()

    if not odds_events:
        logger.warning("No sportsbook events fetched — check ODDS_API_KEY")
        return

    # 2. Match events
    matched = match_events(odds_events, kalshi_markets)
    if not matched:
        logger.info("No sportsbook events matched to Kalshi markets this scan")
        _log_api_credits(logger)
        return

    # 3. Detect value (hard filters already applied inside detect_value)
    scan_id = datetime.utcnow().isoformat()
    scan_log: list[dict] = []
    opportunities = detect_value(matched, min_edge=config.MIN_EDGE, scan_log=scan_log)
    if not opportunities:
        if not dry_run:
            _finalise_scan_log(scan_log, scan_id)
        _log_api_credits(logger)
        logger.info("Scan complete. No value found.")
        return

    # 4a. Score every opportunity (requires Kelly, so must happen here not in value_detector)
    def _composite_score(opp: object, sz: object) -> float:
        """
        Normalized weighted-sum scorer [0–1 range per component]:
          edge      (40%) — primary signal; normalized to 20% gross edge = 1.0
          kelly     (30%) — math confidence; normalized to 50% full-Kelly = 1.0
          books     (15%) — consensus reliability; 8+ books = 1.0
          agreement (15%) — low std dev across books; 0 std = 1.0, ≥5% std = 0
        """
        edge_norm  = min(opp.edge / 0.20, 1.0)
        kelly_norm = min(sz.full_kelly_fraction / 0.50, 1.0)
        book_norm  = min(opp.bookmaker_count / 8.0, 1.0)
        agree_norm = max(0.0, 1.0 - opp.consensus_std / 0.05)
        return 0.40 * edge_norm + 0.30 * kelly_norm + 0.15 * book_norm + 0.15 * agree_norm

    scored = []
    for opp in opportunities:
        sizing = calculate_kelly(
            consensus_prob=opp.consensus_prob,
            market_price=opp.market_price,
            bankroll=bm.bankroll,
            consensus_std=opp.consensus_std,
        )
        if not sizing.has_edge:
            logger.debug("Kelly says no edge for %s — skipping", opp.team_name)
            _update_scan_log(scan_log, opp, "kelly_no_edge",
                             "Kelly criterion: mathematical edge doesn't justify a bet")
            continue
        min_bet = config.MIN_BET_DOLLARS
        if sizing.recommended_dollars < min_bet:
            reason = (
                f"Kelly bet ${sizing.recommended_dollars:.2f} below "
                f"minimum ${min_bet:.2f}"
            )
            logger.debug("Skip %s — %s", opp.team_name, reason)
            _update_scan_log(scan_log, opp, "kelly_no_edge", reason)
            continue
        score = _composite_score(opp, sizing)
        scored.append((score, opp, sizing))

    # Sort by composite score descending — best opportunities first
    scored.sort(key=lambda t: t[0], reverse=True)

    # Detect true arb pairs: both HOME and AWAY of the same non-soccer 2-way game
    # appear in scored with positive edge. Both sides can be bet simultaneously for
    # a near-guaranteed profit (YES_home_ask + YES_away_ask < 1.0).
    arb_game_keys: set[tuple[str, str]] = set()
    _game_outcomes: dict[tuple[str, str], set] = {}
    for _, opp, _ in scored:
        ev = opp.matched_event.odds_event
        if "soccer" in ev.sport_key:
            continue  # soccer is 3-way; can't guarantee payout on both YES sides
        game_key = (ev.home_team, ev.away_team)
        _game_outcomes.setdefault(game_key, set()).add(opp.outcome)
    for game_key, outcomes in _game_outcomes.items():
        if Outcome.HOME in outcomes and Outcome.AWAY in outcomes:
            home_ask = next(
                (opp.market_price for _, opp, _ in scored
                 if opp.matched_event.odds_event.home_team == game_key[0]
                 and opp.outcome == Outcome.HOME), None
            )
            away_ask = next(
                (opp.market_price for _, opp, _ in scored
                 if opp.matched_event.odds_event.home_team == game_key[0]
                 and opp.outcome == Outcome.AWAY), None
            )
            if home_ask is not None and away_ask is not None and (home_ask + away_ask) < 1.0:
                arb_game_keys.add(game_key)
                logger.info(
                    "[ARB] True arbitrage detected: %s vs %s "
                    "(HOME ask=%.3f + AWAY ask=%.3f = %.3f < 1.0, guaranteed profit)",
                    game_key[0], game_key[1], home_ask, away_ask, home_ask + away_ask,
                )

    if not scored:
        if not dry_run:
            _finalise_scan_log(scan_log, scan_id)
        _log_api_credits(logger)
        logger.info("Scan complete. No value found.")
        return

    # 4b. Pre-check all opportunities sequentially (reads DB — must be serial)
    alerted = 0
    approved_live: list[tuple] = []  # (opp, sizing, opp_id) for live parallel execution

    for _score, opp, sizing in scored:
        allowed, reason = tracker.is_allowed(opp, sizing.recommended_dollars, arb_game_keys=arb_game_keys)

        event = opp.matched_event.odds_event
        opp_id = None
        if not dry_run:
            opp_id = log_opportunity(
                sport=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                team_name=opp.team_name,
                platform="Kalshi",
                consensus_prob=opp.consensus_prob,
                market_price=opp.market_price,
                edge=opp.edge,
                market_url=opp.market_url,
                alerted=allowed,
            )

        if not allowed:
            logger.debug("Blocked: %s — %s", opp.team_name, reason)
            _update_scan_log(scan_log, opp, "blocked", reason)
            continue

        if dry_run:
            send_alert(opp, sizing, dry_run=True, paper=False)
            alerted += 1
        elif paper and opp_id:
            send_alert(opp, sizing, dry_run=False, paper=True)
            alerted += 1
            log_alert(opp_id, sizing.recommended_dollars, bm.bankroll)
            _price = max(0.01, min(0.99, opp.market_price))
            _count = max(1, math.floor(sizing.recommended_dollars / _price))
            actual_stake = round(_count * _price, 2)
            add_position(
                sport=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                team_name=opp.team_name,
                platform="Kalshi",
                stake=actual_stake,
                market_price=opp.market_price,
                is_paper=True,
                execution_status="paper",
                market_ticker=opp.matched_event.kalshi_market.ticker,
                side=resolve_side(opp),
                edge=opp.edge,
                bookmaker_count=opp.bookmaker_count,
                consensus_std=opp.consensus_std,
                kalshi_spread=opp.matched_event.kalshi_market.spread,
                commence_time=event.commence_time.isoformat(),
                bet_type=opp.matched_event.kalshi_market.bet_type,
                threshold=opp.matched_event.kalshi_market.threshold,
                bookmakers_json=json.dumps(event.bookmakers),
            )
            logger.info("[PAPER] Position logged: %s $%.2f on Kalshi",
                        opp.team_name, sizing.recommended_dollars)
        elif opp_id:
            # Live mode: queue for parallel execution below
            approved_live.append((opp, sizing, opp_id))

    # 4c. Live mode — place all approved orders in parallel (GTC timeouts run concurrently)
    if approved_live:
        def _do_trade(args):
            opp, sizing, _ = args
            return execute_trade(opp, sizing)

        with ThreadPoolExecutor(max_workers=len(approved_live)) as pool:
            future_map = {pool.submit(_do_trade, item): item for item in approved_live}
            for future in as_completed(future_map):
                opp, sizing, opp_id = future_map[future]
                order_id, exec_status, side, failure_reason, actual_stake, fill_type = future.result()
                event = opp.matched_event.odds_event
                add_position(
                    sport=event.sport_key,
                    home_team=event.home_team,
                    away_team=event.away_team,
                    team_name=opp.team_name,
                    platform="Kalshi",
                    stake=actual_stake,
                    market_price=opp.market_price,
                    is_paper=False,
                    order_id=order_id,
                    execution_status=exec_status,
                    market_ticker=opp.matched_event.kalshi_market.ticker,
                    side=side,
                    edge=opp.edge,
                    bookmaker_count=opp.bookmaker_count,
                    consensus_std=opp.consensus_std,
                    kalshi_spread=opp.matched_event.kalshi_market.spread,
                    commence_time=event.commence_time.isoformat(),
                    bet_type=opp.matched_event.kalshi_market.bet_type,
                    threshold=opp.matched_event.kalshi_market.threshold,
                    bookmakers_json=json.dumps(event.bookmakers),
                    failure_reason=failure_reason or None,
                    fill_type=fill_type or "taker",
                )
                if exec_status == "submitted":
                    send_alert(opp, sizing, dry_run=False, paper=False)
                    alerted += 1
                    log_alert(opp_id, sizing.recommended_dollars, bm.bankroll)
                    logger.info("[LIVE] Order submitted: %s $%.2f on Kalshi  (order_id=%s)",
                                opp.team_name, sizing.recommended_dollars, order_id)
                else:
                    tracker.record_failure(opp.matched_event.kalshi_market.ticker)
                    logger.error("[LIVE] Order FAILED: %s $%.2f — position logged with status=failed",
                                 opp.team_name, sizing.recommended_dollars)
                    _update_scan_log(scan_log, opp, "execution_failed",
                                     failure_reason or "Order unfilled or rejected")


    if not dry_run:
        _finalise_scan_log(scan_log, scan_id)

    bm.snapshot()

    # Auto-settle any open positions whose Kalshi market has now resolved
    if not dry_run:
        auto_settle_positions(is_paper=paper, capture_closing_lines=True, manage_open_positions=config.ENABLE_TRAILING_STOP)

    _log_api_credits(logger)
    if alerted > 0:
        logger.info("Scan complete. %d bet(s) placed.", alerted)
    else:
        logger.info("Scan complete. No value found.")
    return


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi Arbitrage Bot")
    parser.add_argument("--once", action="store_true", help="Run one scan then exit")
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Paper trading mode: simulate bets and log positions without real capital",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without writing to DB")
    parser.add_argument("--bankroll", type=float, default=None, help="Override bankroll amount")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    if not config.ODDS_API_KEY:
        console.print("[bold red]ERROR:[/bold red] ODDS_API_KEY not set in .env")
        sys.exit(1)

    if not config.KALSHI_API_KEY:
        console.print("[bold red]ERROR:[/bold red] KALSHI_API_KEY not set in .env")
        sys.exit(1)

    if not args.dry_run:
        init_db()

    bankroll = args.bankroll or config.BANKROLL
    bm = BankrollManager(bankroll=bankroll, is_paper=args.paper)
    tracker = CorrelationTracker(bankroll_manager=bm)

    odds_client = OddsAPIClient()
    kalshi_client = KalshiClient()

    # In live mode, pull actual available balance from Kalshi instead of using config
    if not args.paper and not args.dry_run and not args.bankroll:
        live_balance = kalshi_client.fetch_balance()
        logger.info("Kalshi available balance: $%.2f", live_balance)
        bm = BankrollManager(bankroll=live_balance, is_paper=False)
        tracker = CorrelationTracker(bankroll_manager=bm)

    # Lock file: prevent two bot instances from running at the same time.
    # --once / --dry-run / --paper are exempt since they're read-only or one-shot.
    _LOCK = "/tmp/arbitrage-bot.lock"
    if not args.once and not args.dry_run and not args.paper:
        if os.path.exists(_LOCK):
            try:
                existing_pid = int(open(_LOCK).read().strip())
                if os.path.exists(f"/proc/{existing_pid}"):
                    console.print(f"[bold red]ERROR:[/bold red] Bot already running (PID {existing_pid}). "
                                  f"Stop it first or delete {_LOCK}.")
                    sys.exit(1)
            except (ValueError, OSError):
                pass  # stale lock — overwrite it
        with open(_LOCK, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(lambda: os.path.exists(_LOCK) and os.remove(_LOCK))

    if args.once:
        run_scan(
            odds_client, kalshi_client, bm, tracker,
            dry_run=args.dry_run,
            paper=args.paper,
        )
        return

    mode_label = "[yellow]PAPER TRADING[/yellow]" if args.paper else "[bold green]LIVE[/bold green]"
    console.print(
        f"[bold]Bot started ({mode_label}) — variable-frequency polling.[/bold] "
        f"Default: {config.POLL_INTERVAL_DEFAULT_SECONDS // 60} min / "
        f"≤{config.PRE_GAME_THRESHOLD_HOURS}h to tip-off: "
        f"{config.POLL_INTERVAL_PRE_GAME_SECONDS // 60} min / "
        f"≤{config.NEAR_GAME_THRESHOLD_MINUTES} min to tip-off: "
        f"{config.POLL_INTERVAL_NEAR_GAME_SECONDS // 60} min. "
        f"Bankroll: [bold]${bm.bankroll:,.2f}[/bold]. "
        f"Press Ctrl+C to stop."
    )
    _run_variable_loop(
        odds_client, kalshi_client, bm, tracker,
        dry_run=args.dry_run,
        paper=args.paper,
        logger=logger,
    )


def _log_sport_intervals(sport_events: dict, logger: logging.Logger) -> None:
    """Log the current polling interval for each active sport."""
    now = datetime.now(timezone.utc)
    lines = []
    for sport in config.SPORTS:
        if not _in_season(sport):
            continue
        cached = sport_events.get(sport, [])
        interval = _sport_poll_interval(sport, cached)
        short = sport.split("_")[-1].upper()
        if interval == config.POLL_INTERVAL_NEAR_GAME_SECONDS:
            label = f"{interval // 60}min ⚡ (game <{config.NEAR_GAME_THRESHOLD_MINUTES}min)"
        elif interval == config.POLL_INTERVAL_PRE_GAME_SECONDS:
            label = f"{interval // 60}min 🔜 (game <{config.PRE_GAME_THRESHOLD_HOURS}h)"
        else:
            label = f"{interval // 60}min"
        lines.append(f"  {short:<8} → {label}")
    logger.info("Polling intervals:\n%s", "\n".join(lines))


def _sport_poll_interval(sport: str, cached_events: list) -> int:
    """
    Return the polling interval (seconds) for *sport* based on its nearest
    upcoming game. Uses the sportsbook events cached from the last fetch.
    """
    now = datetime.now(timezone.utc)
    upcoming = [e for e in cached_events if e.commence_time > now]
    if not upcoming:
        return config.POLL_INTERVAL_DEFAULT_SECONDS
    nearest = min(e.commence_time for e in upcoming)
    minutes_away = (nearest - now).total_seconds() / 60.0
    if minutes_away <= config.NEAR_GAME_THRESHOLD_MINUTES:
        return config.POLL_INTERVAL_NEAR_GAME_SECONDS
    if minutes_away <= config.PRE_GAME_THRESHOLD_HOURS * 60:
        return config.POLL_INTERVAL_PRE_GAME_SECONDS
    return config.POLL_INTERVAL_DEFAULT_SECONDS


def _run_variable_loop(
    odds_client: OddsAPIClient,
    kalshi_client: KalshiClient,
    bm: BankrollManager,
    tracker: CorrelationTracker,
    dry_run: bool,
    paper: bool,
    logger: logging.Logger,
) -> None:
    """
    Variable-frequency polling loop.

    Each sport is fetched independently at a rate determined by how close its
    nearest upcoming game is. Only due sports are re-fetched on each tick,
    so credit usage scales with game proximity rather than always burning all
    credits at once.

    Tick granularity: 30 seconds (small enough to respect the 2-min near-game
    interval without wasting CPU when nothing is due).
    """
    # Per-sport caches keyed by Odds API sport key
    sport_events: dict[str, list] = {}    # sport → list[OddsEvent]
    sport_kalshi: dict[str, list] = {}    # sport → list[KalshiMarket]
    last_fetched: dict[str, float] = {}   # sport → unix timestamp of last fetch

    # ── Initial full fetch ──────────────────────────────────────────────────
    mark_scan_start()
    now_ts = time.time()
    for i, sport in enumerate(config.SPORTS):
        if not _in_season(sport):
            logger.debug("Skipping %s — off season", sport)
            continue
        if i > 0:
            time.sleep(1)  # avoid 429 between sports
        markets = config.SPORT_MARKETS.get(sport, config.ODDS_API_MARKETS)
        sport_events[sport] = odds_client.fetch_odds(sport, markets=markets)
        sport_kalshi[sport] = kalshi_client.fetch_sports_markets(sports=[sport])
        last_fetched[sport] = now_ts

    all_events  = [e for evs in sport_events.values() for e in evs]
    all_kalshi  = [m for ms  in sport_kalshi.values()  for m in ms]
    try:
        run_scan(
            odds_client, kalshi_client, bm, tracker,
            dry_run=dry_run, paper=paper,
            _prefetched_odds=all_events, _prefetched_kalshi=all_kalshi,
        )
    except Exception as e:
        logger.error("Initial scan error (continuing to loop): %s", e, exc_info=True)
    _log_sport_intervals(sport_events, logger)

    # ── Main tick loop ──────────────────────────────────────────────────────
    try:
        while True:
            time.sleep(30)
            now_ts = time.time()

            due: list[str] = []
            for sport in config.SPORTS:
                if not _in_season(sport):
                    continue
                cached = sport_events.get(sport, [])
                interval = _sport_poll_interval(sport, cached)
                elapsed  = now_ts - last_fetched.get(sport, 0)
                if elapsed >= interval:
                    due.append(sport)

            if not due:
                continue

            logger.info(
                "Refreshing %d sport(s): %s",
                len(due), ", ".join(due),
            )

            mark_scan_start()
            for i, sport in enumerate(due):
                if i > 0:
                    time.sleep(1)
                markets = config.SPORT_MARKETS.get(sport, config.ODDS_API_MARKETS)
                sport_events[sport] = odds_client.fetch_odds(sport, markets=markets)
                sport_kalshi[sport] = kalshi_client.fetch_sports_markets(sports=[sport])
                last_fetched[sport] = now_ts

            all_events = [e for evs in sport_events.values() for e in evs]
            all_kalshi = [m for ms  in sport_kalshi.values()  for m in ms]
            try:
                run_scan(
                    odds_client, kalshi_client, bm, tracker,
                    dry_run=dry_run, paper=paper,
                    _prefetched_odds=all_events, _prefetched_kalshi=all_kalshi,
                    refresh_balance=True,
                )
            except Exception as e:
                logger.error("Scan error (will retry next tick): %s", e, exc_info=True)
            _log_sport_intervals(sport_events, logger)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
