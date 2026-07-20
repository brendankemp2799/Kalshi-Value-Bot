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
    full_kelly_fraction: float    # raw Kelly fraction (as % of bankroll), fee-adjusted
    fractional_kelly: float       # after applying KELLY_FRACTION and uncertainty discount
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
    consensus_std: float = 0.0,
) -> BetSizing:
    """
    Calculate recommended bet size using fractional Kelly Criterion.

    consensus_prob: estimated true probability (from de-vigged sportsbooks)
    market_price:   the prediction market's current price (0-1)
    consensus_std:  weighted std dev across books — used to discount bet size
                    when books disagree (higher uncertainty = smaller fraction)
    """
    p = consensus_prob
    q = 1.0 - p

    if market_price <= 0 or market_price >= 1:
        return BetSizing(
            full_kelly_fraction=0.0,
            fractional_kelly=0.0,
            recommended_dollars=0.0,
            bankroll=bankroll,
            has_edge=False,
        )

    b_gross = (1.0 - market_price) / market_price
    # Taker fee (conservative assumption): fee = RATE×price×(1-price)×count
    # simplifies to RATE×(1-price)×stake, so net odds = b_gross × (1 - RATE×price)
    b = b_gross * (1.0 - config.KALSHI_TAKER_FEE_RATE * market_price)

    full_kelly = (b * p - q) / b

    if full_kelly <= 0:
        logger.debug(
            "Kelly ≤ 0 (%.4f) after fee adjustment — no edge. consensus=%.3f market=%.3f taker_fee_rate=%.2f",
            full_kelly, consensus_prob, market_price, config.KALSHI_TAKER_FEE_RATE,
        )
        return BetSizing(
            full_kelly_fraction=full_kelly,
            fractional_kelly=0.0,
            recommended_dollars=0.0,
            bankroll=bankroll,
            has_edge=False,
        )

    # Discount the Kelly fraction when books disagree.
    # Each 0.01 of std_dev costs 20% of the base fraction, floored at 50%.
    # e.g. std_dev=0.03 → factor=0.70; std_dev=0.05+ → factor=0.50
    uncertainty_factor = max(0.5, 1.0 - (consensus_std / 0.05) * 0.5)
    adjusted_fraction = kelly_fraction * uncertainty_factor

    frac_kelly = full_kelly * adjusted_fraction

    # Dollar size before caps
    raw_dollars = frac_kelly * bankroll

    # Apply caps
    cap_from_pct = max_pct_bankroll * bankroll
    recommended = min(raw_dollars, max_bet_dollars, cap_from_pct)
    recommended = max(recommended, 0.0)

    logger.debug(
        "Kelly: full=%.3f unc_factor=%.2f frac=%.3f raw=$%.2f capped=$%.2f (std=%.3f)",
        full_kelly, uncertainty_factor, frac_kelly, raw_dollars, recommended, consensus_std,
    )

    return BetSizing(
        full_kelly_fraction=full_kelly,
        fractional_kelly=frac_kelly,
        recommended_dollars=round(recommended, 2),
        bankroll=bankroll,
        has_edge=True,
    )
