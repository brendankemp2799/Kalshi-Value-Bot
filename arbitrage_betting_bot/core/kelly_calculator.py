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


def _fee_adjusted_b_and_l(
    price: float, fee_rate: float = config.KALSHI_TAKER_FEE_RATE_ESTIMATE,
) -> tuple[float, float]:
    """
    Net win odds (b_net) and net loss multiplier (L_net) for a binary contract at
    `price`, after a fee rate charged on stake regardless of outcome. Defaults to
    Kalshi's estimated taker fee; callers sizing a maker_only opportunity (see
    core/value_detector.py::_eval_edge) should pass fee_rate=0.0 -- those only ever
    execute at the fee-free mid price, never the ask, so sizing them with the
    worst-case taker rate understates real edge and can wrongly reject a good bet.

    Derivation, per $1 staked (N = 1/price contracts):
      win:  payout = N = 1/price; fee = RATE*(1-price); net profit = payout - fee - 1
            => b_net = (1-price)*(1 - RATE*price) / price
      lose: payout = 0; fee still charged => net loss = 1 + RATE*(1-price) = L_net
    """
    b_net = (1.0 - price) * (1.0 - fee_rate * price) / price
    l_net = 1.0 + fee_rate * (1.0 - price)
    return b_net, l_net


def estimated_fee_per_contract(
    price: float, fee_rate: float = config.KALSHI_TAKER_FEE_RATE_ESTIMATE,
) -> float:
    """
    Worst-case (taker) fee in dollars for ONE contract at `price`, on the same
    RATE*(1-price)-per-$1-staked estimate _fee_adjusted_b_and_l() uses for sizing —
    one contract costs `price` dollars, so per-contract fee = price * RATE*(1-price).
    Peaks at price=0.50 (1.75 cents/contract at the default 7% rate), symmetric
    toward either extreme.

    Used outside Kelly sizing wherever a pre-trade decision needs a real dollar fee
    estimate rather than a fee-adjusted probability -- e.g. main.py's true-arbitrage
    detection, which must confirm the price-sum margin actually exceeds what both
    legs' fees would cost before treating it as risk-free (a thin-margin "arb" can
    otherwise be a guaranteed small LOSS once real fees land, if either leg fills as
    taker rather than resting to a maker fill).
    """
    return price * fee_rate * (1.0 - price)


def expected_value_pct(
    consensus_prob: float, market_price: float,
    fee_rate: float = config.KALSHI_TAKER_FEE_RATE_ESTIMATE,
) -> float:
    """
    Fee-adjusted expected value as a fraction of stake: p*b_net - q*l_net -- the
    same numerator calculate_kelly() divides by (b_net*l_net) to get a bet
    fraction, returned here on its own. "For every dollar staked at entry, how
    much did the model expect to make (or lose), net of the estimated fee" --
    independent of bankroll or position sizing. A forecast made AT ENTRY, from
    consensus_prob and market_price as they stood then -- not a measurement of
    what actually happened (that's realized P&L/win rate). Used for post-hoc
    calibration checks (see core/clv_analytics.py's EV% metric): if mean EV%
    tracks realized ROI over enough settled bets, the probability estimates feeding
    it are calibrated; if realized ROI runs persistently below mean EV%, they're
    optimistic.
    """
    p = consensus_prob
    q = 1.0 - p
    effective_price = max(0.01, min(0.99, market_price))
    b_net, l_net = _fee_adjusted_b_and_l(effective_price, fee_rate)
    return p * b_net - q * l_net


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
    fee_rate: float = config.KALSHI_TAKER_FEE_RATE_ESTIMATE,
) -> BetSizing:
    """
    Calculate recommended bet size using fractional Kelly Criterion.

    consensus_prob: estimated true probability (from de-vigged sportsbooks)
    market_price:   the price sizing should assume it pays (0-1) — the ask price
                    for a normal opportunity (the floor execution guarantees via
                    the ask-price fallback step; a better mid-price fill is a
                    bonus, not something to size for), or the mid price for a
                    maker_only opportunity (see core/value_detector.py::
                    _eval_edge) -- those have no ask fallback, mid IS the floor.
    consensus_std:  weighted std dev across books — used to discount bet size
                    when books disagree (higher uncertainty = smaller fraction)
    fee_rate:       fee assumed for sizing. Defaults to Kalshi's estimated taker
                    rate; pass 0.0 for maker_only opportunities, which only ever
                    execute at the fee-free mid price.
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

    b_net, l_net = _fee_adjusted_b_and_l(effective_price, fee_rate)

    full_kelly = (p * b_net - q * l_net) / (b_net * l_net)

    if full_kelly <= 0:
        logger.debug(
            "Kelly ≤ 0 (%.4f) — no edge. consensus=%.3f price=%.3f fee_rate=%.3f",
            full_kelly, consensus_prob, effective_price, fee_rate,
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
        "Kelly: full=%.3f unc_factor=%.2f frac=%.3f raw=$%.2f capped=$%.2f (std=%.3f price=%.3f fee_rate=%.3f)",
        full_kelly, uncertainty_factor, frac_kelly, raw_dollars, recommended, consensus_std, effective_price, fee_rate,
    )

    return BetSizing(
        full_kelly_fraction=full_kelly,
        fractional_kelly=frac_kelly,
        recommended_dollars=round(recommended, 2),
        bankroll=bankroll,
        has_edge=True,
    )


def mm_clip_size(
    kalshi_spread: float,
    bankroll: float = config.BANKROLL,
    max_clip_dollars: float = config.MM_MAX_CLIP_DOLLARS,
    max_concurrent: int = config.MM_MAX_CONCURRENT_QUOTES,
) -> float:
    """
    Size one leg of a market-making quote. Kelly doesn't apply here — there's no
    probability edge being sized, just spread capture — so size scales with how
    much spread there is to capture instead: a wider Kalshi spread means more
    captured per round-trip fill, which can justify a slightly larger clip, up to
    a small hard cap (config.MM_MAX_CLIP_DOLLARS). This deliberately stays well
    below a directional Kelly bet's typical size, since market making is a new,
    unvalidated execution mode (see mm_backtest.py) sharing the same bankroll caps
    as the proven directional strategy.

    Scales linearly from 40% of the cap at the minimum quotable spread
    (config.MM_MIN_SPREAD_TO_QUOTE) up to the full cap at 2x that spread.

    The bankroll-percentage cap is MM_MAX_EXPOSURE_PCT / MM_MAX_CONCURRENT_QUOTES,
    NOT MAX_PCT_BANKROLL. It used to be the latter, which is numerically identical
    to the whole MM budget (MM_MAX_EXPOSURE_PCT * bankroll) — so a single clip
    consumed essentially the entire allowance and run_mm_tick()'s aggregate cap
    rejected every subsequent candidate. Measured live at a $157.72 bankroll:
    total MM budget $7.89, one clip $7.04-$7.68, candidates that fit: 1 (out of
    ~60 per scan). Dividing by max_concurrent is what makes the aggregate cap a
    real ceiling rather than a one-candidate gate.
    """
    min_spread = config.MM_MIN_SPREAD_TO_QUOTE
    if kalshi_spread <= 0 or min_spread <= 0:
        return 0.0
    scale = min(1.0, max(0.4, kalshi_spread / (2.0 * min_spread)))
    dollars = scale * max_clip_dollars
    pct_cap = config.MM_MAX_EXPOSURE_PCT / max(1, max_concurrent)
    return round(min(dollars, max_clip_dollars, pct_cap * bankroll), 2)
