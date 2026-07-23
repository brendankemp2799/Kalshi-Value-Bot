"""
Trailing stop-to-breakeven+ risk management for open positions.

Kalshi has no native stop/conditional order type (verified against their API
reference), so this simulates one: each scan, compute the currently achievable exit
price for a position (same convention as market_price at entry), track the best price
seen since entry (peak_price), and once armed (peak has moved far enough favorably),
trigger a full-position close if price retraces to the trailing stop level.

    arm:   peak_price - entry_price >= config.TRAILING_STOP_ARM_MOVE
    stop:  entry_price + config.TRAILING_STOP_LOCK_FRACTION * (peak_price - entry_price)

The stop rises as peak_price rises — protects more of the gain the further a winner
runs, with no cap on the upside while the position keeps working.

See config.TRAILING_STOP_* for the arm/lock parameters, and config.ENABLE_TRAILING_STOP
for the master on/off switch. Only ever call execute_trailing_stop() from the main
scan loop (main.py) — never from the dashboard — since it can place real orders.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)


class ActionKind(str, Enum):
    NONE = "none"
    UPDATE_PEAK = "update_peak"
    TRIGGER_CLOSE = "trigger_close"


@dataclass
class Action:
    kind: ActionKind
    peak_price: float | None = None   # set for UPDATE_PEAK
    exit_price: float | None = None   # set for TRIGGER_CLOSE


def _achievable_exit_price(side: str, market: dict) -> float | None:
    """
    Price we could actually exit at right now, same convention as market_price:
    YES position -> yes_bid_dollars ; NO position -> 1 - yes_ask_dollars.
    Returns None if the market has no live quote on that side.
    """
    try:
        if side == "yes":
            bid = market.get("yes_bid_dollars")
            return float(bid) if bid not in (None, "") else None
        else:
            ask = market.get("yes_ask_dollars")
            return (1.0 - float(ask)) if ask not in (None, "") else None
    except (TypeError, ValueError):
        return None


def evaluate_trailing_stop(pos, market: dict) -> Action:
    """
    Pure decision function — no side effects, no I/O. `pos` is a positions row
    (sqlite3.Row) with at least: side, market_price, peak_price.
    """
    side = (pos["side"] or "yes").lower()
    entry_price = pos["market_price"]
    peak_price = pos["peak_price"] if pos["peak_price"] is not None else entry_price

    achievable = _achievable_exit_price(side, market)
    if achievable is None:
        return Action(kind=ActionKind.NONE)

    _EPS = 1e-9  # floating-point tolerance, e.g. 0.58-0.48 == 0.09999999999999998 in binary float

    if achievable > peak_price + _EPS:
        return Action(kind=ActionKind.UPDATE_PEAK, peak_price=achievable)

    armed = (peak_price - entry_price) >= config.TRAILING_STOP_ARM_MOVE - _EPS
    if not armed:
        return Action(kind=ActionKind.NONE)

    stop_level = entry_price + config.TRAILING_STOP_LOCK_FRACTION * (peak_price - entry_price)
    if achievable <= stop_level + _EPS:
        return Action(kind=ActionKind.TRIGGER_CLOSE, exit_price=achievable)

    return Action(kind=ActionKind.NONE)


def _fetch_live_contract_count(ticker: str) -> float | None:
    """Authoritative held-contract count from Kalshi's own portfolio data (free, not credit-metered)."""
    try:
        from data.kalshi_auth import auth_headers
        import requests
        url = "https://external-api.kalshi.com/trade-api/v2/portfolio/positions"
        headers = auth_headers("GET", url)
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        for p in resp.json().get("market_positions", []):
            if p.get("ticker") == ticker:
                return abs(float(p.get("position_fp", 0)))
        return None
    except Exception as e:
        logger.warning("Could not fetch live Kalshi position for %s: %s", ticker, e)
        return None


def execute_trailing_stop(pos, action: Action, is_paper: bool) -> None:
    """Apply the decision from evaluate_trailing_stop(): update DB state or close the position."""
    from storage.db import set_peak_price, close_position_early

    if action.kind == ActionKind.NONE:
        return

    if action.kind == ActionKind.UPDATE_PEAK:
        set_peak_price(pos["id"], action.peak_price)
        return

    # TRIGGER_CLOSE
    pos_id = pos["id"]
    side = (pos["side"] or "yes").lower()
    ticker = pos["market_ticker"]
    exit_price = action.exit_price

    if is_paper:
        pnl = close_position_early(pos_id, exit_price, reason="trailing_stop")
        logger.info(
            "[PAPER] Trailing stop triggered: position #%d closed @ %.4f  P&L=$%.2f",
            pos_id, exit_price, pnl,
        )
        return

    contracts = _fetch_live_contract_count(ticker)
    if not contracts or contracts <= 0:
        logger.warning(
            "Trailing stop triggered for position #%d but no live Kalshi position "
            "found for %s — skipping close this cycle", pos_id, ticker,
        )
        return

    from execution.kalshi_executor import close_position
    order_id, status, reason, filled, fill_price, exit_fee = close_position(
        ticker, side, contracts, exit_price,
    )
    if status != "submitted" or filled <= 0:
        logger.warning(
            "Trailing stop close FAILED for position #%d (%s): %s — will retry next scan",
            pos_id, ticker, reason,
        )
        return

    if filled < contracts:
        # Partial-fill accounting isn't automated (v1 is a full-position-exit design,
        # not a partial-position ledger) — leave the position open and flag loudly
        # for manual review rather than silently misrecord P&L.
        logger.error(
            "Trailing stop PARTIAL fill for position #%d (%s): closed %g/%g contracts "
            "@ %.4f (order_id=%s). Position left OPEN for manual review — remaining "
            "contracts are still exposed on Kalshi.",
            pos_id, ticker, filled, contracts, fill_price, order_id,
        )
        return

    pnl = close_position_early(pos_id, fill_price, reason="trailing_stop", exit_fee=exit_fee)
    logger.info(
        "[LIVE] Trailing stop triggered: position #%d closed %g contracts @ %.4f  "
        "P&L=$%.2f  (order_id=%s, exit_fee=$%.4f)",
        pos_id, filled, fill_price, pnl, order_id, exit_fee,
    )
