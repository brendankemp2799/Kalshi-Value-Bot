"""
American-odds profit/loss math for retail-sportsbook bets.

Nothing like this existed before the retail restructure -- the Kalshi path
settled everything in Kalshi's own 0-1 dollar-price space
(storage/db.py::settle_position), which has no notion of American odds or a
push/void landing exactly on a line.
"""
from __future__ import annotations


def american_odds_profit_multiplier(odds: int) -> float:
    """Profit per $1 staked on a win. +150 -> 1.50, -150 -> 0.6667.

    Equivalent to core/kelly_calculator.py::_fee_adjusted_b_and_l(
    american_to_prob(odds), fee_rate=0.0)[0] -- cross-checked against that
    formula rather than re-derived independently, since Kelly sizing and
    settlement P&L must be measuring the same bet the same way.
    """
    if odds > 0:
        return odds / 100.0
    return 100.0 / abs(odds)


def compute_pnl(stake: float, odds_american: int, result: str) -> float:
    """result: 'won' | 'lost' | 'push'.

    A push (totals/spread landing exactly on the threshold) returns 0.0 --
    the stake is returned, not profit, same meaning as a 'void' on the old
    Kalshi path.
    """
    if result == "won":
        return stake * american_odds_profit_multiplier(odds_american)
    if result == "lost":
        return -stake
    if result == "push":
        return 0.0
    raise ValueError(f"Unknown settlement result: {result!r}")
