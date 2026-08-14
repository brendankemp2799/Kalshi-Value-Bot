"""Tests for contract quantization and the rounding-overshoot gate.

Both were changed on 2026-08-14 after an audit of 44 reproducible live bets found
floor() had under-sized 42 of them and over-sized none (median -11.6%, worst -47.4%),
and that a flat $0.50 minimum was discarding +EV opportunities at this bankroll.
"""
from __future__ import annotations

import pytest

import config
from execution.kalshi_executor import _contract_count


# ── quantization ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stake,price,expected", [
    (0.89, 0.45, 2),    # 1.98 -> 2 under round(); floor() gave 1 (a 47% haircut)
    (0.80, 0.45, 2),    # 1.78 -> 2; this is the real bet #517, sized at 1 before
    (1.57, 0.45, 3),    # 3.49 -> 3; unchanged from floor()
    (1.35, 0.45, 3),    # exact multiple, no rounding either way
    (0.60, 0.45, 1),    # 1.33 -> 1; rounds DOWN, so the fix is not merely "round up"
])
def test_rounds_to_nearest_contract(stake, price, expected):
    assert _contract_count(stake, price) == expected


def test_never_returns_zero_contracts():
    """A sub-contract target still buys one contract; whether to bet at all is the
    caller's decision (main.py's overshoot gate), not this function's."""
    assert _contract_count(0.05, 0.90) == 1
    assert _contract_count(0.0, 0.45) == 1


def test_rounding_is_symmetric_not_a_haircut():
    """The whole point of the change: errors must go both ways, not systematically down."""
    price = 0.45
    deviations = []
    for i in range(1, 40):
        target = i * 0.1
        actual = _contract_count(target, price) * price
        deviations.append(actual - target)
    assert any(d > 0 for d in deviations), "rounding must sometimes overshoot"
    assert any(d < 0 for d in deviations), "rounding must sometimes undershoot"
    # Mean error should sit near zero rather than being a one-sided haircut.
    assert abs(sum(deviations) / len(deviations)) < price / 2


def test_zero_price_is_safe():
    assert _contract_count(1.0, 0.0) == 1


# ── overshoot gate (replaces the flat $0.50 minimum) ──────────────────────────

def _overshoot(price: float, kelly: float) -> float:
    return price / max(kelly, 1e-9)


def test_overshoot_gate_admits_bets_the_old_floor_rejected():
    """Real rejections from one live scan: $0.37/$0.42/$0.44/$0.47 Kelly targets.

    Under the old flat $0.50 floor all four were discarded despite Kelly judging them
    +EV. Under the overshoot rule they are admitted whenever one contract does not
    over-bet the target by more than MAX_ROUNDING_OVERSHOOT.
    """
    price = 0.45
    for kelly in (0.37, 0.42, 0.44, 0.47):
        assert _overshoot(price, kelly) <= config.MAX_ROUNDING_OVERSHOOT, (
            f"Kelly ${kelly:.2f} at a ${price:.2f} contract should now be allowed"
        )


def test_overshoot_gate_still_blocks_gross_over_betting():
    """A tiny Kelly target against an expensive contract must still be refused —
    buying one 90c contract for a 20c target is a 4.5x over-bet."""
    assert _overshoot(0.90, 0.20) > config.MAX_ROUNDING_OVERSHOOT


def test_old_floor_discontinuity_is_gone():
    """The old rule rejected $0.47 outright but rounded $0.51 UP to a whole contract
    that could cost $0.65 — same economics, opposite outcome. Both should now be
    treated the same way at the same contract price."""
    price = 0.65
    below, above = 0.47, 0.51
    assert (_overshoot(price, below) <= config.MAX_ROUNDING_OVERSHOOT) == \
           (_overshoot(price, above) <= config.MAX_ROUNDING_OVERSHOOT)
