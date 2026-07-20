"""
Places orders on Kalshi via the REST API v2 portfolio/events/orders endpoint.

Authentication: RSA-signed requests (KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH).
See data/kalshi_auth.py for signing details.

Order model (v2, as of July 2026):
  - side:               "bid" (buy YES) or "ask" (sell YES = buy NO)
  - price:              fixed-point dollar string, e.g. "0.4000" (YES price)
  - count:              fixed-point contract count string, e.g. "25.00"
  - time_in_force:      "immediate_or_cancel" — only IOC and FOK are supported;
                        GTC (resting limit) orders were removed from the retail
                        API on May 6, 2026.

Side mapping from our internal yes/no convention:
  yes → bid,  price = ask_price (market_price as passed in)
  no  → ask,  price = 1 - ask_price (convert no price to the YES price we're selling at)

Taker fee: KALSHI_TAKER_FEE_RATE × ask_price × (1-ask_price) per dollar staked.
"""
from __future__ import annotations

import logging
import json as _json
import math
import uuid
from datetime import datetime, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_ORDERS_URL = "https://api.elections.kalshi.com/trade-api/v2/portfolio/events/orders"


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
    maker_only: bool = False,
    commence_time: datetime | None = None,
) -> tuple[str, str, str, float, str]:
    """
    Place a Kalshi IOC order via the v2 events/orders endpoint.

    Args:
        ticker:         Kalshi market ticker
        side:           "yes" or "no" (internal convention)
        stake_dollars:  dollar amount to wager
        market_price:   ask price of the side we're buying (0.0 – 1.0)
        kalshi_spread:  unused (kept for call-site compatibility)
        maker_only:     unused (kept for call-site compatibility)
        commence_time:  unused (kept for call-site compatibility)

    Returns:
        (order_id, execution_status, failure_reason, actual_stake, fill_type)
        execution_status: "submitted" | "failed"
        failure_reason:   empty string on success, human-readable error on failure
        actual_stake:     dollars actually filled (filled_count × price); 0.0 on failure
        fill_type:        "taker" on success | "" on failure
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0, ""

    price = max(0.01, min(0.99, market_price))
    count = max(1, math.floor(stake_dollars / price))

    if side == "yes":
        api_side = "bid"
        yes_price = price
    else:
        api_side = "ask"
        yes_price = 1.0 - price

    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price, count, "immediate_or_cancel", client_order_id)
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)

        if filled > 0:
            actual_stake = round(filled * price, 2)
            logger.info(
                "Kalshi IOC fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price, actual_stake, order_id,
            )
            return order_id, "submitted", "", actual_stake, "taker"

        reason = f"IOC filled 0 contracts at {yes_price:.4f} — no resting liquidity at ask"
        logger.warning("Kalshi IOC zero fill for %s @ %.4f", ticker, yes_price)
        return order_id, "failed", reason, 0.0, ""

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
        logger.error("Kalshi IOC failed [%s]: %s", code, body[:300])
        return client_order_id, "failed", reason, 0.0, ""

    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi IOC request error: %s", e)
        return client_order_id, "failed", reason, 0.0, ""
