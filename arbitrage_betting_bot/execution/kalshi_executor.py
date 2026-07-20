"""
Places orders on Kalshi via the REST API v2 orders endpoint.

Authentication: RSA-signed requests (KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH).
See data/kalshi_auth.py for signing details.

Order model (v2):
  - side:               "bid" (buy YES) or "ask" (sell YES = buy NO)
  - price:              fixed-point dollar string, e.g. "0.4000" (YES price)
  - count:              fixed-point contract count string, e.g. "25.00"
  - time_in_force:      "fill_or_kill" — fill immediately or cancel (market order equivalent)

Side mapping from our internal yes/no convention:
  yes → bid,  price = yes_ask (market_price as passed in)
  no  → ask,  price = 1 - no_ask  (convert no price to the YES price we're selling at)
"""
from __future__ import annotations

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

_ORDERS_URL = "https://external-api.kalshi.com/trade-api/v2/portfolio/orders"


def _get_order_status(order_id: str) -> dict | None:
    """Fetch current order status from Kalshi. Returns order dict or None on error."""
    try:
        from data.kalshi_auth import auth_headers
        url = f"{_ORDERS_URL}/{order_id}"
        headers = auth_headers("GET", url)
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json().get("order", {})
    except Exception as e:
        logger.warning("Could not fetch order status for %s: %s", order_id, e)
        return None


def _cancel_order(order_id: str) -> bool:
    """Cancel a resting Kalshi order. Returns True if cancelled successfully."""
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
) -> dict:
    """Post a single order to Kalshi. Returns the API response dict."""
    from data.kalshi_auth import auth_headers
    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": api_side,
        "price": f"{yes_price:.4f}",
        "count": f"{count:.2f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
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
    maker_only: bool = False,
    commence_time: datetime | None = None,
) -> tuple[str, str, str, float, str]:
    """
    Place a Kalshi order using the v2 orders endpoint.

    Args:
        ticker:         Kalshi market ticker
        side:           "yes" or "no" (internal convention)
        stake_dollars:  dollar amount to wager
        market_price:   ask price of the side we're buying (0.0 – 1.0)
        kalshi_spread:  bid-ask spread in dollars (used to decide limit vs IOC)
        maker_only:     if True, do not fall back to IOC when limit order expires unfilled
        commence_time:  game start time (UTC) — used to compute adaptive limit timeout

    Returns:
        (order_id, execution_status, failure_reason, actual_stake, fill_type)
        execution_status: "submitted" | "failed"
        failure_reason: empty string on success, human-readable error on failure
        actual_stake: dollars actually filled (filled_count × price); 0.0 on failure
        fill_type: "taker" (IOC fill) | "maker" (limit fill) | "" on failure

    Execution strategy (all orders are GTC — Kalshi charges 0% fee on all GTC):
        Step 1: GTC at mid price, adaptive timeout (2–10 min based on game time).
        Step 2: GTC at ask price, short timeout (30 s) — fills fast against existing
                liquidity at 0% fee. Skipped when maker_only=True (edge is only
                sufficient at mid price; ask price would be below threshold).
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0, ""

    price = max(0.01, min(0.99, market_price))
    count = max(1, math.floor(stake_dollars / price))

    if side == "yes":
        api_side = "bid"
        yes_price_ask = price
        # Mid price is half-spread below the ask
        yes_price_mid = max(0.01, price - kalshi_spread / 2.0)
    else:
        api_side = "ask"
        yes_price_ask = 1.0 - price
        yes_price_mid = min(0.99, yes_price_ask + kalshi_spread / 2.0)

    # ── Try limit order at mid price first — saves taker fee and gets better fill ─
    timeout = _limit_timeout(commence_time)
    client_order_id = str(uuid.uuid4())
    limit_attempted = False
    try:
        data = _place_raw_order(ticker, api_side, yes_price_mid, count, "gtc", client_order_id)
        limit_attempted = True
        order = data.get("order", {})
        order_id = order.get("order_id", client_order_id)
        filled = float(order.get("filled_count", 0) or 0)

        if filled >= count:
            actual_stake = round(filled * price, 2)
            logger.info(
                "Kalshi limit fill (immediate): %s %s %g contracts @ %.4f mid  actual_stake=$%.2f",
                api_side.upper(), ticker, filled, yes_price_mid, actual_stake,
            )
            return order_id, "submitted", "", actual_stake, "maker"

        # Poll for fill up to adaptive timeout
        deadline = time.time() + timeout
        while time.time() < deadline and filled < count:
            time.sleep(5)
            status = _get_order_status(order_id)
            if status:
                filled = float(status.get("filled_count", 0) or 0)
                if filled >= count:
                    break

        if filled > 0:
            actual_stake = round(filled * price, 2)
            logger.info(
                "Kalshi limit fill: %s %s %g/%d contracts @ %.4f mid  actual_stake=$%.2f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price_mid, actual_stake, order_id,
            )
            _cancel_order(order_id)
            return order_id, "submitted", "", actual_stake, "maker"

        _cancel_order(order_id)
        if maker_only:
            reason = f"GTC mid unfilled after {timeout}s — edge insufficient at ask price, giving up"
            logger.info("Kalshi GTC mid unfilled for %s — mid_only, not trying ask step", ticker)
            return order_id, "failed", reason, 0.0, ""
        logger.info(
            "Kalshi GTC mid unfilled after %ds — trying GTC at ask for %s",
            timeout, ticker,
        )
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.warning("Kalshi GTC mid failed [%s] for %s — %s",
                       code, ticker, "giving up (mid_only)" if maker_only else "trying ask step")
        if maker_only:
            return client_order_id, "failed", f"GTC mid HTTP {code} — mid_only, no ask fallback", 0.0, ""
    except requests.RequestException as e:
        logger.warning("GTC mid request error for %s — %s: %s",
                       ticker, "giving up (mid_only)" if maker_only else "trying ask step", e)
        if maker_only:
            return client_order_id, "failed", f"GTC mid network error — mid_only, no ask fallback", 0.0, ""

    # ── Step 2: GTC at ask — 0% maker fee, fills fast against existing liquidity ─
    # Kalshi classifies ALL GTC orders as maker regardless of immediate crossing.
    ask_timeout = config.LIMIT_ORDER_ASK_TIMEOUT_SECONDS
    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price_ask, count, "gtc", client_order_id)
        order = data.get("order", {})
        order_id = order.get("order_id", client_order_id)
        filled = float(order.get("filled_count", 0) or 0)

        if filled >= count:
            actual_stake = round(filled * price, 2)
            logger.info(
                "Kalshi GTC ask fill (immediate): %s %s %g contracts @ %.4f  actual_stake=$%.2f",
                api_side.upper(), ticker, filled, yes_price_ask, actual_stake,
            )
            return order_id, "submitted", "", actual_stake, "maker"

        deadline = time.time() + ask_timeout
        while time.time() < deadline and filled < count:
            time.sleep(5)
            status = _get_order_status(order_id)
            if status:
                filled = float(status.get("filled_count", 0) or 0)
                if filled >= count:
                    break

        if filled > 0:
            actual_stake = round(filled * price, 2)
            logger.info(
                "Kalshi GTC ask fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price_ask, actual_stake, order_id,
            )
            _cancel_order(order_id)
            return order_id, "submitted", "", actual_stake, "maker"

        _cancel_order(order_id)
        reason = f"GTC ask unfilled after {ask_timeout}s — no resting liquidity at ask"
        logger.warning("Kalshi GTC ask zero fill for %s @ %.4f", ticker, yes_price_ask)
        return order_id, "failed", reason, 0.0, ""
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        logger.error("Kalshi GTC ask failed [%s]: %s", code, body)
        reason = f"HTTP {code}"
        try:
            import json as _json
            err = _json.loads(body)
            if "message" in err:
                reason = f"HTTP {code}: {err['message']}"
            elif "error" in err:
                reason = f"HTTP {code}: {err['error']}"
        except Exception:
            if body:
                reason = f"HTTP {code}: {body[:200]}"
        return client_order_id, "failed", reason, 0.0, ""
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi GTC ask request error: %s", e)
        return client_order_id, "failed", reason, 0.0, ""
