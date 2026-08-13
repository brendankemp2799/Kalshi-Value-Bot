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
        from data.kalshi_auth import auth_headers, session
        url = "https://external-api.kalshi.com/trade-api/v2/portfolio/positions"
        headers = auth_headers("GET", url)
        resp = session().get(url, headers=headers, timeout=10, params={"limit": 1000})
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


# ── Ghost-position audit ─────────────────────────────────────────────────────────
#
# Built 2026-07-23 after finding 4 positions whose `order_id` was actually a
# client-side UUID (our own locally-generated order identifier) rather than
# Kalshi's real assigned order_id — each one double-counted a real trade under a
# second, fictitious position with the wrong stake. Confirmed by querying Kalshi's
# order-status endpoint directly (404 = not a real order). This check catches the
# same pattern going forward: verify every new real position's order_id actually
# appears in Kalshi's own fill history.

def _all_real_order_ids() -> set[str] | None:
    """Every order_id Kalshi has ever recorded a fill for on this account. Paginated —
    not credit-metered, but only worth fetching when there's something to check
    against it (see audit_order_ids' cheap early-exit). None on fetch failure."""
    try:
        from data.kalshi_auth import auth_headers, session
        url = "https://external-api.kalshi.com/trade-api/v2/portfolio/fills"
        order_ids: set[str] = set()
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            headers = auth_headers("GET", url)
            resp = session().get(url, headers=headers, timeout=15, params=params)
            resp.raise_for_status()
            data = resp.json()
            fills = data.get("fills", [])
            order_ids.update(f["order_id"] for f in fills)
            cursor = data.get("cursor")
            if not cursor or not fills:
                break
        return order_ids
    except Exception as e:
        logger.warning("Ghost-position audit: could not fetch Kalshi fills: %s", e)
        return None


def audit_order_ids() -> None:
    """
    Verify every not-yet-checked real position's order_id against Kalshi's actual
    fill history, flag any that don't match (loudly — for manual review, this never
    auto-deletes), then mark all of them checked either way so this doesn't re-fetch
    Kalshi's full fill history (which only grows) on every scan once a position has
    already been verified once.
    """
    from storage.db import get_unverified_real_positions, mark_order_verified

    pending = get_unverified_real_positions()
    if not pending:
        return  # cheap common case — no Kalshi call needed

    real_order_ids = _all_real_order_ids()
    if real_order_ids is None:
        return  # transient fetch failure — leave unverified, retry next scan

    ghosts = []
    for pos in pending:
        if pos["order_id"] not in real_order_ids:
            ghosts.append(pos)
        mark_order_verified(pos["id"])

    if ghosts:
        logger.critical(
            "GHOST POSITION(S) DETECTED — order_id doesn't match any real Kalshi "
            "order for %d position(s). Not auto-deleted — needs manual review:\n%s",
            len(ghosts),
            "\n".join(
                f"  #{g['id']} {g['market_ticker']} stake=${g['stake']:.2f} order_id={g['order_id']}"
                for g in ghosts
            ),
        )
