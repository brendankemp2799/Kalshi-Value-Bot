"""Fair-value settlement path for markets that don't resolve to a clean yes/no.

Discovered 2026-08-28 via a live reconciliation mismatch: Kalshi's portfolio API
showed 0 contracts for 4 player-prop positions we still tracked as open, one for
5+ days. Root cause: those markets resolved with `result: "scalar"` and a payout
in `settlement_value_dollars` (Kalshi's "fair market price" mechanism for a binary
market whose underlying condition can't cleanly resolve, e.g. a scratched player) —
auto_settle_positions() only recognized yes/no/void, so the check silently fell
through and these positions never got pnl computed or their status closed.

These tests pin: settle_position_at_price()'s pnl formula, the YES-denominated
settlement_value_dollars being converted correctly for a NO-side position (Kalshi's
`settlement_value_dollars` is always in YES terms, but positions.market_price is
"price of the side we hold" — a bug here would silently mis-price every NO-side
fair-value settlement), and that an unrecognized finalized result is NOT silently
closed (logs for manual review instead of guessing).
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


def _add(fresh_db, **overrides):
    kwargs = dict(
        sport="baseball_mlb", home_team="WSH", away_team="MIA", team_name="Abrams",
        platform="Kalshi", stake=1.38, market_price=0.46, is_paper=False,
        order_id="o1", execution_status="submitted", market_ticker="KXTEST-1",
        side="yes", bet_type="player_prop",
    )
    kwargs.update(overrides)
    return fresh_db.add_position(**kwargs)


# ── storage/db.py::settle_position_at_price ─────────────────────────────────────

def test_settlement_at_entry_price_is_near_breakeven(fresh_db):
    pos_id = _add(fresh_db, stake=1.38, market_price=0.46)
    pnl = fresh_db.settle_position_at_price(pos_id, 0.46)
    assert pnl == pytest.approx(0.0)


def test_settlement_above_entry_is_a_gain(fresh_db):
    pos_id = _add(fresh_db, stake=1.0, market_price=0.20)
    pnl = fresh_db.settle_position_at_price(pos_id, 0.60)
    # contracts = 1.0/0.20 = 5; pnl = 5 * (0.60 - 0.20) = 2.0
    assert pnl == pytest.approx(2.0)


def test_settlement_below_entry_is_a_loss_and_position_closes(fresh_db):
    pos_id = _add(fresh_db, stake=1.0, market_price=0.50)
    pnl = fresh_db.settle_position_at_price(pos_id, 0.10)
    assert pnl == pytest.approx(-0.8)
    row = fresh_db.get_connection().execute(
        "SELECT status, pnl FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    assert row["status"] == "closed"
    assert row["pnl"] == pytest.approx(-0.8)


def test_entry_fee_is_subtracted(fresh_db):
    pos_id = _add(fresh_db, stake=1.0, market_price=0.50, entry_fee_paid=0.05)
    pnl = fresh_db.settle_position_at_price(pos_id, 0.50)
    assert pnl == pytest.approx(-0.05)


# ── execution/auto_settle.py — scalar/fair-value branch ─────────────────────────

def _run_auto_settle(monkeypatch, pos_row, market):
    monkeypatch.setattr(auto_settle, "get_open_positions", lambda is_paper=False: [pos_row])
    monkeypatch.setattr(auto_settle, "_fetch_market", lambda ticker: market)
    return auto_settle.auto_settle_positions(is_paper=False)


def test_yes_side_scalar_market_settles_at_settlement_value(fresh_db, monkeypatch):
    pos_id = _add(fresh_db, stake=1.38, market_price=0.46, side="yes",
                  market_ticker="KXMLBTB-1")
    pos_row = fresh_db.get_connection().execute(
        "SELECT * FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    market = {
        "status": "finalized", "result": "scalar", "market_type": "binary",
        "settlement_value_dollars": "0.4600",
    }
    settled = _run_auto_settle(monkeypatch, pos_row, market)
    assert settled == 1
    row = fresh_db.get_connection().execute(
        "SELECT status, pnl FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    assert row["status"] == "closed"
    assert row["pnl"] == pytest.approx(0.0)


def test_no_side_scalar_settlement_uses_the_complement_price(fresh_db, monkeypatch):
    # NO position entered at 0.40 (i.e. yes was ~0.60 at entry). Market later
    # settles at settlement_value_dollars=0.90 (YES-denominated) -- meaning the NO
    # side we hold is actually worth only 0.10, a big loss, not a gain. A bug that
    # forgot to take the complement would price this as a gain instead.
    pos_id = _add(fresh_db, stake=1.0, market_price=0.40, side="no",
                  market_ticker="KXMLBTB-2")
    pos_row = fresh_db.get_connection().execute(
        "SELECT * FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    market = {
        "status": "finalized", "result": "scalar", "market_type": "binary",
        "settlement_value_dollars": "0.9000",
    }
    _run_auto_settle(monkeypatch, pos_row, market)
    row = fresh_db.get_connection().execute(
        "SELECT status, pnl FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    assert row["status"] == "closed"
    # contracts = 1.0/0.40 = 2.5; our price = 1-0.90 = 0.10; pnl = 2.5*(0.10-0.40) = -0.75
    assert row["pnl"] == pytest.approx(-0.75)


def test_unrecognized_finalized_result_is_not_silently_settled(fresh_db, monkeypatch):
    pos_id = _add(fresh_db, market_ticker="KXTEST-3")
    pos_row = fresh_db.get_connection().execute(
        "SELECT * FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    market = {"status": "finalized", "result": "something_unexpected"}
    settled = _run_auto_settle(monkeypatch, pos_row, market)
    assert settled == 0
    row = fresh_db.get_connection().execute(
        "SELECT status, pnl FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["pnl"] is None


def test_still_open_market_is_left_alone(fresh_db, monkeypatch):
    pos_id = _add(fresh_db, market_ticker="KXTEST-4")
    pos_row = fresh_db.get_connection().execute(
        "SELECT * FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    market = {"status": "active", "result": ""}
    settled = _run_auto_settle(monkeypatch, pos_row, market)
    assert settled == 0
