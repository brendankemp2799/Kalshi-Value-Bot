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
import json
import logging
import sys
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.logging import RichHandler

import config
from storage.db import init_db, log_opportunity, log_alert, add_position, count_alerts_today, log_scan_results, mark_scan_start, get_api_credits
from execution.trade_executor import execute_trade, resolve_side
from data.odds_fetcher import OddsAPIClient, _in_season
from data.kalshi_client import KalshiClient
from core.market_matcher import match_events
from core.value_detector import detect_value
from core.kelly_calculator import calculate_kelly
from core.bankroll_manager import BankrollManager
from core.correlation_tracker import CorrelationTracker
from alerts.alert_manager import send_alert, print_no_value_found
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
) -> None:
    """
    Run one scan cycle.

    If _prefetched_odds / _prefetched_kalshi are provided the API fetch step is
    skipped — the variable-frequency loop passes cached+fresh data here so we
    don't double-spend credits.
    """
    logger = logging.getLogger(__name__)
    mode = "PAPER" if paper else "DRY RUN" if dry_run else "LIVE"
    logger.info("Starting scan... [%s] (bankroll: $%.2f)", mode, bm.bankroll)
    mark_scan_start()

    # 0. Check hard limits before spending any API credits
    daily_count = count_alerts_today()
    if daily_count >= config.MAX_DAILY_ALERTS:
        logger.info(
            "Daily cap reached (%d/%d) — skipping API fetch. "
            "Running auto-settle only.",
            daily_count, config.MAX_DAILY_ALERTS,
        )
        if not dry_run:
            auto_settle_positions(is_paper=paper)
            bm.snapshot()
        return

    exposure_pct = bm.total_at_risk / bm.bankroll if bm.bankroll > 0 else 0
    if exposure_pct >= config.MAX_TOTAL_EXPOSURE_PCT:
        logger.info(
            "Bankroll fully deployed (%.0f%% exposed, max %.0f%%) — "
            "skipping API fetch. Running auto-settle only.",
            exposure_pct * 100, config.MAX_TOTAL_EXPOSURE_PCT * 100,
        )
        if not dry_run:
            auto_settle_positions(is_paper=paper)
            bm.snapshot()
        return

    # 1. Fetch data (or use pre-fetched data from variable-frequency loop)
    if _prefetched_odds is not None and _prefetched_kalshi is not None:
        odds_events = _prefetched_odds
        kalshi_markets = _prefetched_kalshi
    else:
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
    opportunities = detect_value(matched, scan_log=scan_log)
    if not opportunities:
        print_no_value_found()
        if not dry_run:
            _finalise_scan_log(scan_log, scan_id)
        _log_api_credits(logger)
        return

    # 4a. Score every opportunity (requires Kelly, so must happen here not in value_detector)
    def _composite_score(opp: object, sz: object) -> float:
        """
        Composite quality score for ranking opportunities.
          edge             — primary signal (how mispriced is Kalshi vs consensus)
          full_kelly       — math-backed confidence (higher = stronger edge relative to odds)
          book_confidence  — reliability of consensus (capped at 1.0 for 10+ books)
          agreement        — penalises high std dev across books (books disagreeing = noise)
        """
        book_confidence = min(opp.bookmaker_count / 10.0, 1.0)
        agreement = max(0.0, 1.0 - opp.consensus_std * 10)
        return opp.edge * sz.full_kelly_fraction * book_confidence * agreement

    scored = []
    for opp in opportunities:
        sizing = calculate_kelly(
            consensus_prob=opp.consensus_prob,
            market_price=opp.market_price,
            bankroll=bm.bankroll,
        )
        if not sizing.has_edge:
            logger.debug("Kelly says no edge for %s — skipping", opp.team_name)
            _update_scan_log(scan_log, opp, "kelly_no_edge",
                             "Kelly criterion: mathematical edge doesn't justify a bet")
            continue
        if sizing.recommended_dollars < config.MIN_BET_DOLLARS:
            reason = (
                f"Kelly bet ${sizing.recommended_dollars:.2f} below "
                f"minimum ${config.MIN_BET_DOLLARS:.0f}"
            )
            logger.debug("Skip %s — %s", opp.team_name, reason)
            _update_scan_log(scan_log, opp, "kelly_no_edge", reason)
            continue
        score = _composite_score(opp, sizing)
        scored.append((score, opp, sizing))

    # Sort by composite score descending — best opportunities first
    scored.sort(key=lambda t: t[0], reverse=True)

    if not scored:
        print_no_value_found()
        if not dry_run:
            _finalise_scan_log(scan_log, scan_id)
        _log_api_credits(logger)
        return

    # 4b. Iterate in ranked order; correlation/exposure checks unchanged
    alerted = 0
    for _score, opp, sizing in scored:
        allowed, reason = tracker.is_allowed(opp, sizing.recommended_dollars)

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
            logger.info("Blocked: %s — %s", opp.team_name, reason)
            _update_scan_log(scan_log, opp, "blocked", reason)
            continue

        send_alert(opp, sizing, dry_run=dry_run, paper=paper)
        alerted += 1

        if not dry_run and opp_id:
            log_alert(opp_id, sizing.recommended_dollars, bm.bankroll)

        if paper and opp_id:
            # Paper mode: log simulated position, skip execution
            add_position(
                sport=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                team_name=opp.team_name,
                platform="Kalshi",
                stake=sizing.recommended_dollars,
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
            logger.info(
                "[PAPER] Position logged: %s $%.2f on Kalshi",
                opp.team_name, sizing.recommended_dollars,
            )
        elif not dry_run and not paper and opp_id:
            # Live mode: execute the trade, then record the position
            order_id, exec_status, side = execute_trade(opp, sizing)
            add_position(
                sport=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                team_name=opp.team_name,
                platform="Kalshi",
                stake=sizing.recommended_dollars,
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
            )
            if exec_status == "submitted":
                logger.info(
                    "[LIVE] Order submitted: %s $%.2f on Kalshi  (order_id=%s)",
                    opp.team_name, sizing.recommended_dollars, order_id,
                )
            else:
                logger.error(
                    "[LIVE] Order FAILED: %s $%.2f — position logged with status=failed",
                    opp.team_name, sizing.recommended_dollars,
                )

        if count_alerts_today() >= config.MAX_DAILY_ALERTS:
            logger.info("Daily cap (%d) reached — stopping scan early", config.MAX_DAILY_ALERTS)
            # Mark remaining scored opportunities as daily_cap
            for _, remaining_opp, _ in scored[alerted:]:
                _update_scan_log(scan_log, remaining_opp, "daily_cap",
                                 f"Daily bet cap of {config.MAX_DAILY_ALERTS} reached")
            break

    if alerted == 0:
        print_no_value_found()

    if not dry_run:
        _finalise_scan_log(scan_log, scan_id)

    bm.snapshot()

    # Auto-settle any open positions whose Kalshi market has now resolved
    if not dry_run:
        auto_settle_positions(is_paper=paper)

    _log_api_credits(logger)
    logger.info("Scan complete. %d order(s) placed.", alerted)
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
        f"Bankroll: [bold]${bankroll:,.2f}[/bold]. "
        f"Press Ctrl+C to stop."
    )
    _run_variable_loop(
        odds_client, kalshi_client, bm, tracker,
        dry_run=args.dry_run,
        paper=args.paper,
        logger=logger,
    )


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
    run_scan(
        odds_client, kalshi_client, bm, tracker,
        dry_run=dry_run, paper=paper,
        _prefetched_odds=all_events, _prefetched_kalshi=all_kalshi,
    )

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

            for i, sport in enumerate(due):
                if i > 0:
                    time.sleep(1)
                markets = config.SPORT_MARKETS.get(sport, config.ODDS_API_MARKETS)
                sport_events[sport] = odds_client.fetch_odds(sport, markets=markets)
                sport_kalshi[sport] = kalshi_client.fetch_sports_markets(sports=[sport])
                last_fetched[sport] = now_ts

            all_events = [e for evs in sport_events.values() for e in evs]
            all_kalshi = [m for ms  in sport_kalshi.values()  for m in ms]
            run_scan(
                odds_client, kalshi_client, bm, tracker,
                dry_run=dry_run, paper=paper,
                _prefetched_odds=all_events, _prefetched_kalshi=all_kalshi,
            )

    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
