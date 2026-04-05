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

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from storage.db import get_open_positions, settle_position
from data.kalshi_auth import auth_headers

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


def auto_settle_positions(is_paper: bool = False) -> int:
    """
    Check all open positions and settle any whose Kalshi market has resolved.

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

    return settled_count
