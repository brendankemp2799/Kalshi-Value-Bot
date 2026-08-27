"""research/metrics.py -- confidence-interval / significance layer on top of the
existing point-estimate metrics (roi/win_rate/sharpe_ratio/edge_calibration).
Pure functions over lists of dicts, same convention as test_clv_analytics.py: a
small _pos(**overrides) fixture helper, no DB mocking needed.
"""
from __future__ import annotations

import pytest

from research.metrics import roi_confidence_interval, edge_calibration


def _pos(**overrides):
    base = dict(pnl=1.0, stake=10.0, edge=0.02)
    base.update(overrides)
    return base


# ── roi_confidence_interval ──────────────────────────────────────────────────────

def test_clearly_positive_low_variance_returns_are_significant():
    # Every trade returns +20% on stake, with only tiny jitter -- CI should sit
    # comfortably above zero.
    positions = [_pos(pnl=2.0, stake=10.0) for _ in range(30)]
    positions[0] = _pos(pnl=1.9, stake=10.0)
    positions[1] = _pos(pnl=2.1, stake=10.0)
    result = roi_confidence_interval(positions)
    assert result["n"] == 30
    assert result["ci_low_pct"] > 0.0
    assert result["significant"] is True


def test_small_mixed_sign_sample_is_not_significant():
    # A handful of trades with wins and losses roughly cancelling out -- CI should
    # straddle zero.
    positions = [
        _pos(pnl=5.0, stake=10.0),
        _pos(pnl=-4.0, stake=10.0),
        _pos(pnl=-6.0, stake=10.0),
        _pos(pnl=3.0, stake=10.0),
    ]
    result = roi_confidence_interval(positions)
    assert result["ci_low_pct"] < 0.0 < result["ci_high_pct"]
    assert result["significant"] is False


def test_fewer_than_two_settled_trades_is_insufficient_data():
    assert roi_confidence_interval([]) == {"n": 0, "insufficient_data": True}
    assert roi_confidence_interval([_pos()]) == {"n": 1, "insufficient_data": True}


def test_zero_variance_returns_is_insufficient_data():
    # Every trade returns exactly the same amount -- stdev is 0, CI is undefined,
    # must not divide by zero.
    positions = [_pos(pnl=1.0, stake=10.0) for _ in range(5)]
    result = roi_confidence_interval(positions)
    assert result == {"n": 5, "insufficient_data": True}


def test_unsettled_and_zero_stake_positions_are_excluded():
    positions = [
        _pos(pnl=None, stake=10.0),  # unsettled
        _pos(pnl=1.0, stake=0.0),    # zero stake
    ]
    result = roi_confidence_interval(positions)
    assert result == {"n": 0, "insufficient_data": True}


def test_confidence_level_is_echoed_back():
    positions = [_pos(pnl=p, stake=10.0) for p in (5.0, -4.0, -6.0, 3.0)]
    result = roi_confidence_interval(positions, confidence=0.90)
    assert result["confidence"] == 0.90


def test_wider_confidence_level_widens_the_interval():
    positions = [_pos(pnl=p, stake=10.0) for p in (5.0, -4.0, -6.0, 3.0, 2.0, -1.0)]
    narrow = roi_confidence_interval(positions, confidence=0.80)
    wide = roi_confidence_interval(positions, confidence=0.99)
    width_narrow = narrow["ci_high_pct"] - narrow["ci_low_pct"]
    width_wide = wide["ci_high_pct"] - wide["ci_low_pct"]
    assert width_wide > width_narrow


# ── edge_calibration backward compatibility + new CI fields ─────────────────────

def test_edge_calibration_keeps_existing_keys():
    positions = [_pos(edge=0.02, pnl=1.0, stake=10.0)]
    buckets = edge_calibration(positions)
    bucket = next(b for b in buckets if b["edge_bucket"] == "1.5-3%")
    assert set(["edge_bucket", "n", "roi_pct", "win_rate_pct"]).issubset(bucket.keys())


def test_edge_calibration_adds_roi_ci_per_bucket():
    positions = [_pos(edge=0.02, pnl=1.0, stake=10.0)]
    buckets = edge_calibration(positions)
    bucket = next(b for b in buckets if b["edge_bucket"] == "1.5-3%")
    assert "roi_ci" in bucket
    assert bucket["roi_ci"]["n"] == 1
    assert bucket["roi_ci"]["insufficient_data"] is True


def test_edge_calibration_empty_bucket_has_insufficient_data_ci():
    buckets = edge_calibration([])
    for bucket in buckets:
        assert bucket["roi_ci"] == {"n": 0, "insufficient_data": True}
