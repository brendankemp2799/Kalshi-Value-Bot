"""research/metrics.py::counterfactual_backtest -- would the bot have done just
as well betting every qualifying candidate flat, or a random same-sized subset
of them, as it did with the composite-score ranking that actually picks 5/day?

Pure-function tests only (no DB) -- candidates/real_positions are plain dicts,
same house style as test_clv_analytics.py. See tests/test_metrics_counterfactual_db.py
[inline below, DB-layer section] for storage/db.py::get_qualifying_candidates_with_outcomes.
"""
from __future__ import annotations

import pytest

import research.metrics as metrics
import storage.db as db


def _cand(kalshi_price=0.50, actual_outcome=1.0, position_id=None, **overrides):
    base = dict(
        kalshi_price=kalshi_price, edge=0.03, actual_outcome=actual_outcome,
        position_id=position_id, sport="baseball_mlb", bet_type="h2h",
        scanned_at="2026-08-20T18:00:00",
    )
    base.update(overrides)
    return base


def _pos(pnl=1.0, stake=10.0, **overrides):
    base = dict(pnl=pnl, stake=stake)
    base.update(overrides)
    return base


# ── flat_all_qualifiers / random_n_of_qualifiers math ──────────────────────────

def test_flat_all_qualifiers_uses_every_candidate_at_unit_stake():
    # All 4 candidates win at price 0.50 -> pnl = 10*(1-0.5)/0.5 = 10 each.
    candidates = [_cand(kalshi_price=0.50, actual_outcome=1.0) for _ in range(4)]
    out = metrics.counterfactual_backtest(candidates, [], unit_stake=10.0)
    assert out["flat_all_qualifiers"]["n"] == 4
    assert out["flat_all_qualifiers"]["roi_pct"] == pytest.approx(100.0)
    assert out["flat_all_qualifiers"]["win_rate_pct"] == pytest.approx(100.0)


def test_flat_all_qualifiers_mixes_wins_and_losses():
    candidates = [
        _cand(kalshi_price=0.50, actual_outcome=1.0),  # +10
        _cand(kalshi_price=0.50, actual_outcome=0.0),  # -10
    ]
    out = metrics.counterfactual_backtest(candidates, [], unit_stake=10.0)
    assert out["flat_all_qualifiers"]["n"] == 2
    assert out["flat_all_qualifiers"]["roi_pct"] == pytest.approx(0.0)
    assert out["flat_all_qualifiers"]["win_rate_pct"] == pytest.approx(50.0)


def test_random_n_of_qualifiers_draws_exactly_n_placed():
    """n_actual_placed = count of candidates with a position_id set. Here 2 of 5
    qualifying candidates were actually placed, so the random leg should draw 2."""
    candidates = [_cand(actual_outcome=1.0, position_id=1),
                  _cand(actual_outcome=1.0, position_id=2)]
    candidates += [_cand(actual_outcome=0.0) for _ in range(3)]  # unplaced
    out = metrics.counterfactual_backtest(candidates, [], unit_stake=10.0)
    assert out["random_n_of_qualifiers"]["n_sampled"] == 2
    assert out["random_n_of_qualifiers"]["n"] == 2


def test_candidates_without_a_positive_price_are_skipped_not_crashed():
    candidates = [_cand(kalshi_price=None), _cand(kalshi_price=0.0),
                  _cand(kalshi_price=0.50, actual_outcome=1.0)]
    out = metrics.counterfactual_backtest(candidates, [], unit_stake=10.0)
    assert out["flat_all_qualifiers"]["n"] == 1


# ── actual leg reuses roi()/win_rate() on real_positions ───────────────────────

def test_actual_leg_matches_roi_and_win_rate_directly():
    positions = [_pos(pnl=5.0, stake=10.0), _pos(pnl=-10.0, stake=10.0)]
    expected_roi, expected_n = metrics.roi(positions)
    expected_win, _ = metrics.win_rate(positions)
    out = metrics.counterfactual_backtest([], positions, unit_stake=10.0)
    assert out["actual"] == {"n": expected_n, "roi_pct": expected_roi, "win_rate_pct": expected_win}


# ── verdict direction ───────────────────────────────────────────────────────────

def test_verdict_says_ranking_adds_value_when_actual_beats_both_baselines():
    # Full qualifying pool is a coin flip at even money -> ~0% ROI baseline.
    candidates = [_cand(kalshi_price=0.50, actual_outcome=1.0, position_id=i)
                  for i in range(1, 6)]
    candidates += [_cand(kalshi_price=0.50, actual_outcome=0.0) for _ in range(15)]
    # But the actual placed bets (what the ranking picked) all won.
    positions = [_pos(pnl=10.0, stake=10.0) for _ in range(5)]
    out = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=1)
    assert out["actual"]["roi_pct"] == pytest.approx(100.0)
    assert out["verdict"].startswith("RANKING ADDS VALUE")


def test_verdict_says_ranking_not_adding_value_when_actual_lags_both_baselines():
    # Full qualifying pool wins every time -> great baseline.
    candidates = [_cand(kalshi_price=0.50, actual_outcome=1.0, position_id=i)
                  for i in range(1, 6)]
    candidates += [_cand(kalshi_price=0.50, actual_outcome=1.0) for _ in range(15)]
    # But the actual placed bets all lost.
    positions = [_pos(pnl=-10.0, stake=10.0) for _ in range(5)]
    out = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=1)
    assert out["verdict"].startswith("RANKING NOT ADDING VALUE")


def test_verdict_is_no_data_when_no_real_positions_settled():
    out = metrics.counterfactual_backtest([_cand()], [], unit_stake=10.0)
    assert out["verdict"].startswith("NO DATA")


def test_verdict_handles_one_baseline_available_without_misreporting_as_a_loss():
    """Regression: if the random leg draws zero candidates (nothing was ever
    actually placed out of this qualifying pool, so n_actual_placed == 0) while
    the flat-all leg still has a valid ROI, `rand_roi` is None but `flat_roi`
    isn't. The verdict must say "one baseline unavailable", NOT silently treat
    the missing random-leg comparison as "actual failed to beat it" -- that
    would misreport "no comparison possible" as "randomly beaten" in text an
    autonomous agent is meant to trust verbatim."""
    candidates = [_cand(kalshi_price=0.50, actual_outcome=1.0, position_id=None)
                  for _ in range(5)]  # all qualify, none were ever placed
    positions = [_pos(pnl=10.0, stake=10.0)]  # but some other real trade exists
    out = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=1)
    assert out["random_n_of_qualifiers"]["n_sampled"] == 0
    assert out["random_n_of_qualifiers"]["roi_pct"] is None
    assert out["flat_all_qualifiers"]["roi_pct"] is not None
    assert out["verdict"].startswith("PARTIAL COMPARISON")
    assert "not a full verdict" in out["verdict"]


# ── determinism ─────────────────────────────────────────────────────────────────

def test_identical_input_produces_byte_identical_output():
    candidates = [_cand(kalshi_price=0.4 + 0.01 * i, actual_outcome=float(i % 2),
                         position_id=(i if i < 3 else None))
                  for i in range(10)]
    positions = [_pos(pnl=1.0 if i % 2 else -1.0, stake=10.0) for i in range(6)]
    out1 = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=42)
    out2 = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=42)
    assert out1 == out2


def test_different_seeds_do_not_change_the_deterministic_legs():
    """The seed only affects which candidates the random leg draws -- flat_all
    and actual must not move with it."""
    candidates = [_cand(kalshi_price=0.5, actual_outcome=float(i % 2), position_id=i)
                  for i in range(10)]
    positions = [_pos(pnl=1.0, stake=10.0)]
    out1 = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=1)
    out2 = metrics.counterfactual_backtest(candidates, positions, unit_stake=10.0, seed=2)
    assert out1["flat_all_qualifiers"] == out2["flat_all_qualifiers"]
    assert out1["actual"] == out2["actual"]


# ── empty / tiny pools don't crash ──────────────────────────────────────────────

def test_empty_candidates_and_positions_does_not_crash():
    out = metrics.counterfactual_backtest([], [], unit_stake=10.0)
    assert out["flat_all_qualifiers"] == {"n": 0, "roi_pct": None, "win_rate_pct": None}
    assert out["random_n_of_qualifiers"]["n_sampled"] == 0
    assert out["actual"] == {"n": 0, "roi_pct": None, "win_rate_pct": None}
    assert isinstance(out["verdict"], str)


def test_single_candidate_pool_does_not_crash():
    out = metrics.counterfactual_backtest([_cand(position_id=1)], [_pos()], unit_stake=10.0)
    assert out["flat_all_qualifiers"]["n"] == 1
    assert out["random_n_of_qualifiers"]["n_sampled"] == 1


# ── DB layer: get_qualifying_candidates_with_outcomes ───────────────────────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _insert_bpl_row(conn, **overrides):
    row = dict(
        scanned_at="2026-08-20T18:00:00", sport="baseball_mlb", bet_type="h2h",
        team_name="Dodgers", threshold=None, kalshi_ticker="KXTEST-1",
        kalshi_side="yes", consensus_prob=0.55, bookmaker_count=5,
        bookmakers_json=None, commence_time="2026-08-20T20:00:00+00:00",
        kalshi_price=0.50, edge=0.03, status="value", reason="Edge found",
        actual_outcome=None, position_id=None,
    )
    row.update(overrides)
    conn.execute(
        """
        INSERT INTO book_probability_log
            (scanned_at, sport, bet_type, team_name, threshold, kalshi_ticker,
             kalshi_side, consensus_prob, bookmaker_count, bookmakers_json,
             commence_time, kalshi_price, edge, status, reason, actual_outcome,
             position_id)
        VALUES
            (:scanned_at, :sport, :bet_type, :team_name, :threshold, :kalshi_ticker,
             :kalshi_side, :consensus_prob, :bookmaker_count, :bookmakers_json,
             :commence_time, :kalshi_price, :edge, :status, :reason, :actual_outcome,
             :position_id)
        """,
        row,
    )


def test_get_qualifying_candidates_requires_status_value(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=1.0)
        _insert_bpl_row(conn, status="no_edge", actual_outcome=1.0)
        _insert_bpl_row(conn, status="kelly_no_edge", actual_outcome=0.0)
    rows = fresh_db.get_qualifying_candidates_with_outcomes()
    assert len(rows) == 1


def test_get_qualifying_candidates_requires_resolved_outcome(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=None)  # unresolved/void
        _insert_bpl_row(conn, status="value", actual_outcome=0.0)
    rows = fresh_db.get_qualifying_candidates_with_outcomes()
    assert len(rows) == 1
    assert rows[0]["actual_outcome"] == 0.0


def test_get_qualifying_candidates_requires_edge(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=1.0, edge=None)  # pre-widening row
        _insert_bpl_row(conn, status="value", actual_outcome=1.0, edge=0.02)
    rows = fresh_db.get_qualifying_candidates_with_outcomes()
    assert len(rows) == 1


def test_get_qualifying_candidates_carries_position_id_when_placed(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=1.0, position_id=99)
        _insert_bpl_row(conn, status="value", actual_outcome=1.0, position_id=None)
    rows = fresh_db.get_qualifying_candidates_with_outcomes()
    ids = sorted(r["position_id"] for r in rows if r["position_id"] is not None)
    assert ids == [99]
    assert sum(1 for r in rows if r["position_id"] is None) == 1


def test_get_qualifying_candidates_does_not_require_is_paper_column(fresh_db):
    """book_probability_log has no is_paper column -- the param must not blow up
    looking for one, regardless of what's passed."""
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=1.0)
    assert len(fresh_db.get_qualifying_candidates_with_outcomes(is_paper=True)) == 1
    assert len(fresh_db.get_qualifying_candidates_with_outcomes(is_paper=False)) == 1


def test_get_qualifying_candidates_returns_expected_columns(fresh_db):
    with fresh_db.get_connection() as conn:
        _insert_bpl_row(conn, status="value", actual_outcome=1.0, position_id=5)
    row = fresh_db.get_qualifying_candidates_with_outcomes()[0]
    for field in ("kalshi_price", "edge", "actual_outcome", "position_id",
                  "sport", "bet_type", "scanned_at"):
        assert field in row


def test_get_qualifying_candidates_on_empty_db_is_empty_list(fresh_db):
    assert fresh_db.get_qualifying_candidates_with_outcomes() == []
