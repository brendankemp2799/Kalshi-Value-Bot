"""dk_scaled_shadow_log: the calibration record for DK-scaled player-prop estimates.

Added 2026-08-24 alongside DK_SCALED_SHADOW_MODE, in response to an external review
of the anchor-and-scale feature: "run it in shadow mode first and empirically measure
its calibration... the most important question is how prediction error changes with
distance from the Pinnacle anchor." This file pins the storage layer that makes that
measurement possible -- write, resolve, and summarize -- independent of the detector
tests in test_player_props.py, which pin where the writes come FROM.
"""
from __future__ import annotations

import pytest

import storage.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A real SQLite file so the retry-cap and aggregation SQL run for real."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _entry(**overrides):
    base = dict(
        sport="baseball_mlb", home_team="LAD", away_team="PIT",
        participant="Pitcher A", market_key="pitcher_strikeouts",
        kalshi_ticker="KXMLBKS-26AUG221915PITLAD-X-6", kalshi_side="yes",
        target_point=6.5, anchor_point=4.5, distance=2.0,
        anchor_fair_prob=0.58, anchor_raw_prob=0.606, target_raw_prob=0.27,
        scaling_ratio=0.957, scaled_prob=0.26, kalshi_price=0.20, edge=0.06,
        would_bet=1, status="dk_shadow_value", reason="ok",
        commence_time="2026-08-22T19:15:00Z",
    )
    base.update(overrides)
    return base


def test_logged_estimates_round_trip(fresh_db):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    rows = db.get_dk_scaled_shadow_rows()
    assert len(rows) == 1
    assert rows[0]["participant"] == "Pitcher A"
    assert rows[0]["distance"] == pytest.approx(2.0)
    assert rows[0]["actual_outcome"] is None


def test_an_empty_entry_list_is_a_no_op(fresh_db):
    db.log_dk_scaled_estimates("scan1", [])
    assert db.get_dk_scaled_shadow_rows() == []


def test_pending_outcomes_are_everything_unsettled(fresh_db):
    db.log_dk_scaled_estimates("scan1", [_entry(), _entry(kalshi_side="no")])
    pending = db.get_pending_dk_scaled_outcomes()
    assert len(pending) == 2


def test_setting_an_outcome_removes_it_from_pending(fresh_db):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    row_id = db.get_pending_dk_scaled_outcomes()[0]["id"]
    db.set_dk_scaled_outcome(row_id, 1.0)
    assert db.get_pending_dk_scaled_outcomes() == []
    rows = db.get_dk_scaled_shadow_rows(settled_only=True)
    assert len(rows) == 1 and rows[0]["actual_outcome"] == 1.0


def test_a_void_outcome_bumps_attempts_without_settling(fresh_db):
    """Mirrors book_probability_log's void handling: a void result is recorded as
    actual_outcome=None, which still matches "unresolved" -- it bumps the attempt
    counter (so it eventually falls off the retry cap, see
    test_pending_outcomes_respect_the_retry_cap) rather than counting as a scored
    calibration point."""
    db.log_dk_scaled_estimates("scan1", [_entry()])
    row_id = db.get_pending_dk_scaled_outcomes()[0]["id"]
    db.set_dk_scaled_outcome(row_id, None)
    pending = db.get_pending_dk_scaled_outcomes()
    assert len(pending) == 1 and pending[0]["outcome_check_attempts"] == 1
    assert db.get_dk_scaled_shadow_rows(settled_only=True) == [], \
        "void is not a scored outcome"


def test_pending_outcomes_respect_the_retry_cap(fresh_db):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    row_id = db.get_pending_dk_scaled_outcomes()[0]["id"]
    for _ in range(5):
        db.set_dk_scaled_outcome(row_id, None)
    assert db.get_pending_dk_scaled_outcomes(max_attempts=5) == [], \
        "must fall off the retry queue once max_attempts is exhausted"


# ── summary / calibration aggregation ────────────────────────────────────────────

def test_summary_on_an_empty_table(fresh_db):
    summary = db.get_dk_scaled_shadow_summary()
    assert summary["n"] == 0 and summary["n_settled"] == 0 and summary["brier"] is None


def test_summary_counts_logged_vs_settled_vs_would_bet(fresh_db):
    db.log_dk_scaled_estimates("scan1", [
        _entry(kalshi_ticker="A", would_bet=1),
        _entry(kalshi_ticker="B", would_bet=0),
    ])
    row_id = db.get_pending_dk_scaled_outcomes()[0]["id"]
    db.set_dk_scaled_outcome(row_id, 1.0)
    summary = db.get_dk_scaled_shadow_summary()
    assert summary["n"] == 2
    assert summary["n_settled"] == 1
    assert summary["n_would_bet"] == 1


def test_brier_score_is_computed_correctly(fresh_db):
    """Perfectly wrong prediction (predicted 0.9, actual 0.0) has Brier = 0.81."""
    db.log_dk_scaled_estimates("scan1", [_entry(scaled_prob=0.9)])
    row_id = db.get_pending_dk_scaled_outcomes()[0]["id"]
    db.set_dk_scaled_outcome(row_id, 0.0)
    summary = db.get_dk_scaled_shadow_summary()
    assert summary["brier"] == pytest.approx(0.81)


def test_calibration_buckets_by_distance_from_anchor(fresh_db):
    """A near-anchor rung and a far-from-anchor rung must land in different buckets,
    with independent Brier scores -- this is the whole point of the table."""
    db.log_dk_scaled_estimates("scan1", [
        _entry(kalshi_ticker="near", distance=0.2, scaled_prob=0.5),   # perfect
        _entry(kalshi_ticker="far", distance=4.0, scaled_prob=0.9),    # way off
    ])
    ids = [r["id"] for r in db.get_pending_dk_scaled_outcomes()]
    tickers = {r["id"]: r["kalshi_ticker"] for r in db.get_dk_scaled_shadow_rows()}
    for row_id in ids:
        actual = 0.5 if tickers[row_id] == "near" else 0.0
        db.set_dk_scaled_outcome(row_id, actual)

    summary = db.get_dk_scaled_shadow_summary()
    near_bucket = next(b for b in summary["buckets"] if b["range"] == "0–0.5")
    far_bucket = next(b for b in summary["buckets"] if b["range"] == "3+")
    assert near_bucket["n"] == 1 and near_bucket["brier"] == pytest.approx(0.0)
    assert far_bucket["n"] == 1 and far_bucket["brier"] == pytest.approx(0.81)


def test_shadow_rows_are_newest_first(fresh_db):
    db.log_dk_scaled_estimates("scan1", [_entry(kalshi_ticker="first")])
    db.log_dk_scaled_estimates("scan2", [_entry(kalshi_ticker="second")])
    rows = db.get_dk_scaled_shadow_rows()
    assert rows[0]["kalshi_ticker"] == "second"
