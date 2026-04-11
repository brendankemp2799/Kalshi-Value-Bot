"""
Executes a Kalshi order for a given ValueOpportunity.

Usage (from main.py):
    from execution.trade_executor import execute_trade
    order_id, status = execute_trade(opp, sizing)
"""
from __future__ import annotations

import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.value_detector import ValueOpportunity, Outcome
from core.kelly_calculator import BetSizing

logger = logging.getLogger(__name__)


def resolve_side(opp: ValueOpportunity) -> str:
    """Return the Kalshi side ('yes' or 'no') to bet for this opportunity."""
    me = opp.matched_event
    # NO_OVER = buying the NO (Under) side of an Over market
    if opp.outcome == Outcome.NO_OVER:
        return "no"
    # All other non-H2H outcomes (totals YES side, spread cover, draw) buy YES
    if opp.outcome in (Outcome.DRAW, Outcome.OVER, Outcome.UNDER, Outcome.COVER):
        return "yes"
    if opp.outcome == Outcome.HOME:
        return me.kalshi_outcome or "yes"
    # AWAY
    return "no" if (me.kalshi_outcome or "yes") == "yes" else "yes"


def execute_trade(opp: ValueOpportunity, sizing: BetSizing) -> tuple[str, str, str]:
    """
    Place a live Kalshi order for the given opportunity.

    Returns (order_id, execution_status, side).
    execution_status: "submitted" | "failed"
    side: "yes" | "no"
    """
    from execution import kalshi_executor

    me = opp.matched_event
    km = me.kalshi_market
    side = resolve_side(opp)

    order_id, status = kalshi_executor.place_order(
        ticker=km.ticker,
        side=side,
        stake_dollars=sizing.recommended_dollars,
        market_price=opp.market_price,
    )
    return order_id, status, side
