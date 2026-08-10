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


def execute_trade(opp: ValueOpportunity, sizing: BetSizing) -> tuple[str, str, str, str, float, str, float]:
    """
    Place a live Kalshi order for the given opportunity.

    Returns (order_id, execution_status, side, failure_reason, actual_stake, fill_type, fee_paid).
    execution_status: "submitted" | "failed"
    side: "yes" | "no"
    failure_reason: empty string on success, human-readable error on failure
    actual_stake: dollars actually filled (0.0 on failure)
    fill_type: "maker" | "taker" on success (derived from fee_paid) | "" on failure
    fee_paid: actual dollars Kalshi charged for this fill (0.0 on failure)
    """
    from execution import kalshi_executor

    me = opp.matched_event
    km = me.kalshi_market
    side = resolve_side(opp)

    order_id, status, reason, actual_stake, fill_type, fee_paid = kalshi_executor.place_order(
        ticker=km.ticker,
        side=side,
        stake_dollars=sizing.recommended_dollars,
        market_price=opp.market_price,
        kalshi_spread=km.spread,
        commence_time=me.odds_event.commence_time,
        edge=opp.edge,
    )
    return order_id, status, side, reason, actual_stake, fill_type, fee_paid
