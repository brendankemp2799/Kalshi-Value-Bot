"""research/metrics.py::dk_scaled_shadow_backtest -- would the DK-scaled
player-prop shadow-mode picks have actually made money, not just been
well-calibrated (storage/db.py::get_dk_scaled_shadow_summary's Brier score
answers the calibration question; this answers the profitability one).

Pure-function tests for the backtest math, plus a DB-layer section for
storage/db.py::get_dk_scaled_settled_rows. Same house style as
test_metrics_counterfactual.py: plain-dict fixtures, no DB needed for the
pure-function tests.
"""
from __future__ import annotations

import pytest

import research.metrics as metrics
import storage.db as db


def _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1, distance=1.0, **overrides):
    base = dict(
        kalshi_price=kalshi_price, edge=0.03, distance=distance, would_bet=would_bet,
        actual_outcome=actual_outcome, sport="baseball_mlb", market_key="player_hits",
    )
    base.update(overrides)
    return base


# ── would_bet / all_settled math ─────────────────────────────────────────────────

def test_would_bet_uses_only_would_bet_rows():
    rows = [
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1),  # +10
        _row(kalshi_price=0.50, actual_outcome=0.0, would_bet=1),  # -10
        _row(kalshi_price=0.50, actual_outcome=0.0, would_bet=0),  # excluded from would_bet leg
    ]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert out["would_bet"]["n"] == 2
    assert out["would_bet"]["roi_pct"] == pytest.approx(0.0)
    assert out["would_bet"]["win_rate_pct"] == pytest.approx(50.0)


def test_all_settled_includes_non_would_bet_rows():
    rows = [
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1),
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=0),
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=0),
    ]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert out["all_settled"]["n"] == 3
    assert out["would_bet"]["n"] == 1


def test_rows_without_a_positive_price_are_skipped_not_crashed():
    rows = [_row(kalshi_price=None), _row(kalshi_price=0.0),
            _row(kalshi_price=0.50, actual_outcome=1.0)]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert out["would_bet"]["n"] == 1


def test_rows_without_a_resolved_outcome_are_skipped():
    rows = [_row(actual_outcome=None), _row(kalshi_price=0.50, actual_outcome=1.0)]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert out["would_bet"]["n"] == 1


# ── by_distance_bucket ────────────────────────────────────────────────────────────

def test_by_distance_bucket_matches_calibration_bucket_ranges():
    out = metrics.dk_scaled_shadow_backtest([], unit_stake=10.0)
    labels = [b["range"] for b in out["by_distance_bucket"]]
    assert labels == ["0-0.5", "0.5-1.5", "1.5-3", "3+"]


def test_by_distance_bucket_sorts_rows_by_absolute_distance():
    rows = [
        _row(distance=0.2, kalshi_price=0.50, actual_outcome=1.0, would_bet=1),   # 0-0.5
        _row(distance=-2.0, kalshi_price=0.50, actual_outcome=1.0, would_bet=1),  # 1.5-3 (abs)
        _row(distance=5.0, kalshi_price=0.50, actual_outcome=0.0, would_bet=1),   # 3+
    ]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    by_range = {b["range"]: b for b in out["by_distance_bucket"]}
    assert by_range["0-0.5"]["n"] == 1
    assert by_range["0.5-1.5"]["n"] == 0
    assert by_range["1.5-3"]["n"] == 1
    assert by_range["3+"]["n"] == 1


def test_by_distance_bucket_only_considers_would_bet_rows():
    rows = [_row(distance=0.2, would_bet=0, kalshi_price=0.50, actual_outcome=1.0)]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert out["by_distance_bucket"][0]["n"] == 0


# ── verdict text ────────────────────────────────────────────────────────────────

def test_verdict_is_no_data_when_nothing_would_bet_and_settled():
    out = metrics.dk_scaled_shadow_backtest([_row(would_bet=0)], unit_stake=10.0)
    assert out["verdict"].startswith("NO DATA")


def test_verdict_reports_significant_when_ci_excludes_zero():
    # All wins at even money, with a tiny price jitter so returns aren't
    # perfectly zero-variance (roi_confidence_interval can't compute a CI on a
    # zero-stdev sample -- see its own "insufficient_data" handling).
    rows = [_row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1) for _ in range(30)]
    rows[0] = _row(kalshi_price=0.49, actual_outcome=1.0, would_bet=1)
    rows[1] = _row(kalshi_price=0.51, actual_outcome=1.0, would_bet=1)
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert "significant" in out["verdict"]
    assert out["would_bet"]["roi_ci"]["significant"] is True


def test_verdict_reports_not_significant_on_small_mixed_sample():
    rows = [
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1),
        _row(kalshi_price=0.50, actual_outcome=0.0, would_bet=1),
        _row(kalshi_price=0.50, actual_outcome=1.0, would_bet=1),
        _row(kalshi_price=0.50, actual_outcome=0.0, would_bet=1),
    ]
    out = metrics.dk_scaled_shadow_backtest(rows, unit_stake=10.0)
    assert "not yet distinguishable from zero" in out["verdict"]


# ── empty input doesn't crash ────────────────────────────────────────────────────

def test_empty_rows_does_not_crash():
    out = metrics.dk_scaled_shadow_backtest([], unit_stake=10.0)
    assert out["would_bet"] == {
        "n": 0, "roi_pct": None, "win_rate_pct": None,
        "roi_ci": {"n": 0, "insufficient_data": True},
    }
    assert out["all_settled"] == {"n": 0, "roi_pct": None, "win_rate_pct": None}
    assert isinstance(out["verdict"], str)


# ── DB layer: get_dk_scaled_settled_rows ─────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _insert_dk_row(conn, **overrides):
    row = dict(
        scan_id="s1", scanned_at="2026-08-20T18:00:00", sport="baseball_mlb",
        home_team="Dodgers", away_team="Giants", participant="Mookie Betts",
        market_key="player_hits", kalshi_ticker="KXTEST-1", kalshi_side="yes",
        target_point=1.5, anchor_point=0.5, distance=1.0, anchor_fair_prob=0.6,
        anchor_raw_prob=0.58, target_raw_prob=0.3, scaling_ratio=1.03,
        scaled_prob=0.31, kalshi_price=0.30, edge=0.01, would_bet=1,
        status="dk_shadow_value", reason="shadow", commence_time="2026-08-20T20:00:00+00:00",
        position_id=None, actual_outcome=None,
    )
    row.update(overrides)
    conn.execute(
        """
        INSERT INTO dk_scaled_shadow_log
            (scan_id, scanned_at, sport, home_team, away_team, participant,
             market_key, kalshi_ticker, kalshi_side, target_point, anchor_point,
             distance, anchor_fair_prob, anchor_raw_prob, target_raw_prob,
             scaling_ratio, scaled_prob, kalshi_price, edge, would_bet, status,
             reason, commence_time, position_id, actual_outcome)
        VALUES
            (:scan_id, :scanned_at, :sport, :home_team, :away_team, :participant,
             :market_key, :kalshi_ticker, :kalshi_side, :target_point, :anchor_point,
             :distance, :anchor_fair_prob, :anchor_raw_prob, :target_raw_prob,
             :scaling_ratio, :scaled_prob, :kalshi_price, :edge, :would_bet, :status,
             :reason, :commence_time, :position_id, :actual_outcome)
        """,
        row,
    )


def test_get_dk_scaled_settled_rows_requires_resolved_outcome(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_dk_row(conn, kalshi_ticker="A", actual_outcome=None)
        _insert_dk_row(conn, kalshi_ticker="B", actual_outcome=1.0)
    rows = fresh_db.get_dk_scaled_settled_rows()
    assert len(rows) == 1
    assert rows[0]["actual_outcome"] == 1.0


def test_get_dk_scaled_settled_rows_dedupes_per_ticker_and_side(fresh_db):
    """Same rung re-evaluated across multiple scans should collapse to one row,
    same as get_dk_scaled_shadow_rows -- otherwise a backtest would badly
    overweight tickers that got scanned many times before settling."""
    with fresh_db.get_connection() as conn:
        _insert_dk_row(conn, kalshi_ticker="A", kalshi_side="yes",
                        scanned_at="2026-08-20T18:00:00", would_bet=0, actual_outcome=1.0)
        _insert_dk_row(conn, kalshi_ticker="A", kalshi_side="yes",
                        scanned_at="2026-08-20T19:00:00", would_bet=1, actual_outcome=1.0)
    rows = fresh_db.get_dk_scaled_settled_rows()
    assert len(rows) == 1
    assert rows[0]["would_bet"] == 1  # would_bet=1 wins the dedup, per _DK_DEDUPED


def test_get_dk_scaled_settled_rows_returns_expected_columns(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_dk_row(conn, kalshi_ticker="A", actual_outcome=1.0)
    row = fresh_db.get_dk_scaled_settled_rows()[0]
    for field in ("kalshi_price", "edge", "distance", "would_bet", "actual_outcome",
                  "sport", "market_key"):
        assert field in row


def test_get_dk_scaled_settled_rows_on_empty_db_is_empty_list(fresh_db):
    assert fresh_db.get_dk_scaled_settled_rows() == []
