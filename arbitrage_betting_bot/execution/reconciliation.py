"""
Reconciliation safety net: compares our tracked open live positions against
Kalshi's own authoritative portfolio data every live scan cycle.

Built after discovering that a fill-recording bug could leave real Kalshi orders
completely untracked in our DB for days without any signal that something was
wrong. Kalshi's portfolio API isn't credit-metered, so this check is free to run
every cycle. Live-only — paper mode has no real Kalshi positions to compare
against, and this must never run from the dashboard (it only reads, but keeping
it on the same gating as the other live-only checks avoids the dashboard's 60s
cadence doing anything unnecessary).
"""
from __future__ import annotations

import logging
from collections import defaultdict

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logger = logging.getLogger(__name__)

TOLERANCE_CONTRACTS = 0.5  # rounding/fee slack before flagging a mismatch


def _kalshi_position_by_ticker() -> dict[str, float] | None:
    """
    Authoritative signed contract count per ticker from Kalshi's own portfolio
    data (positive = long YES, negative = short YES / holding NO). Returns None
    on fetch failure so callers can skip the check rather than false-alarm.
    """
    try:
        from data.kalshi_auth import auth_headers
        import requests
        url = "https://external-api.kalshi.com/trade-api/v2/portfolio/positions"
        headers = auth_headers("GET", url)
        resp = requests.get(url, headers=headers, timeout=10, params={"limit": 1000})
        resp.raise_for_status()
        by_ticker: dict[str, float] = {}
        for p in resp.json().get("market_positions", []):
            pos_fp = float(p.get("position_fp", 0) or 0)
            if pos_fp != 0:
                by_ticker[p["ticker"]] = pos_fp
        return by_ticker
    except Exception as e:
        logger.warning("Reconciliation: could not fetch Kalshi positions: %s", e)
        return None


def _our_position_by_ticker(is_paper: bool = False) -> dict[str, float]:
    """Same signed-contract-count convention as Kalshi, derived from our own open positions."""
    from storage.db import get_open_positions

    by_ticker: dict[str, float] = defaultdict(float)
    for pos in get_open_positions(is_paper=is_paper):
        ticker = pos["market_ticker"]
        side = (pos["side"] or "").lower()
        price = pos["market_price"]
        if not ticker or side not in ("yes", "no") or not price:
            continue
        contracts = pos["stake"] / price
        by_ticker[ticker] += contracts if side == "yes" else -contracts
    return dict(by_ticker)


def reconcile_with_kalshi() -> list[str]:
    """
    Returns a list of human-readable discrepancy descriptions (empty if our
    records and Kalshi's agree, within TOLERANCE_CONTRACTS, on every ticker).
    """
    kalshi = _kalshi_position_by_ticker()
    if kalshi is None:
        return []  # couldn't fetch — don't false-alarm on a transient API error

    ours = _our_position_by_ticker(is_paper=False)

    discrepancies = []
    for ticker in sorted(set(kalshi) | set(ours)):
        k = kalshi.get(ticker, 0.0)
        o = ours.get(ticker, 0.0)
        if abs(k - o) > TOLERANCE_CONTRACTS:
            discrepancies.append(
                f"{ticker}: Kalshi shows {k:+.1f} contracts, we track {o:+.1f} (diff {k - o:+.1f})"
            )
    return discrepancies


def run_reconciliation_check() -> None:
    """Log a loud warning for any live-position mismatch vs Kalshi's own records."""
    try:
        discrepancies = reconcile_with_kalshi()
    except Exception as e:
        logger.warning("Reconciliation check failed: %s", e)
        return
    if discrepancies:
        logger.error(
            "RECONCILIATION MISMATCH — our records disagree with Kalshi's live "
            "positions for %d ticker(s):\n%s",
            len(discrepancies), "\n".join(f"  {d}" for d in discrepancies),
        )
