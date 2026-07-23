"""
Fractional Kelly Criterion bet sizing, adjusted for Kalshi's real fee.

Standard full Kelly:
    f* = (b·p - q) / b
    b = net odds received per unit wagered = (1 - price) / price
    p = estimated true probability of winning (sportsbook consensus)
    q = 1 - p

This assumes a loss costs exactly 1x the stake and nothing else. That's not true here:
Kalshi's taker fee is charged at fill time regardless of outcome (see
storage/db.py::settle_position), so a loss actually costs stake + fee — more than 1x.
The standard formula also overstates the win payout by ignoring the fee taken out of it.

Generalized two-outcome Kelly (win pays b_net per unit, loss costs L_net per unit,
L_net > 1 here because of the sunk fee):
    f* = (p·b_net - q·L_net) / (b_net · L_net)

See _fee_adjusted_b_and_l() for the derivation of b_net/L_net. This is a pre-trade
ESTIMATE (config.KALSHI_TAKER_FEE_RATE_ESTIMATE) — we don't know at sizing time whether
the fill will actually be maker or taker, so it assumes the worst case (taker), matching
the same guaranteed-floor philosophy already used for the ask-price edge check.

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


def _fee_adjusted_b_and_l(price: float) -> tuple[float, float]:
    """
    Net win odds (b_net) and net loss multiplier (L_net) for a binary contract at
    `price`, after Kalshi's estimated taker fee (config.KALSHI_TAKER_FEE_RATE_ESTIMATE),
    which is charged on stake regardless of outcome.

    Derivation, per $1 staked (N = 1/price contracts):
      win:  payout = N = 1/price; fee = RATE*(1-price); net profit = payout - fee - 1
            => b_net = (1-price)*(1 - RATE*price) / price
      lose: payout = 0; fee still charged => net loss = 1 + RATE*(1-price) = L_net
    """
    fee_rate = config.KALSHI_TAKER_FEE_RATE_ESTIMATE
    b_net = (1.0 - price) * (1.0 - fee_rate * price) / price
    l_net = 1.0 + fee_rate * (1.0 - price)
    return b_net, l_net


def fee_adjusted_breakeven_prob(price: float) -> float:
    """
    The true probability at which a contract at `price` has exactly zero net EV after
    the estimated fee (versus plain `price`, which is the zero-fee breakeven point).
    Used by value_detector to raise the effective edge bar by the fee amount, without
    changing what the stored `edge` value itself means (still consensus - price).
    """
    b_net, l_net = _fee_adjusted_b_and_l(price)
    return l_net / (b_net + l_net)


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
    market_price:   the prediction market's ask price (0-1) — the price
                    value_detector's edge check qualifies against, and the
                    floor execution guarantees via the ask-price fallback
                    step. A better mid-price fill is a bonus, not something
                    to size for.
    consensus_std:  weighted std dev across books — used to discount bet size
                    when books disagree (higher uncertainty = smaller fraction)
    """
    p = consensus_prob
    q = 1.0 - p

    effective_price = max(0.01, min(0.99, market_price))

    if effective_price <= 0 or effective_price >= 1:
        return BetSizing(
            full_kelly_fraction=0.0,
            fractional_kelly=0.0,
            recommended_dollars=0.0,
            bankroll=bankroll,
            has_edge=False,
        )

    b_net, l_net = _fee_adjusted_b_and_l(effective_price)

    full_kelly = (p * b_net - q * l_net) / (b_net * l_net)

    if full_kelly <= 0:
        logger.debug(
            "Kelly ≤ 0 (%.4f) — no edge. consensus=%.3f ask=%.3f",
            full_kelly, consensus_prob, effective_price,
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
        "Kelly: full=%.3f unc_factor=%.2f frac=%.3f raw=$%.2f capped=$%.2f (std=%.3f ask=%.3f)",
        full_kelly, uncertainty_factor, frac_kelly, raw_dollars, recommended, consensus_std, effective_price,
    )

    return BetSizing(
        full_kelly_fraction=full_kelly,
        fractional_kelly=frac_kelly,
        recommended_dollars=round(recommended, 2),
        bankroll=bankroll,
        has_edge=True,
    )
