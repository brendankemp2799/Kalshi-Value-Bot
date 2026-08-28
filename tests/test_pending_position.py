"""Regression coverage for the 2026-08-28 untracked-fill incident.

A real fill (Cam Schlittler 8+ strikeouts, 3 contracts) landed on Kalshi but was
never recorded anywhere: the worker thread placing it was killed by a routine bot
restart while still inside place_order()'s poll loop (up to 15 minutes), and the
DB write only ever happened AFTER that loop returned. The order rests on Kalshi
independently of our process once accepted, but the thread that would have
recorded it does not survive a restart.

Fix: place_order() now calls on_order_placed(order_id) the moment Kalshi confirms
the order — before the poll loop — so main.py can write a 'pending' position row
immediately. These tests pin: the callback fires before/regardless of poll outcome,
it does NOT fire when no order is ever placed, a callback exception never aborts a
live order, and finalize_pending_position() correctly resolves a pending row.
"""
from __future__ import annotations

import time as time_module

import pytest

import config
import execution.kalshi_executor as ke
import storage.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


@pytest.fixture
def kalshi_key(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_API_KEY", "fake-key-for-tests")


# ── on_order_placed callback ────────────────────────────────────────────────────

def test_callback_fires_on_immediate_fill(monkeypatch, kalshi_key):
    monkeypatch.setattr(ke, "_place_raw_order", lambda *a, **kw: {
        "order_id": "order-123", "fill_count": 10,
    })
    monkeypatch.setattr(ke, "_classify_fill", lambda *a, **kw: ("taker", 0.05))

    seen = []
    order_id, status, reason, stake, fill_type, fee = ke.place_order(
        ticker="KXTEST-1", side="yes", stake_dollars=4.0, market_price=0.40,
        on_order_placed=seen.append,
    )
    assert status == "submitted"
    assert seen == ["order-123"]


def test_callback_fires_before_poll_loop_resolves(monkeypatch, kalshi_key):
    """The callback must fire even when the order is NOT immediately filled —
    i.e. before place_order() enters its blocking poll loop, not after. The poll
    loop resolves on its first check (full fill) so this test doesn't burn real
    wall-clock time waiting out a multi-minute timeout."""
    monkeypatch.setattr(ke, "_place_raw_order", lambda *a, **kw: {
        "order_id": "order-456", "fill_count": 0,
    })
    monkeypatch.setattr(ke, "_classify_fill", lambda *a, **kw: ("maker", 0.01))
    monkeypatch.setattr(ke, "_cancel_order", lambda *a, **kw: True)
    monkeypatch.setattr(time_module, "sleep", lambda *_: None)

    call_order = []

    def _on_placed(order_id):
        call_order.append(("callback", order_id))

    def _fake_status(order_id, retries=0):
        call_order.append(("poll",))
        return {"fill_count_fp": 10}  # fills on the very first poll check

    monkeypatch.setattr(ke, "_get_order_status", _fake_status)

    order_id, status, reason, stake, fill_type, fee = ke.place_order(
        ticker="KXTEST-2", side="yes", stake_dollars=4.0, market_price=0.40,
        commence_time=None, maker_only=True,
        on_order_placed=_on_placed,
    )

    assert status == "submitted"
    assert ("callback", "order-456") in call_order
    # callback must precede the first poll iteration
    assert call_order.index(("callback", "order-456")) < call_order.index(("poll",))


def test_callback_not_called_when_no_order_placed(kalshi_key):
    """KALSHI_API_KEY missing (or any early-return before an order exists) must
    never invoke the callback — there is nothing to write a pending row for."""
    import config as cfg
    old_key = cfg.KALSHI_API_KEY
    cfg.KALSHI_API_KEY = ""
    try:
        seen = []
        ke.place_order(
            ticker="KXTEST-3", side="yes", stake_dollars=4.0, market_price=0.40,
            on_order_placed=seen.append,
        )
        assert seen == []
    finally:
        cfg.KALSHI_API_KEY = old_key


def test_callback_exception_does_not_abort_order_placement(monkeypatch, kalshi_key):
    """A bug in the (storage-layer) callback must never take down a live order."""
    monkeypatch.setattr(ke, "_place_raw_order", lambda *a, **kw: {
        "order_id": "order-789", "fill_count": 10,
    })
    monkeypatch.setattr(ke, "_classify_fill", lambda *a, **kw: ("taker", 0.05))

    def _boom(order_id):
        raise RuntimeError("db is on fire")

    order_id, status, reason, stake, fill_type, fee = ke.place_order(
        ticker="KXTEST-4", side="yes", stake_dollars=4.0, market_price=0.40,
        on_order_placed=_boom,
    )
    assert status == "submitted"
    assert order_id == "order-789"


# ── finalize_pending_position ────────────────────────────────────────────────────

def _add_pending(fresh_db, **overrides):
    kwargs = dict(
        sport="baseball_mlb", home_team="NYY", away_team="BOS", team_name="Cam S 8+",
        platform="Kalshi", stake=4.0, market_price=0.40, is_paper=False,
        order_id="order-abc", execution_status="pending",
        market_ticker="KXTEST-5", side="yes", bet_type="player_prop",
    )
    kwargs.update(overrides)
    return fresh_db.add_position(**kwargs)


def test_finalize_resolves_pending_row_to_submitted(fresh_db):
    pos_id = _add_pending(fresh_db)
    row = fresh_db.get_connection().execute(
        "SELECT status, execution_status, stake FROM positions WHERE id=?", (pos_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["execution_status"] == "pending"

    fresh_db.finalize_pending_position(
        pos_id, stake=3.5, execution_status="submitted", fill_type="maker",
        entry_fee_paid=0.02, failure_reason=None,
    )
    row = fresh_db.get_connection().execute(
        "SELECT status, execution_status, stake, fill_type, entry_fee_paid "
        "FROM positions WHERE id=?", (pos_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["execution_status"] == "submitted"
    assert row["stake"] == 3.5
    assert row["fill_type"] == "maker"
    assert row["entry_fee_paid"] == 0.02


def test_finalize_resolves_pending_row_to_failed(fresh_db):
    pos_id = _add_pending(fresh_db)
    fresh_db.finalize_pending_position(
        pos_id, stake=0.0, execution_status="failed", fill_type="",
        entry_fee_paid=0.0, failure_reason="GTC mid unfilled after 900s",
    )
    row = fresh_db.get_connection().execute(
        "SELECT status, execution_status, failure_reason FROM positions WHERE id=?",
        (pos_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["execution_status"] == "failed"
    assert row["failure_reason"] == "GTC mid unfilled after 900s"


def test_a_pending_row_counts_toward_open_positions(fresh_db):
    """A restart-orphaned order must still be visible to exposure/correlation
    tracking, not silently invisible the way it was before this fix."""
    _add_pending(fresh_db)
    assert fresh_db.count_open_positions(is_paper=False) == 1
