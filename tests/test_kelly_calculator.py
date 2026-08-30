"""core/kelly_calculator.py::expected_value_pct -- added 2026-08-29 to back the
dashboard's EV% metric (see core/clv_analytics.py). It returns the same
p*b_net - q*l_net numerator calculate_kelly() divides by (b_net*l_net) to get a
bet fraction; these tests pin that it's the same number, not a separate formula
that could drift from what sizing actually uses.
"""
from __future__ import annotations

import pytest

import config
from core.kelly_calculator import expected_value_pct, calculate_kelly, _fee_adjusted_b_and_l


def test_zero_edge_is_zero_ev():
    """At the fee-free breakeven price (consensus == price), taker fees make this
    a real loser, not a wash -- EV must be negative, not zero."""
    ev = expected_value_pct(0.50, 0.50, fee_rate=config.KALSHI_TAKER_FEE_RATE_ESTIMATE)
    assert ev < 0


def test_zero_fee_at_true_breakeven_is_exactly_zero():
    ev = expected_value_pct(0.50, 0.50, fee_rate=0.0)
    assert ev == pytest.approx(0.0, abs=1e-9)


def test_positive_edge_is_positive_ev():
    ev = expected_value_pct(0.55, 0.45, fee_rate=config.KALSHI_TAKER_FEE_RATE_ESTIMATE)
    assert ev > 0


def test_maker_only_zero_fee_beats_taker_fee_at_the_same_price():
    """Same consensus/price, only the fee differs -- zero-fee (maker) EV must be
    strictly higher than the taker-fee estimate."""
    taker_ev = expected_value_pct(0.52, 0.45, fee_rate=config.KALSHI_TAKER_FEE_RATE_ESTIMATE)
    maker_ev = expected_value_pct(0.52, 0.45, fee_rate=0.0)
    assert maker_ev > taker_ev


def test_matches_calculate_kellys_own_numerator():
    """expected_value_pct must be the SAME p*b_net - q*l_net calculate_kelly() uses
    internally, not a drifted duplicate -- reconstruct it from calculate_kelly's
    public output (full_kelly_fraction * b_net * l_net) and compare."""
    p, price = 0.55, 0.45
    sizing = calculate_kelly(p, price, fee_rate=config.KALSHI_TAKER_FEE_RATE_ESTIMATE)
    b_net, l_net = _fee_adjusted_b_and_l(price, config.KALSHI_TAKER_FEE_RATE_ESTIMATE)
    reconstructed = sizing.full_kelly_fraction * b_net * l_net
    assert expected_value_pct(p, price, config.KALSHI_TAKER_FEE_RATE_ESTIMATE) == pytest.approx(reconstructed)


def test_price_is_clamped_to_valid_range():
    """A price of exactly 0 or 1 would divide by zero / blow up b_net or l_net --
    must not raise."""
    expected_value_pct(0.5, 0.0, fee_rate=0.07)
    expected_value_pct(0.5, 1.0, fee_rate=0.07)
