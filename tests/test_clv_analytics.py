"""core/clv_analytics.py -- CLV and TTE math, added 2026-08-25 for the dashboard
revamp (P&L now tracked externally via Pikkit; this dashboard's job is CLV broken
down by sport/bet-type/time, and whether time-to-event correlates with outcome).

kalshi_clv/consensus_clv/tte_hours/won are all DERIVED from stored position fields,
never re-fetched -- these tests pin the derivation math directly, independent of the
DB layer (see test_clv_dashboard.py for the DB + dashboard integration).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.clv_analytics import (
    compute_row, compute_rows, overall_summary, group_by_field, bucket_by_tte,
    weekly_clv_series, pearson_correlation, filter_rows_since,
)


def _pos(**overrides):
    base = dict(
        sport="baseball_mlb", bet_type="h2h", team_name="Dodgers",
        entered_at="2026-08-20T18:00:00", commence_time="2026-08-20T20:00:00+00:00",
        market_price=0.45, kalshi_close_price=0.50,
        consensus_prob=0.47, consensus_close_prob=0.49,
        pnl=1.0, stake=10.0, threshold=None, maker_only=0,
        market_ticker="KXTEST-1",
    )
    base.update(overrides)
    return base


# ── compute_row: the core derivations ────────────────────────────────────────────

def test_kalshi_clv_is_close_minus_entry():
    row = compute_row(_pos(market_price=0.45, kalshi_close_price=0.50))
    assert row["kalshi_clv"] == pytest.approx(0.05)


def test_consensus_clv_is_close_minus_entry():
    row = compute_row(_pos(consensus_prob=0.47, consensus_close_prob=0.49))
    assert row["consensus_clv"] == pytest.approx(0.02)


def test_negative_clv_when_the_market_moved_against_us():
    row = compute_row(_pos(market_price=0.55, kalshi_close_price=0.40))
    assert row["kalshi_clv"] == pytest.approx(-0.15)


# ── ev_pct ────────────────────────────────────────────────────────────────────

def test_ev_pct_matches_kelly_calculators_taker_estimate_by_default():
    from core.kelly_calculator import expected_value_pct
    import config
    row = compute_row(_pos(consensus_prob=0.47, market_price=0.45, maker_only=0))
    expected = round(expected_value_pct(0.47, 0.45, config.KALSHI_TAKER_FEE_RATE_ESTIMATE), 4)
    assert row["ev_pct"] == pytest.approx(expected)


def test_ev_pct_uses_zero_fee_when_maker_only():
    from core.kelly_calculator import expected_value_pct
    row = compute_row(_pos(consensus_prob=0.47, market_price=0.45, maker_only=1))
    expected = round(expected_value_pct(0.47, 0.45, 0.0), 4)
    assert row["ev_pct"] == pytest.approx(expected)
    # maker_only must actually change the number, not silently no-op the fee arg
    taker_row = compute_row(_pos(consensus_prob=0.47, market_price=0.45, maker_only=0))
    assert row["ev_pct"] != taker_row["ev_pct"]


def test_ev_pct_is_none_without_consensus_prob():
    row = compute_row(_pos(consensus_prob=None))
    assert row["ev_pct"] is None


def test_ev_pct_is_none_without_market_price():
    row = compute_row(_pos(market_price=None))
    assert row["ev_pct"] is None


# ── filter_rows_since ────────────────────────────────────────────────────────────

def test_filter_rows_since_none_cutoff_returns_everything_unchanged():
    rows = compute_rows([_pos(entered_at="2020-01-01T00:00:00")])
    assert filter_rows_since(rows, None) == rows


def test_filter_rows_since_keeps_rows_at_or_after_cutoff():
    rows = compute_rows([
        _pos(entered_at="2026-08-20T12:00:00", team_name="before"),
        _pos(entered_at="2026-08-25T00:00:00", team_name="exactly_at_cutoff"),
        _pos(entered_at="2026-08-28T12:00:00", team_name="after"),
    ])
    cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
    kept = {r["team_name"] for r in filter_rows_since(rows, cutoff)}
    assert kept == {"exactly_at_cutoff", "after"}


def test_filter_rows_since_drops_rows_with_no_entered_at():
    rows = compute_rows([_pos(entered_at=None)])
    assert filter_rows_since(rows, datetime(2020, 1, 1, tzinfo=timezone.utc)) == []


def test_tte_hours_is_commence_minus_entered_in_hours():
    row = compute_row(_pos(entered_at="2026-08-20T18:00:00",
                           commence_time="2026-08-20T20:30:00+00:00"))
    assert row["tte_hours"] == pytest.approx(2.5)


def test_naive_entered_at_is_treated_as_utc():
    """entered_at is written with datetime.utcnow().isoformat() -- naive, no
    offset. It must be compared against commence_time (tz-aware) as UTC, not as
    the local system timezone."""
    row = compute_row(_pos(entered_at="2026-08-20T18:00:00",
                           commence_time="2026-08-20T19:00:00+00:00"))
    assert row["tte_hours"] == pytest.approx(1.0)


def test_won_is_derived_from_positive_pnl():
    assert compute_row(_pos(pnl=1.0))["won"] is True
    assert compute_row(_pos(pnl=-1.0))["won"] is False
    assert compute_row(_pos(pnl=0.0))["won"] is False  # void: not a win


def test_missing_pnl_leaves_won_as_none_not_false():
    """An unsettled/void position must not silently count as a loss in win-rate
    math -- that would be a fabricated data point, not a real observation."""
    assert compute_row(_pos(pnl=None))["won"] is None


def test_missing_closing_line_leaves_clv_as_none():
    row = compute_row(_pos(kalshi_close_price=None))
    assert row["kalshi_clv"] is None


def test_missing_entry_consensus_leaves_consensus_clv_as_none():
    """Positions entered before the consensus_prob column existed (2026-08-25)
    have consensus_prob=NULL -- must degrade to None, not crash or fabricate 0."""
    row = compute_row(_pos(consensus_prob=None))
    assert row["consensus_clv"] is None


def test_missing_commence_time_leaves_tte_as_none():
    row = compute_row(_pos(commence_time=None))
    assert row["tte_hours"] is None


def test_original_fields_survive_unchanged():
    row = compute_row(_pos(sport="soccer_epl"))
    assert row["sport"] == "soccer_epl"


# ── pearson_correlation ──────────────────────────────────────────────────────────

def test_perfect_positive_correlation():
    assert pearson_correlation([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)


def test_perfect_negative_correlation():
    assert pearson_correlation([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)


def test_no_correlation_is_near_zero():
    """y is symmetric around x's mean (a parabola shape), giving exactly zero
    covariance with a linear x -- a real zero-correlation case, not an assumption."""
    assert pearson_correlation([1, 2, 3, 4], [1, 4, 4, 1]) == pytest.approx(0.0, abs=1e-9)


def test_fewer_than_three_pairs_returns_none():
    assert pearson_correlation([1, 2], [1, 2]) is None
    assert pearson_correlation([], []) is None


def test_none_values_are_dropped_as_pairs_not_treated_as_zero():
    """A None in either axis must exclude that PAIR, not get coerced to 0.0 --
    coercion would fabricate a data point and bias the correlation."""
    with_none = pearson_correlation([1, 2, 3, None], [1, 2, 3, 100])
    without = pearson_correlation([1, 2, 3], [1, 2, 3])
    assert with_none == without


def test_zero_variance_axis_returns_none():
    """Every x identical (or every y identical) makes correlation undefined, not
    a divide-by-zero crash."""
    assert pearson_correlation([5, 5, 5], [1, 2, 3]) is None


# ── overall_summary ──────────────────────────────────────────────────────────────

def test_overall_summary_on_empty_input():
    s = overall_summary([])
    assert s["n"] == 0
    assert s["mean_kalshi_clv"] is None
    assert s["win_rate"] is None


def test_overall_summary_counts_and_means():
    rows = compute_rows([
        _pos(market_price=0.40, kalshi_close_price=0.50, pnl=1.0),   # clv +0.10, won
        _pos(market_price=0.40, kalshi_close_price=0.30, pnl=-1.0),  # clv -0.10, lost
    ])
    s = overall_summary(rows)
    assert s["n"] == 2
    assert s["n_with_kalshi_clv"] == 2
    assert s["mean_kalshi_clv"] == pytest.approx(0.0)
    assert s["win_rate"] == pytest.approx(50.0)
    assert s["pct_positive_kalshi_clv"] == pytest.approx(50.0)


def test_overall_summary_excludes_unsettled_clv_from_the_denominator():
    rows = compute_rows([
        _pos(kalshi_close_price=0.50, market_price=0.40),
        _pos(kalshi_close_price=None),
    ])
    s = overall_summary(rows)
    assert s["n"] == 2
    assert s["n_with_kalshi_clv"] == 1


def test_overall_summary_mean_ev_pct():
    rows = compute_rows([
        _pos(consensus_prob=0.47, market_price=0.45),
        _pos(consensus_prob=None, market_price=0.45),  # excluded, not averaged as 0
    ])
    s = overall_summary(rows)
    assert s["n_with_ev_pct"] == 1
    assert s["mean_ev_pct"] == pytest.approx(rows[0]["ev_pct"])


# ── group_by_field ────────────────────────────────────────────────────────────────

def test_group_by_sport_splits_correctly():
    rows = compute_rows([
        _pos(sport="baseball_mlb", pnl=1.0),
        _pos(sport="baseball_mlb", pnl=-1.0),
        _pos(sport="soccer_epl", pnl=1.0),
    ])
    groups = {g["key"]: g for g in group_by_field(rows, "sport")}
    assert groups["baseball_mlb"]["n"] == 2
    assert groups["soccer_epl"]["n"] == 1


def test_groups_are_sorted_largest_first():
    rows = compute_rows([_pos(sport="a")] + [_pos(sport="b")] * 3)
    groups = group_by_field(rows, "sport")
    assert groups[0]["key"] == "b" and groups[0]["n"] == 3


def test_missing_field_falls_back_to_unknown_not_none_key():
    rows = compute_rows([_pos(bet_type=None)])
    groups = group_by_field(rows, "bet_type")
    assert groups[0]["key"] == "unknown"


# ── bucket_by_tte ─────────────────────────────────────────────────────────────────

def test_tte_buckets_partition_by_hours_before_commence():
    rows = compute_rows([
        _pos(entered_at="2026-08-20T19:30:00", commence_time="2026-08-20T20:00:00+00:00"),  # 0.5h
        _pos(entered_at="2026-08-20T15:00:00", commence_time="2026-08-20T20:00:00+00:00"),  # 5h
        _pos(entered_at="2026-08-18T20:00:00", commence_time="2026-08-20T20:00:00+00:00"),  # 48h
    ])
    buckets = {b["range"]: b["n"] for b in bucket_by_tte(rows)}
    assert buckets["0–1h"] == 1
    assert buckets["3–6h"] == 1
    assert buckets["48h+"] == 1


def test_a_bet_placed_after_commence_time_is_excluded_from_every_bucket():
    """An in-play/live line (tte_hours < 0) must not silently land in the first
    bucket -- that would misrepresent it as a fast pre-game bet."""
    rows = compute_rows([
        _pos(entered_at="2026-08-20T21:00:00", commence_time="2026-08-20T20:00:00+00:00"),
    ])
    buckets = bucket_by_tte(rows)
    assert sum(b["n"] for b in buckets) == 0


def test_bucket_win_rate_and_clv_are_computed_per_bucket():
    rows = compute_rows([
        _pos(entered_at="2026-08-20T19:30:00", commence_time="2026-08-20T20:00:00+00:00",
             pnl=1.0, market_price=0.40, kalshi_close_price=0.50),
    ])
    bucket = next(b for b in bucket_by_tte(rows) if b["range"] == "0–1h")
    assert bucket["win_rate"] == pytest.approx(100.0)
    assert bucket["mean_kalshi_clv"] == pytest.approx(0.10)


# ── weekly_clv_series ─────────────────────────────────────────────────────────────

def test_weekly_series_groups_by_iso_week_monday_start():
    rows = compute_rows([
        _pos(entered_at="2026-08-17T12:00:00"),  # Monday
        _pos(entered_at="2026-08-19T12:00:00"),  # Wednesday, same week
        _pos(entered_at="2026-08-24T12:00:00"),  # next Monday
    ])
    series = weekly_clv_series(rows)
    weeks = {w["week"]: w["n"] for w in series}
    assert weeks["2026-08-17"] == 2
    assert weeks["2026-08-24"] == 1


def test_weekly_series_is_ordered_oldest_first():
    rows = compute_rows([
        _pos(entered_at="2026-08-24T12:00:00"),
        _pos(entered_at="2026-08-10T12:00:00"),
    ])
    series = weekly_clv_series(rows)
    assert series[0]["week"] < series[-1]["week"]


def test_weekly_series_mean_clv_ignores_rows_without_a_settled_clv():
    rows = compute_rows([
        _pos(entered_at="2026-08-17T12:00:00", kalshi_close_price=0.60, market_price=0.50),
        _pos(entered_at="2026-08-18T12:00:00", kalshi_close_price=None),
    ])
    series = weekly_clv_series(rows)
    assert series[0]["n"] == 2  # both rows counted
    assert series[0]["mean_kalshi_clv"] == pytest.approx(0.10)  # only the settled one
