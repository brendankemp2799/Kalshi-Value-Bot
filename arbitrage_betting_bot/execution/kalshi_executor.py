"""
Places market orders on Kalshi via the REST API v2.

Authentication: RSA-signed requests (KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH).
See data/kalshi_auth.py for signing details.

Order model:
  - type: "market"  — fill at best available price immediately
  - action: "buy"
  - side: "yes" or "no"
  - count: number of contracts  (each contract costs yes_price cents)
  - buy_max_cost: maximum total spend in cents (guards against slippage)

Kalshi contract math:
  price_cents = round(market_price * 100)   e.g. 0.40 → 40 cents
  count       = floor(stake_dollars / (price_cents / 100))
  max_cost    = stake_dollars * 100  (in cents, sets the spending cap)
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


def place_order(
    ticker: str,
    side: str,
    stake_dollars: float,
    market_price: float,
) -> tuple[str, str]:
    """
    Place a Kalshi market buy order.

    Args:
        ticker:        Kalshi market ticker (e.g. "NFLWC-24-DEN")
        side:          "yes" or "no"
        stake_dollars: dollar amount to wager
        market_price:  current market price as a probability (0.0 – 1.0),
                       used to convert dollars → contract count

    Returns:
        (order_id, execution_status)
        execution_status is "submitted" on success or "failed" on error.
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed"

    price_cents = max(1, min(99, round(market_price * 100)))
    count = max(1, math.floor(stake_dollars / (price_cents / 100)))
    buy_max_cost = math.ceil(stake_dollars * 100)   # cents, rounds up for safety

    client_order_id = str(uuid.uuid4())
    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "type": "market",
        "action": "buy",
        "side": side,
        "count": count,
        "buy_max_cost": buy_max_cost,
    }

    url = f"{config.KALSHI_API_BASE_URL}/portfolio/orders"

    try:
        from data.kalshi_auth import auth_headers
        headers = auth_headers("POST", url)
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        order_id = data.get("order", {}).get("order_id", client_order_id)
        logger.info(
            "Kalshi order submitted: %s %s %d contracts @ %d¢  (order_id=%s)",
            side.upper(), ticker, count, price_cents, order_id,
        )
        return order_id, "submitted"
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        logger.error("Kalshi order failed [%s]: %s", e.response.status_code if e.response else "?", body)
        return client_order_id, "failed"
    except requests.RequestException as e:
        logger.error("Kalshi order request error: %s", e)
        return client_order_id, "failed"
