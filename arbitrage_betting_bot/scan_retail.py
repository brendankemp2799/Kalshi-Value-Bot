"""
Entrypoint for the retail-sportsbook value detector.

Phase 1+2 of the Kalshi -> legal-retail-sportsbook restructure: fetches sharp +
retail odds in one Odds API call, flags value bets, sizes them against the
derived current bankroll (storage/db.py::get_current_bankroll()), and persists
each one to the `flagged_bets` table for the dashboard/Telegram phases to pick
up. Never touches the (already-stopped) Kalshi execution path.

Dedup: a bet is only written once per (odds_api_event_id, outcome_label) while
an existing non-terminal row for it exists (storage/db.py::
find_open_flagged_bet) -- this is the new pipeline's equivalent of
core/correlation_tracker.py's Rule 0 (same-ticker block), rekeyed off that
tuple instead of a Kalshi ticker. NOTE: correlation_tracker's Rules 1-3
(same-game/team/bankroll-exposure caps) are NOT yet wired into this path --
they're built around Kalshi-shaped position data and need their own
adaptation. Flagged as a follow-up, not silently dropped.

Usage:
    python3 scan_retail.py [--min-edge 0.02] [--bankroll 500] [--no-db]
"""
from __future__ import annotations

import argparse
import logging

import config
# Process-local override: use the sharp+retail union for THIS process's odds
# fetch, without touching the shared default the (stopped) legacy Kalshi path
# still reads. See config.py's RETAIL_SCAN_BOOKMAKERS comment.
config.ODDS_API_BOOKMAKERS = config.RETAIL_SCAN_BOOKMAKERS

import storage.db as db
from data.odds_fetcher import OddsAPIClient
from core.retail_value_detector import detect_retail_value, RetailOpportunity
from core.kelly_calculator import calculate_kelly, BetSizing
from core.deeplink_builder import resolve_deep_link
from alerts.telegram_notifier import send_bet_notification

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _print_opportunity(opp: RetailOpportunity, sizing: BetSizing, bet_id: int | None) -> None:
    tag = f"#{bet_id}" if bet_id is not None else "(not saved)"
    print(
        f"[{opp.sport_key}] {opp.home_team} vs {opp.away_team}  "
        f"{opp.commence_time:%Y-%m-%d %H:%M} UTC  {tag}\n"
        f"  {opp.outcome_label}  @ {opp.book_key}  {opp.book_odds_american:+d}  "
        f"(implied {opp.book_price_prob:.1%})\n"
        f"  fair {opp.consensus_prob:.1%}  edge {opp.edge:+.1%}  "
        f"books={opp.bookmaker_count} std={opp.consensus_std:.3f}\n"
        f"  stake ${sizing.recommended_dollars:.2f}  "
        f"(kelly {sizing.fractional_kelly:.1%} of ${sizing.bankroll:.0f} bankroll)\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Retail sportsbook value scan")
    parser.add_argument("--min-edge", type=float, default=config.MIN_EDGE)
    parser.add_argument("--bankroll", type=float, default=None,
                         help="Override the derived current bankroll (storage/db.py::get_current_bankroll())")
    parser.add_argument("--no-db", action="store_true",
                         help="Detect and print only -- skip writing flagged_bets rows")
    args = parser.parse_args()

    if not args.no_db:
        db.init_db()

    bankroll = args.bankroll if args.bankroll is not None else (
        config.STARTING_BANKROLL if args.no_db else db.get_current_bankroll()
    )

    client = OddsAPIClient()
    events = client.fetch_all_sports()
    logger.info("Fetched %d events across %d configured sports", len(events), len(config.SPORTS))

    opportunities = detect_retail_value(events, min_edge=args.min_edge)
    opportunities.sort(key=lambda o: o.edge, reverse=True)
    logger.info("Found %d candidate opportunities (min_edge=%.1f%%)", len(opportunities), args.min_edge * 100)

    shown = 0
    saved = 0
    for opp in opportunities:
        sizing = calculate_kelly(
            consensus_prob=opp.consensus_prob,
            market_price=opp.book_price_prob,
            bankroll=bankroll,
            consensus_std=opp.consensus_std,
            fee_rate=0.0,
        )
        if not sizing.has_edge:
            continue

        bet_id = None
        if not args.no_db:
            existing = db.find_open_flagged_bet(opp.odds_api_event_id, opp.outcome_label)
            if existing is not None:
                logger.debug("Skipping %s — already flagged as bet #%d (status=%s)",
                             opp.outcome_label, existing["id"], existing["status"])
            else:
                bet_id = db.add_flagged_bet(
                    odds_api_event_id=opp.odds_api_event_id,
                    sport=opp.sport_key,
                    home_team=opp.home_team,
                    away_team=opp.away_team,
                    commence_time=opp.commence_time.isoformat(),
                    bet_type=opp.bet_type,
                    outcome_label=opp.outcome_label,
                    side=opp.side,
                    threshold=opp.threshold,
                    recommended_book=opp.book_key,
                    recommended_odds_american=opp.book_odds_american,
                    recommended_prob=opp.book_price_prob,
                    consensus_prob=opp.consensus_prob,
                    edge=opp.edge,
                    bookmaker_count=opp.bookmaker_count,
                    consensus_std=opp.consensus_std,
                    recommended_stake=sizing.recommended_dollars,
                )
                saved += 1

                # Deep link + notification, best-effort -- a failure here must
                # never lose the flagged bet itself (already committed above).
                try:
                    links = client.fetch_event_links(
                        opp.sport_key, opp.odds_api_event_id,
                        markets=config.SPORT_MARKETS.get(opp.sport_key, "h2h,totals,spreads"),
                        bookmakers=",".join(config.RETAIL_BOOKMAKERS),
                    )
                    url, quality = resolve_deep_link(
                        links, opp.book_key, opp.bet_type, opp.side, opp.threshold,
                    )
                    dashboard_url = f"{config.DASHBOARD_BASE_URL}/value-bets/{bet_id}"
                    db.set_deep_link(bet_id, url, quality, dashboard_url)
                except Exception:
                    logger.exception("Deep link lookup failed for bet #%d — continuing without one", bet_id)
                    url, quality, dashboard_url = None, None, None

                flagged_bet = dict(db.get_flagged_bet(bet_id))
                message_id = send_bet_notification(flagged_bet)
                db.mark_bet_notified(bet_id, message_id)

        _print_opportunity(opp, sizing, bet_id)
        shown += 1

    if not shown:
        print("No value opportunities found this scan.")
    elif not args.no_db:
        logger.info("Saved %d new flagged_bets row(s) (%d already tracked)", saved, shown - saved)


if __name__ == "__main__":
    main()
