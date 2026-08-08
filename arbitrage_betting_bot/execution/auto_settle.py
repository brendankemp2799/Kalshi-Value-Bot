"""
Auto-settlement of open positions.

After each scan (and on dashboard refresh), this module checks every open
position against the Kalshi API. When a market has resolved (YES/NO/void),
the position is automatically closed and P&L is recorded.

How it works:
  1. Load all open positions that have a market_ticker stored.
  2. For each unique ticker, call GET /markets/{ticker}.
  3. If the market result is "yes" or "no":
       won = (position.side == result)  →  settle as won or lost
  4. If the market result is "void":
       settle as void (P&L = 0, stake effectively returned).
  5. Skip markets still open/unresolved.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from storage.db import (
    get_open_positions,
    settle_position,
    get_positions_pending_closing_lines,
    set_closing_lines,
)
from data.kalshi_auth import auth_headers
from data.kalshi_client import KalshiClient
from data.odds_fetcher import OddsAPIClient
from core.odds_converter import consensus_stats

logger = logging.getLogger(__name__)


def _fetch_market(ticker: str) -> dict | None:
    """Fetch a single Kalshi market by ticker. Returns the market dict or None on error."""
    url = f"{config.KALSHI_API_BASE_URL}/markets/{ticker}"
    try:
        resp = requests.get(url, headers=auth_headers("GET", url), timeout=10)
        resp.raise_for_status()
        return resp.json().get("market", {})
    except requests.HTTPError as e:
        logger.warning("Kalshi HTTP error fetching market %s: %s", ticker, e)
    except requests.RequestException as e:
        logger.warning("Network error fetching market %s: %s", ticker, e)
    return None


def auto_settle_positions(
    is_paper: bool = False,
    capture_closing_lines: bool = False,
    manage_open_positions: bool = False,
) -> int:
    """
    Check all open positions and settle any whose Kalshi market has resolved.

    capture_closing_lines: also backfill Kalshi/consensus closing-line data for
    settled positions missing it. The Odds API call this involves is credit-
    metered, so only the main scan loop should pass True here — never the
    dashboard's 60s auto-refresh path (see _capture_closing_lines).

    manage_open_positions: also run the trailing-stop risk manager (see
    execution/risk_manager.py) against every still-open position using the same
    market data already fetched here. This can place REAL closing orders — only
    the main scan loop should ever pass True here, never the dashboard, and only
    when config.ENABLE_TRAILING_STOP is on.

    Returns the number of positions settled in this call.
    """
    open_positions = get_open_positions(is_paper=is_paper)
    # Only consider positions where we stored a ticker and side
    checkable = [p for p in open_positions if p["market_ticker"] and p["side"]]

    if not checkable:
        return 0

    # Batch by ticker to avoid redundant API calls
    ticker_to_market: dict[str, dict | None] = {}
    for pos in checkable:
        ticker = pos["market_ticker"]
        if ticker not in ticker_to_market:
            ticker_to_market[ticker] = _fetch_market(ticker)

    settled_count = 0
    for pos in checkable:
        ticker = pos["market_ticker"]
        market = ticker_to_market.get(ticker)
        if not market:
            continue

        if manage_open_positions:
            try:
                if _check_trailing_stop(pos, market, is_paper):
                    continue  # position just closed early — don't also run natural settlement below
            except Exception as e:
                logger.warning("Trailing stop check failed for position #%d: %s", pos["id"], e)

            if config.ENABLE_STOP_LOSS:
                try:
                    if _check_stop_loss(pos, market, is_paper):
                        continue  # position just closed early — don't also run natural settlement below
                except Exception as e:
                    logger.warning("Stop-loss check failed for position #%d: %s", pos["id"], e)

        result = (market.get("result") or "").lower()
        if result not in ("yes", "no", "void"):
            # Market still open or result not yet published
            continue

        pos_id = pos["id"]
        side = (pos["side"] or "").lower()

        if result == "void":
            outcome = "void"
        else:
            outcome = "won" if side == result else "lost"

        pnl = settle_position(pos_id, outcome)
        mode_tag = "[PAPER]" if is_paper else "[LIVE]"
        logger.info(
            "%s Auto-settled position #%d (%s on %s): %s  P&L=$%.2f",
            mode_tag, pos_id, side.upper(), ticker, outcome.upper(), pnl,
        )
        settled_count += 1

    if settled_count:
        logger.info("Auto-settled %d position(s).", settled_count)

    if capture_closing_lines:
        try:
            _capture_closing_lines()
        except Exception as e:
            logger.warning("Closing-line capture failed: %s", e)

    return settled_count


def _check_trailing_stop(pos, market: dict, is_paper: bool) -> bool:
    """
    Evaluate + apply the trailing-stop decision for one open position.
    Returns True only if the position was actually closed early this cycle (caller
    should not also run the natural-settlement check against the same now-stale
    `pos` row) — this reflects execute_trailing_stop()'s real outcome, not just
    whether a close was attempted, since a close attempt can fail (e.g. Kalshi's
    live contract count briefly unavailable) and natural settlement must still run
    in that case rather than being silently skipped too.
    """
    from execution.risk_manager import evaluate_trailing_stop, execute_trailing_stop, ActionKind

    action = evaluate_trailing_stop(pos, market)
    if action.kind == ActionKind.NONE:
        return False
    return execute_trailing_stop(pos, action, is_paper)


def _check_stop_loss(pos, market: dict, is_paper: bool) -> bool:
    """
    Evaluate + apply the stop-loss decision for one open position. Same
    True-only-on-actual-close contract as _check_trailing_stop().
    """
    from execution.risk_manager import evaluate_stop_loss, execute_stop_loss, ActionKind

    action = evaluate_stop_loss(pos, market)
    if action.kind == ActionKind.NONE:
        return False
    return execute_stop_loss(pos, action, is_paper)


# ── Closing Line Value ──────────────────────────────────────────────────────────

def _fetch_kalshi_closing_price(pos) -> float | None:
    """
    Kalshi price of our side at (or just before) game start, via candlesticks.
    Mirrors the yes/no → "price of the side we bought" convention already used
    for market_price at entry, so the two are directly comparable.
    """
    ticker = pos["market_ticker"]
    commence_str = pos["commence_time"]
    if not ticker or not commence_str:
        return None
    try:
        commence = datetime.fromisoformat(commence_str)
        if commence.tzinfo is None:
            commence = commence.replace(tzinfo=timezone.utc)
        end_ts = int(commence.timestamp())
        start_ts = end_ts - 7200  # 2h lookback window
        series_ticker = ticker.split("-")[0]
        data = KalshiClient().fetch_candlesticks(series_ticker, ticker, start_ts, end_ts)
        candles = data.get("candlesticks", [])
        if not candles:
            return None
        last = candles[-1]
        yes_ask = float((last.get("yes_ask") or {}).get("close_dollars") or 0)
        yes_bid = float((last.get("yes_bid") or {}).get("close_dollars") or 0)
        last_price = float((last.get("price") or {}).get("close_dollars") or 0)

        side = (pos["side"] or "yes").lower()
        if side == "yes":
            price = yes_ask if yes_ask > 0 else last_price
        else:
            price = (1.0 - yes_bid) if yes_bid > 0 else ((1.0 - last_price) if last_price > 0 else 0.0)
        return round(price, 4) if price > 0 else None
    except Exception as e:
        logger.warning("Closing Kalshi price fetch failed for %s: %s", ticker, e)
        return None


def _fetch_consensus_closing_prob(pos) -> float | None:
    """
    Sportsbook consensus probability (same de-vig method as entry-time edge)
    for our side, at the historical odds snapshot closest to game start.
    """
    sport = pos["sport"]
    commence_str = pos["commence_time"]
    if not sport or not commence_str:
        return None
    try:
        date_iso = commence_str.replace("+00:00", "Z")
        events = OddsAPIClient().fetch_historical_odds(sport, date_iso, "h2h,totals,spreads")
        home, away = pos["home_team"], pos["away_team"]
        event = next(
            (e for e in events if e.get("home_team") == home and e.get("away_team") == away),
            None,
        )
        if event is None:
            return None

        bet_type = pos["bet_type"] or "h2h"
        team_name = pos["team_name"] or ""
        threshold = pos["threshold"]

        if bet_type == "h2h":
            outcome_name = "Draw" if team_name == "Draw" else team_name
            market_key, point = "h2h", None
        elif bet_type == "totals":
            outcome_name = "Over" if team_name.lower().startswith("over") else "Under"
            market_key, point = "totals", threshold
        elif bet_type == "spread":
            covering = home if team_name.startswith(home) else away
            outcome_name, market_key, point = covering, "spreads", threshold
        else:
            return None

        consensus, _book_count, _std = consensus_stats(
            event.get("bookmakers", []), outcome_name, market_key=market_key, point=point,
        )
        return consensus
    except Exception as e:
        logger.warning("Closing consensus fetch failed for position #%s: %s", pos["id"], e)
        return None


def _capture_closing_lines() -> None:
    """
    Backfill Kalshi + consensus closing-line data for settled positions that
    don't have it yet (retry-capped — see get_positions_pending_closing_lines).
    """
    pending = get_positions_pending_closing_lines()
    for pos in pending:
        kalshi_close = _fetch_kalshi_closing_price(pos)
        consensus_close = _fetch_consensus_closing_prob(pos)
        set_closing_lines(pos["id"], kalshi_close, consensus_close)
        logger.info(
            "Closing lines captured for position #%d: kalshi=%s consensus=%s",
            pos["id"],
            f"{kalshi_close:.3f}" if kalshi_close is not None else "—",
            f"{consensus_close:.3f}" if consensus_close is not None else "—",
        )
