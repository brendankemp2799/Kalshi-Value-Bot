"""execution/auto_settle.py::_backfill_dk_scaled_outcomes -- resolving DK-scaled
shadow-log rows against Kalshi's own market-resolution endpoint.

Same mechanism, same shape, as _backfill_book_probability_outcomes (see that
function's tests for the pattern this mirrors). What's specific to this one: it
reads and writes dk_scaled_shadow_log, not book_probability_log, and its
kalshi_side is always "yes" or "no" (never None -- every row is written with a
resolved side, unlike book_probability_log's scan-time rows).
"""
from __future__ import annotations

import pytest

import storage.db as db
import execution.auto_settle as auto_settle


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
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


def test_a_resolved_yes_market_settles_a_yes_row_as_true(fresh_db, monkeypatch):
    db.log_dk_scaled_estimates("scan1", [_entry(kalshi_side="yes")])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: {"result": "yes"})

    auto_settle._backfill_dk_scaled_outcomes()

    rows = db.get_dk_scaled_shadow_rows(settled_only=True)
    assert len(rows) == 1 and rows[0]["actual_outcome"] == 1.0


def test_a_resolved_yes_market_settles_a_no_row_as_false(fresh_db, monkeypatch):
    db.log_dk_scaled_estimates("scan1", [_entry(kalshi_side="no")])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: {"result": "yes"})

    auto_settle._backfill_dk_scaled_outcomes()

    rows = db.get_dk_scaled_shadow_rows(settled_only=True)
    assert len(rows) == 1 and rows[0]["actual_outcome"] == 0.0


def test_a_still_open_market_is_left_pending(fresh_db, monkeypatch):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: {"result": ""})

    auto_settle._backfill_dk_scaled_outcomes()

    assert db.get_dk_scaled_shadow_rows(settled_only=True) == []
    pending = db.get_pending_dk_scaled_outcomes()
    assert len(pending) == 1 and pending[0]["outcome_check_attempts"] == 0, \
        "an unresolved market must not burn a retry attempt"


def test_a_void_market_is_recorded_without_scoring(fresh_db, monkeypatch):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: {"result": "void"})

    auto_settle._backfill_dk_scaled_outcomes()

    assert db.get_dk_scaled_shadow_rows(settled_only=True) == []
    pending = db.get_pending_dk_scaled_outcomes()
    assert len(pending) == 1 and pending[0]["outcome_check_attempts"] == 1


def test_a_missing_market_is_skipped_cleanly(fresh_db, monkeypatch):
    db.log_dk_scaled_estimates("scan1", [_entry()])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: None)

    auto_settle._backfill_dk_scaled_outcomes()  # must not raise

    assert db.get_pending_dk_scaled_outcomes()[0]["outcome_check_attempts"] == 0


def test_no_pending_rows_short_circuits_without_fetching(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: calls.append(ticker))

    auto_settle._backfill_dk_scaled_outcomes()

    assert calls == []


def test_shared_tickers_are_fetched_once(fresh_db, monkeypatch):
    """Two rows (yes + no) on the same ticker must not double-fetch the market --
    same batching as _backfill_book_probability_outcomes."""
    db.log_dk_scaled_estimates("scan1", [
        _entry(kalshi_side="yes"), _entry(kalshi_side="no"),
    ])
    calls = []

    def fake_fetch(ticker):
        calls.append(ticker)
        return {"result": "yes"}

    monkeypatch.setattr(auto_settle, "_fetch_market", fake_fetch)
    auto_settle._backfill_dk_scaled_outcomes()

    assert calls == [_entry()["kalshi_ticker"]]
    assert len(db.get_dk_scaled_shadow_rows(settled_only=True)) == 2
