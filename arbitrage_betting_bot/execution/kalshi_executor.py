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


def place_order(
    ticker: str,
    side: str,
    stake_dollars: float,
    market_price: float,
    kalshi_spread: float = 0.0,
) -> tuple[str, str, str, float]:
    """
    Place a Kalshi order using the v2 orders endpoint.

    Args:
        ticker:         Kalshi market ticker
        side:           "yes" or "no" (internal convention)
        stake_dollars:  dollar amount to wager
        market_price:   ask price of the side we're buying (0.0 – 1.0)
        kalshi_spread:  bid-ask spread in dollars (used to decide limit vs IOC)

    Returns:
        (order_id, execution_status, failure_reason, actual_stake)
        execution_status: "submitted" | "failed"
        failure_reason: empty string on success, human-readable error on failure
        actual_stake: dollars actually filled (filled_count × price); 0.0 on failure

    Execution strategy:
        - If spread ≥ LIMIT_ORDER_SPREAD_THRESHOLD: post a GTC limit at mid price,
          wait up to LIMIT_ORDER_TIMEOUT_SECONDS for fill, then cancel and fall back to IOC.
        - Otherwise: place an IOC order at the ask (existing behaviour).
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0

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

    # ── Try limit order at mid price when spread is wide enough to save money ─
    use_limit = kalshi_spread >= config.LIMIT_ORDER_SPREAD_THRESHOLD
    if use_limit:
        client_order_id = str(uuid.uuid4())
        try:
            data = _place_raw_order(ticker, api_side, yes_price_mid, count, "gtc", client_order_id)
            order = data.get("order", {})
            order_id = order.get("order_id", client_order_id)
            filled = float(order.get("filled_count", 0) or 0)

            if filled >= count:
                # Immediate full fill at mid price
                actual_stake = round(filled * price, 2)
                logger.info(
                    "Kalshi limit fill (immediate): %s %s %g contracts @ %.4f mid  actual_stake=$%.2f",
                    api_side.upper(), ticker, filled, yes_price_mid, actual_stake,
                )
                return order_id, "submitted", "", actual_stake

            # Poll for fill up to timeout
            deadline = time.time() + config.LIMIT_ORDER_TIMEOUT_SECONDS
            while time.time() < deadline and filled < count:
                time.sleep(5)
                status = _get_order_status(order_id)
                if status:
                    filled = float(status.get("filled_count", 0) or 0)
                    if filled >= count:
                        break

            if filled > 0:
                # Partial or full fill achieved
                actual_stake = round(filled * price, 2)
                logger.info(
                    "Kalshi limit fill: %s %s %g/%d contracts @ %.4f mid  actual_stake=$%.2f (order_id=%s)",
                    api_side.upper(), ticker, filled, count, yes_price_mid, actual_stake, order_id,
                )
                # Cancel any remaining resting quantity
                _cancel_order(order_id)
                return order_id, "submitted", "", actual_stake

            # No fill — cancel and fall through to IOC at ask
            _cancel_order(order_id)
            logger.info(
                "Kalshi limit order unfilled after %ds — falling back to IOC at ask for %s",
                config.LIMIT_ORDER_TIMEOUT_SECONDS, ticker,
            )
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            logger.warning("Kalshi limit order failed [%s] — falling back to IOC for %s", code, ticker)
        except requests.RequestException as e:
            logger.warning("Limit order request error — falling back to IOC for %s: %s", ticker, e)

    # ── IOC at ask (market order equivalent) ─────────────────────────────────
    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price_ask, count, "immediate_or_cancel", client_order_id)
        order = data.get("order", {})
        order_id = order.get("order_id", client_order_id)
        filled = float(order.get("filled_count", 0) or 0)

        if filled == 0:
            reason = "No resting volume — order cancelled with zero fill"
            logger.warning("Kalshi IOC zero fill: %s %s %d contracts @ %.4f", api_side.upper(), ticker, count, yes_price_ask)
            return order_id, "failed", reason, 0.0

        actual_stake = round(filled * price, 2)
        if filled < count:
            logger.warning(
                "Kalshi IOC partial fill: %g/%d contracts ($%.2f of $%.2f) for %s %s @ %.4f",
                filled, count, actual_stake, stake_dollars, ticker, api_side.upper(), yes_price_ask,
            )
        logger.info(
            "Kalshi IOC filled: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f  (order_id=%s)",
            api_side.upper(), ticker, filled, count, yes_price_ask, actual_stake, order_id,
        )
        return order_id, "submitted", "", actual_stake
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        logger.error("Kalshi order failed [%s]: %s", code, body)
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
        return client_order_id, "failed", reason, 0.0
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi order request error: %s", e)
        return client_order_id, "failed", reason, 0.0
