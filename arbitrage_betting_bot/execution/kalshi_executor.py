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
import uuid

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_ORDERS_URL = "https://external-api.kalshi.com/trade-api/v2/portfolio/events/orders"


def place_order(
    ticker: str,
    side: str,
    stake_dollars: float,
    market_price: float,
) -> tuple[str, str, str]:
    """
    Place a Kalshi order using the v2 orders endpoint.

    Args:
        ticker:        Kalshi market ticker
        side:          "yes" or "no" (internal convention)
        stake_dollars: dollar amount to wager
        market_price:  ask price of the side we're buying (0.0 – 1.0)

    Returns:
        (order_id, execution_status, failure_reason)
        execution_status: "submitted" | "failed"
        failure_reason: empty string on success, human-readable error on failure
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured"

    # Contract count based on price of the side we're buying
    price = max(0.01, min(0.99, market_price))
    count = max(1, math.floor(stake_dollars / price))

    # v2 API uses bid/ask (YES perspective) instead of yes/no
    if side == "yes":
        api_side = "bid"
        yes_price = price
    else:
        api_side = "ask"
        yes_price = 1.0 - price  # convert no_ask → yes_bid (price we sell YES at)

    client_order_id = str(uuid.uuid4())
    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": api_side,
        "price": f"{yes_price:.4f}",
        "count": f"{count:.2f}",
        "time_in_force": "fill_or_kill",
        "self_trade_prevention_type": "taker_at_cross",
    }

    try:
        from data.kalshi_auth import auth_headers
        headers = auth_headers("POST", _ORDERS_URL)
        resp = requests.post(_ORDERS_URL, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        order_id = data.get("order", {}).get("order_id", client_order_id)
        logger.info(
            "Kalshi order submitted: %s %s %d contracts @ %.4f  (order_id=%s)",
            api_side.upper(), ticker, count, yes_price, order_id,
        )
        return order_id, "submitted", ""
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        logger.error("Kalshi order failed [%s]: %s", code, body)
        # Parse Kalshi error message if JSON, else use raw body (truncated)
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
        return client_order_id, "failed", reason
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi order request error: %s", e)
        return client_order_id, "failed", reason
