"""Regression coverage for the 2026-09-09 daily capital-risk cap fix.

get_daily_stake_total() feeds config.MAX_DAILY_CAPITAL_RISK_PCT's gate in main.py.
It used to sum EVERY stake entered today (UTC), including ones that had already
settled -- so a day's cumulative activity could keep the gate tripped for the rest
of the day even though most of that money was no longer at risk. Real example:
$54.42 staked on 2026-09-08 tripped a $54.00 cap, but $19.92 of that had already
settled by evening, leaving only ~$34.50 actually open -- yet the bot kept refusing
new bets against the full $54.42 for hours.

Fixed to sum only today's positions that are STILL OPEN (status='open'), while
still resetting at UTC midnight like before -- see storage/db.py's docstring.
"""
from __future__ import annotations

import pytest

import storage.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()


def _open_position(stake: float) -> int:
    return db.add_position(
        sport="baseball_mlb", home_team="A", away_team="B", team_name="A",
        platform="Kalshi", stake=stake, market_price=0.5,
        execution_status="submitted",
    )


def test_settled_positions_from_today_do_not_count(fresh_db):
    still_open = _open_position(10.0)
    _open_position(5.0)  # will be settled below
    settled_id = _open_position(5.0)
    db.settle_position(settled_id, "won")

    assert db.get_daily_stake_total(is_paper=False) == pytest.approx(15.0)


def test_failed_orders_never_counted(fresh_db):
    _open_position(10.0)
    db.add_position(
        sport="baseball_mlb", home_team="A", away_team="B", team_name="A",
        platform="Kalshi", stake=999.0, market_price=0.5,
        execution_status="failed",
    )
    assert db.get_daily_stake_total(is_paper=False) == pytest.approx(10.0)


def test_paper_and_live_stakes_are_tracked_separately(fresh_db):
    db.add_position(
        sport="baseball_mlb", home_team="A", away_team="B", team_name="A",
        platform="Kalshi", stake=20.0, market_price=0.5,
        execution_status="submitted", is_paper=True,
    )
    _open_position(10.0)

    assert db.get_daily_stake_total(is_paper=False) == pytest.approx(10.0)
    assert db.get_daily_stake_total(is_paper=True) == pytest.approx(20.0)
