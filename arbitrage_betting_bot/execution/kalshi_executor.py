"""
Places orders on Kalshi via the REST API v2 orders endpoints.

Authentication: RSA-signed requests (KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH).
See data/kalshi_auth.py for signing details.

Endpoints (as of July 2026):
  POST   /portfolio/events/orders            — create order (new v2 path)
  GET    /portfolio/orders/{id}              — check status (old path still works)
  DELETE /portfolio/events/orders/{id}       — cancel order (new v2 path)

Order model:
  - side:           "bid" (buy YES) or "ask" (sell YES = buy NO)
  - price:          fixed-point dollar string, e.g. "0.4000" (YES price)
  - count:          fixed-point contract count string, e.g. "25.00"
  - time_in_force:  "good_till_canceled" (American spelling, one L) — resting limit order

Side mapping from our internal yes/no convention:
  yes → bid,  price = yes_ask (market_price as passed in)
  no  → ask,  price = 1 - no_ask  (convert no price to the YES price we're selling at)

Fee model:
  Kalshi charges a real fee whenever an order crosses the spread at placement (an
  economic "taker" action), regardless of GTC vs IOC order type — step 2 below ("GTC
  at ask") is designed to cross immediately and often does incur this fee. The actual
  fee charged is read directly from the order's maker_fees_dollars/taker_fees_dollars
  fields (GET /portfolio/orders/{id}) after every fill rather than inferred/assumed,
  since guessing this got it wrong before (see _actual_fee_dollars()).

Execution strategy:
  Step 1: GTC at mid price, adaptive timeout (2–10 min based on game time).
  Step 2: GTC at ask price, short timeout (30 s).
"""
from __future__ import annotations

import json as _json
import logging
import math
import time
import uuid
from datetime import datetime, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_BASE_URL     = "https://api.elections.kalshi.com/trade-api/v2"
_ORDERS_URL   = f"{_BASE_URL}/portfolio/events/orders"   # POST + DELETE
_STATUS_URL   = f"{_BASE_URL}/portfolio/orders"          # GET (old path still works)


def _get_order_status(order_id: str) -> dict | None:
    """Fetch current order status. Returns order dict or None on error."""
    try:
        from data.kalshi_auth import auth_headers
        url = f"{_STATUS_URL}/{order_id}"
        headers = auth_headers("GET", url)
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("order", {})
    except Exception as e:
        logger.warning("Could not fetch order status for %s: %s", order_id, e)
        return None


def _actual_fee_dollars(order_id: str) -> float:
    """
    The real fee Kalshi charged for an order (maker + taker combined), read directly
    from the order's own record. Do not infer/assume this — an earlier version of this
    code hardcoded "always maker, 0% fee" and was wrong for any fill that crossed the
    spread at placement.
    """
    status = _get_order_status(order_id)
    if not status:
        return 0.0
    maker_fee = float(status.get("maker_fees_dollars", 0) or 0)
    taker_fee = float(status.get("taker_fees_dollars", 0) or 0)
    return round(maker_fee + taker_fee, 6)


def _cancel_order(order_id: str) -> bool:
    """Cancel a resting GTC order. Returns True if cancelled successfully."""
    try:
        from data.kalshi_auth import auth_headers
        url = f"{_ORDERS_URL}/{order_id}"
        headers = auth_headers("DELETE", url)
        resp = requests.delete(url, headers=headers, timeout=10)
        return resp.ok
    except Exception as e:
        logger.warning("Could not cancel order %s: %s", order_id, e)
        return False


def _place_raw_order(
    ticker: str,
    api_side: str,
    yes_price: float,
    count: int,
    time_in_force: str,
    client_order_id: str,
    reduce_only: bool = False,
) -> dict:
    """
    Post a single order to Kalshi. Returns the API response dict.

    reduce_only: when True, the order can only shrink/close an existing position —
    it can never open new exposure on the other side. Used for risk-management exits
    (see execution/risk_manager.py); entries always leave this False.
    """
    from data.kalshi_auth import auth_headers
    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": api_side,
        "price": f"{yes_price:.4f}",
        "count": f"{count:.2f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": reduce_only,
    }
    headers = auth_headers("POST", _ORDERS_URL)
    resp = requests.post(_ORDERS_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _limit_timeout(commence_time: datetime | None) -> int:
    """Return the appropriate GTC limit-order timeout in seconds based on time to game."""
    if commence_time is None:
        return config.LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS
    now = datetime.now(timezone.utc)
    if commence_time.tzinfo is None:
        commence_time = commence_time.replace(tzinfo=timezone.utc)
    minutes_to_game = (commence_time - now).total_seconds() / 60.0
    if minutes_to_game <= config.NEAR_GAME_THRESHOLD_MINUTES:
        return config.LIMIT_ORDER_TIMEOUT_NEAR_GAME_SECONDS
    if minutes_to_game <= config.PRE_GAME_THRESHOLD_HOURS * 60:
        return config.LIMIT_ORDER_TIMEOUT_PRE_GAME_SECONDS
    return config.LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS


def place_order(
    ticker: str,
    side: str,
    stake_dollars: float,
    market_price: float,
    kalshi_spread: float = 0.0,
    commence_time: datetime | None = None,
) -> tuple[str, str, str, float, str, float]:
    """
    Place a Kalshi order using GTC limit orders.

    Args:
        ticker:         Kalshi market ticker
        side:           "yes" or "no" (internal convention)
        stake_dollars:  dollar amount to wager
        market_price:   ask price of the side we're buying (0.0 – 1.0)
        kalshi_spread:  bid-ask spread in dollars (used to compute mid price)
        commence_time:  game start time (UTC) — used to compute adaptive limit timeout

    Returns:
        (order_id, execution_status, failure_reason, actual_stake, fill_type, fee_paid)
        execution_status: "submitted" | "failed"
        failure_reason:   empty string on success, human-readable error on failure
        actual_stake:     dollars actually filled; 0.0 on failure
        fill_type:        "maker" | "taker" on success (derived from fee_paid) | "" on failure
        fee_paid:         actual dollars charged by Kalshi for this fill; 0.0 on failure

    Execution strategy:
        Step 1: GTC at mid price, adaptive timeout (2–10 min based on game time).
        Step 2: GTC at ask price, short timeout (30 s) — this step crosses the book
                immediately by design and often incurs a real taker fee.
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0, "", 0.0

    price = max(0.01, min(0.99, market_price))
    count = max(1, math.floor(stake_dollars / price))

    if side == "yes":
        api_side = "bid"
        yes_price_ask = price
        # Round to nearest cent — Kalshi prices are on a 1¢ grid (step=0.01)
        yes_price_mid = round(max(0.01, price - kalshi_spread / 2.0), 2)
    else:
        api_side = "ask"
        yes_price_ask = 1.0 - price
        yes_price_mid = round(min(0.99, yes_price_ask + kalshi_spread / 2.0), 2)

    # ── Step 1: GTC at mid price ──────────────────────────────────────────────
    timeout = _limit_timeout(commence_time)
    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price_mid, count, "good_till_canceled", client_order_id)
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)

        if filled >= count:
            actual_stake = round(filled * price, 2)
            fee_paid = _actual_fee_dollars(order_id)
            fill_type = "taker" if fee_paid > 0 else "maker"
            logger.info(
                "Kalshi GTC mid fill (immediate): %s %s %g contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f",
                api_side.upper(), ticker, filled, yes_price_mid, actual_stake, fee_paid,
            )
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        # Poll for fill up to adaptive timeout
        deadline = time.time() + timeout
        while time.time() < deadline and filled < count:
            time.sleep(5)
            status = _get_order_status(order_id)
            if status:
                filled = float(status.get("fill_count_fp", 0) or 0)
                if filled >= count:
                    break

        if filled > 0:
            actual_stake = round(filled * price, 2)
            fee_paid = _actual_fee_dollars(order_id)
            fill_type = "taker" if fee_paid > 0 else "maker"
            logger.info(
                "Kalshi GTC mid fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price_mid, actual_stake, fee_paid, order_id,
            )
            _cancel_order(order_id)
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        _cancel_order(order_id)
        logger.info(
            "Kalshi GTC mid unfilled after %ds — trying GTC at ask for %s",
            timeout, ticker,
        )
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        logger.warning("Kalshi GTC mid failed [%s] for %s — trying ask step", code, ticker)
        logger.debug("GTC mid error body: %s", body[:300])
    except requests.RequestException as e:
        logger.warning("GTC mid network error for %s — trying ask step: %s", ticker, e)

    # ── Step 2: GTC at ask price ──────────────────────────────────────────────
    ask_timeout = config.LIMIT_ORDER_ASK_TIMEOUT_SECONDS
    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price_ask, count, "good_till_canceled", client_order_id)
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)

        if filled >= count:
            actual_stake = round(filled * price, 2)
            fee_paid = _actual_fee_dollars(order_id)
            fill_type = "taker" if fee_paid > 0 else "maker"
            logger.info(
                "Kalshi GTC ask fill (immediate): %s %s %g contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f",
                api_side.upper(), ticker, filled, yes_price_ask, actual_stake, fee_paid,
            )
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        deadline = time.time() + ask_timeout
        while time.time() < deadline and filled < count:
            time.sleep(5)
            status = _get_order_status(order_id)
            if status:
                filled = float(status.get("fill_count_fp", 0) or 0)
                if filled >= count:
                    break

        if filled > 0:
            actual_stake = round(filled * price, 2)
            fee_paid = _actual_fee_dollars(order_id)
            fill_type = "taker" if fee_paid > 0 else "maker"
            logger.info(
                "Kalshi GTC ask fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price_ask, actual_stake, fee_paid, order_id,
            )
            _cancel_order(order_id)
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        _cancel_order(order_id)
        reason = f"GTC ask unfilled after {ask_timeout}s — no resting liquidity at ask"
        logger.warning("Kalshi GTC ask zero fill for %s @ %.4f", ticker, yes_price_ask)
        return order_id, "failed", reason, 0.0, "", 0.0

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        reason = f"HTTP {code}"
        try:
            err = _json.loads(body)
            err_obj = err.get("error", err)
            msg = err_obj.get("message", "")
            if msg:
                reason = f"HTTP {code}: {msg}"
        except Exception:
            if body:
                reason = f"HTTP {code}: {body[:200]}"
        logger.error("Kalshi GTC ask failed [%s]: %s", code, body[:300])
        return client_order_id, "failed", reason, 0.0, "", 0.0
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi GTC ask network error: %s", e)
        return client_order_id, "failed", reason, 0.0, "", 0.0


def close_position(
    ticker: str,
    side: str,
    count: int,
    exit_price: float,
    timeout_seconds: int = 30,
) -> tuple[str, str, str, float, float, float]:
    """
    Close (all of) an existing position via a single reduce_only GTC order — used by
    the trailing-stop risk manager, not the entry flow. Unlike place_order(), this is
    a single attempt at the requested price (no mid-then-ask two-step): the whole
    point of an exit is to lock in protection now, not to optimize entry price at the
    cost of further slippage while the position sits exposed.

    Args:
        side:       "yes" or "no" — the side of the EXISTING position being closed
                    (same convention as place_order's side param at entry).
        count:      contracts to close.
        exit_price: desired price of the side being closed (0-1), same convention as
                    market_price (e.g. current yes_bid for a yes position).

    Returns:
        (order_id, execution_status, failure_reason, filled_count, fill_price, fee_paid)
        execution_status: "submitted" | "failed"
        fee_paid: actual dollars Kalshi charged for this close, read from the order's
                  own record (see _actual_fee_dollars) — closing orders can incur a
                  real fee just like entries.
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot close position")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0, 0.0, 0.0

    price = max(0.01, min(0.99, exit_price))
    if side == "yes":
        api_side = "ask"   # selling YES closes a long-YES position
        yes_price = price
    else:
        api_side = "bid"   # buying YES back closes a short-YES (NO) position
        yes_price = 1.0 - price

    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(
            ticker, api_side, yes_price, count, "good_till_canceled",
            client_order_id, reduce_only=True,
        )
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)

        if filled < count:
            deadline = time.time() + timeout_seconds
            while time.time() < deadline and filled < count:
                time.sleep(5)
                status = _get_order_status(order_id)
                if status:
                    filled = float(status.get("fill_count_fp", 0) or 0)
                    if filled >= count:
                        break
            if filled < count:
                _cancel_order(order_id)

        if filled <= 0:
            reason = f"Close order unfilled after {timeout_seconds}s — no resting liquidity"
            logger.warning("Kalshi close zero fill for %s @ %.4f", ticker, yes_price)
            return order_id, "failed", reason, 0.0, 0.0, 0.0

        fee_paid = _actual_fee_dollars(order_id)
        logger.info(
            "Kalshi position close: %s %s %g/%d contracts @ %.4f  fee=$%.4f",
            api_side.upper(), ticker, filled, count, yes_price, fee_paid,
        )
        return order_id, "submitted", "", filled, price, fee_paid

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        reason = f"HTTP {code}"
        try:
            err = _json.loads(body)
            err_obj = err.get("error", err)
            msg = err_obj.get("message", "")
            if msg:
                reason = f"HTTP {code}: {msg}"
        except Exception:
            if body:
                reason = f"HTTP {code}: {body[:200]}"
        logger.error("Kalshi close failed [%s]: %s", code, body[:300])
        return client_order_id, "failed", reason, 0.0, 0.0, 0.0
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi close network error: %s", e)
        return client_order_id, "failed", reason, 0.0, 0.0, 0.0
