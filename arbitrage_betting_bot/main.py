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

import schedule
from rich.console import Console
from rich.logging import RichHandler

import config
from storage.db import init_db, log_opportunity, log_alert, add_position, count_alerts_today
from execution.trade_executor import execute_trade, resolve_side
from data.odds_fetcher import OddsAPIClient
from data.kalshi_client import KalshiClient
from core.market_matcher import match_events
from core.value_detector import detect_value
from core.kelly_calculator import calculate_kelly
from core.bankroll_manager import BankrollManager
from core.correlation_tracker import CorrelationTracker
from alerts.alert_manager import send_alert, print_no_value_found
from execution.auto_settle import auto_settle_positions

console = Console()


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def run_scan(
    odds_client: OddsAPIClient,
    kalshi_client: KalshiClient,
    bm: BankrollManager,
    tracker: CorrelationTracker,
    dry_run: bool = False,
    paper: bool = False,
) -> None:
    logger = logging.getLogger(__name__)
    mode = "PAPER" if paper else "DRY RUN" if dry_run else "LIVE"
    logger.info("Starting scan... [%s] (bankroll: $%.2f)", mode, bm.bankroll)

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

    # 1. Fetch data
    odds_events = odds_client.fetch_all_sports()
    kalshi_markets = kalshi_client.fetch_sports_markets()

    if not odds_events:
        logger.warning("No sportsbook events fetched — check ODDS_API_KEY")
        return

    # 2. Match events
    matched = match_events(odds_events, kalshi_markets)
    if not matched:
        logger.info("No sportsbook events matched to Kalshi markets this scan")
        return

    # 3. Detect value (hard filters already applied inside detect_value)
    opportunities = detect_value(matched)
    if not opportunities:
        print_no_value_found()
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
            continue
        score = _composite_score(opp, sizing)
        scored.append((score, opp, sizing))

    # Sort by composite score descending — best opportunities first
    scored.sort(key=lambda t: t[0], reverse=True)

    if not scored:
        print_no_value_found()
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
            break

    if alerted == 0:
        print_no_value_found()

    bm.snapshot()

    # Auto-settle any open positions whose Kalshi market has now resolved
    if not dry_run:
        auto_settle_positions(is_paper=paper)

    logger.info("Scan complete. %d order(s) placed.", alerted)


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

    def scan():
        run_scan(
            odds_client, kalshi_client, bm, tracker,
            dry_run=args.dry_run,
            paper=args.paper,
        )

    if args.once:
        scan()
        return

    mode_label = "[yellow]PAPER TRADING[/yellow]" if args.paper else "[bold green]LIVE[/bold green]"
    console.print(
        f"[bold]Bot started ({mode_label}).[/bold] "
        f"Scanning every {config.POLL_INTERVAL_SECONDS // 60} minutes. "
        f"Bankroll: [bold]${bankroll:,.2f}[/bold]. "
        f"Press Ctrl+C to stop."
    )
    scan()  # immediate first scan
    schedule.every(config.POLL_INTERVAL_SECONDS).seconds.do(scan)

    try:
        while True:
            schedule.run_pending()
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
