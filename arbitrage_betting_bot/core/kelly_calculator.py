"""
Fractional Kelly Criterion bet sizing.

Full Kelly formula:
    f* = (b·p - q) / b

Where:
    b = net odds received per unit wagered = (1 / market_price) - 1
    p = estimated true probability of winning (sportsbook consensus)
    q = 1 - p

Fractional Kelly:
    f = f* × KELLY_FRACTION  (default: 0.25 to reduce variance)

Final bet size is further capped by:
    - MAX_BET_DOLLARS: hard dollar cap
    - MAX_PCT_BANKROLL: max % of bankroll per bet

If the Kelly fraction is zero or negative, there is no mathematical edge
and we refuse to recommend the bet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)


@dataclass
class BetSizing:
    full_kelly_fraction: float    # raw Kelly fraction (as % of bankroll)
    fractional_kelly: float       # after applying KELLY_FRACTION multiplier
    recommended_dollars: float    # final recommended bet size
    bankroll: float
    has_edge: bool                # False = no bet recommended


def calculate_kelly(
    consensus_prob: float,
    market_price: float,
    bankroll: float = config.BANKROLL,
    kelly_fraction: float = config.KELLY_FRACTION,
    max_bet_dollars: float = config.MAX_BET_DOLLARS,
    max_pct_bankroll: float = config.MAX_PCT_BANKROLL,
) -> BetSizing:
    """
    Calculate recommended bet size using fractional Kelly Criterion.

    consensus_prob: estimated true probability (from de-vigged sportsbooks)
    market_price:   the prediction market's current price (0-1)
    """
    p = consensus_prob
    q = 1.0 - p

    # b = decimal net odds (what you win per $1 wagered)
    # At price 0.40, you risk $0.40 to win $0.60 → net odds = 0.60/0.40 = 1.5
    if market_price <= 0 or market_price >= 1:
        return BetSizing(
            full_kelly_fraction=0.0,
            fractional_kelly=0.0,
            recommended_dollars=0.0,
            bankroll=bankroll,
            has_edge=False,
        )

    b = (1.0 - market_price) / market_price  # net odds per unit

    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        logger.debug(
            "Kelly ≤ 0 (%.4f) — no edge. consensus=%.3f market=%.3f",
            full_kelly, consensus_prob, market_price,
        )
        return BetSizing(
            full_kelly_fraction=full_kelly,
            fractional_kelly=0.0,
            recommended_dollars=0.0,
            bankroll=bankroll,
            has_edge=False,
        )

    frac_kelly = full_kelly * kelly_fraction

    # Dollar size before caps
    raw_dollars = frac_kelly * bankroll

    # Apply caps
    cap_from_pct = max_pct_bankroll * bankroll
    recommended = min(raw_dollars, max_bet_dollars, cap_from_pct)
    recommended = max(recommended, 0.0)

    logger.debug(
        "Kelly: full=%.3f frac=%.3f raw=$%.2f capped=$%.2f",
        full_kelly, frac_kelly, raw_dollars, recommended,
    )

    return BetSizing(
        full_kelly_fraction=full_kelly,
        fractional_kelly=frac_kelly,
        recommended_dollars=round(recommended, 2),
        bankroll=bankroll,
        has_edge=True,
    )
