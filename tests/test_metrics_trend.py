"""Tests for the time-bucketed trend views in research/metrics.py:
weekly_performance_series() and weekly_edge_calibration_series().

Mirrors tests/test_clv_analytics.py's weekly_clv_series coverage (Monday-start
week alignment, same-week grouping, oldest-first ordering, empty weeks omitted
rather than fabricated) since these are the same bucketing logic applied to
ROI/win-rate/edge-calibration instead of CLV.
"""
import pytest

from research.metrics import (
    edge_calibration,
    roi,
    win_rate,
    weekly_edge_calibration_series,
    weekly_performance_series,
)


def _pos(**overrides):
    base = dict(
        sport="baseball_mlb", bet_type="h2h", team_name="Dodgers",
        entered_at="2026-08-20T18:00:00",
        stake=10.0, pnl=1.0, edge=0.02,
    )
    base.update(overrides)
    return base


# ── weekly_performance_series ────────────────────────────────────────────────────

def test_groups_by_iso_week_monday_start():
    positions = [
        _pos(entered_at="2026-08-17T12:00:00"),  # Monday
        _pos(entered_at="2026-08-19T12:00:00"),  # Wednesday, same week
        _pos(entered_at="2026-08-24T12:00:00"),  # next Monday
    ]
    series = weekly_performance_series(positions)
    weeks = {w["week"]: w["n"] for w in series}
    assert weeks["2026-08-17"] == 2
    assert weeks["2026-08-24"] == 1


def test_ordered_oldest_first():
    positions = [
        _pos(entered_at="2026-08-24T12:00:00"),
        _pos(entered_at="2026-08-10T12:00:00"),
    ]
    series = weekly_performance_series(positions)
    assert series[0]["week"] < series[-1]["week"]
    assert series[0]["week"] == "2026-08-10"


def test_weeks_with_zero_settled_positions_are_omitted_not_fabricated():
    positions = [
        _pos(entered_at="2026-08-10T12:00:00", pnl=1.0),
        _pos(entered_at="2026-08-17T12:00:00", pnl=None),  # unsettled, same week alone
        _pos(entered_at="2026-08-24T12:00:00", pnl=2.0),
    ]
    series = weekly_performance_series(positions)
    weeks = [w["week"] for w in series]
    assert "2026-08-17" not in weeks
    assert weeks == ["2026-08-10", "2026-08-24"]


def test_roi_and_win_rate_match_calling_the_underlying_functions_directly():
    week_positions = [
        _pos(entered_at="2026-08-17T09:00:00", stake=10.0, pnl=5.0),
        _pos(entered_at="2026-08-18T09:00:00", stake=20.0, pnl=-4.0),
    ]
    other_week = [_pos(entered_at="2026-08-24T09:00:00", stake=10.0, pnl=1.0)]

    series = weekly_performance_series(week_positions + other_week)
    expected_r, expected_n = roi(week_positions)
    expected_w, _ = win_rate(week_positions)

    row = next(w for w in series if w["week"] == "2026-08-17")
    assert row["n"] == expected_n
    assert row["roi_pct"] == pytest.approx(expected_r)
    assert row["win_rate_pct"] == pytest.approx(expected_w)


def test_unsettled_positions_are_excluded_entirely():
    positions = [_pos(pnl=None)]
    assert weekly_performance_series(positions) == []


def test_empty_input_does_not_crash():
    assert weekly_performance_series([]) == []


def test_positions_missing_or_unparseable_entered_at_are_skipped():
    positions = [
        _pos(entered_at=None),
        _pos(entered_at="not-a-date"),
        _pos(entered_at="2026-08-17T12:00:00"),
    ]
    series = weekly_performance_series(positions)
    assert len(series) == 1
    assert series[0]["n"] == 1


# ── weekly_edge_calibration_series ───────────────────────────────────────────────

def test_recent_vs_older_split_lands_on_the_right_side_of_the_boundary():
    # latest entered_at is 2026-08-24; recent_weeks=4 -> cutoff is 4 weeks earlier.
    old = _pos(entered_at="2026-06-01T12:00:00", edge=0.02)   # well before cutoff
    recent = _pos(entered_at="2026-08-24T12:00:00", edge=0.02)  # the latest trade itself

    result = weekly_edge_calibration_series([old, recent], recent_weeks=4)
    assert result["recent"]["n"] == 1
    assert result["older"]["n"] == 1


def test_delegates_to_edge_calibration_rather_than_reimplementing_buckets():
    positions = [_pos(entered_at="2026-08-24T12:00:00", edge=0.02, pnl=1.0)]
    result = weekly_edge_calibration_series(positions, recent_weeks=4)

    expected_labels = [b["edge_bucket"] for b in edge_calibration(positions)]
    recent_labels = [b["edge_bucket"] for b in result["recent"]["calibration"]]
    assert recent_labels == expected_labels


def test_small_n_note_appears_when_expected():
    positions = [_pos(entered_at="2026-08-24T12:00:00")]
    result = weekly_edge_calibration_series(positions, recent_weeks=4)
    assert "n=1" in result["note"] or "< 20" in result["note"]


def test_note_is_clean_when_both_sides_have_enough_n():
    recent = [_pos(entered_at="2026-08-24T12:00:00", edge=0.02) for _ in range(20)]
    older = [_pos(entered_at="2026-06-01T12:00:00", edge=0.02) for _ in range(20)]
    result = weekly_edge_calibration_series(recent + older, recent_weeks=4)
    assert "n>=20" in result["note"] or "not just noise" in result["note"]


def test_empty_input_does_not_crash_and_notes_no_data():
    result = weekly_edge_calibration_series([])
    assert result["recent"]["n"] == 0
    assert result["older"]["n"] == 0
    assert "note" in result
