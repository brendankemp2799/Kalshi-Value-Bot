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


def test_callback_fires_twice_on_the_real_step1_timeout_step2_path(monkeypatch, kalshi_key):
    """Exercises place_order()'s actual step-1-times-out-then-step-2 fallthrough
    (not a DB-layer simulation): step 1 places an order that never fills and gets
    cancelled once the poll deadline passes, step 2 places a NEW order that fills
    immediately. on_order_placed must fire once per real order (twice total), with
    DIFFERENT order_ids -- this is the exact sequence that produced the phantom
    pending row on 2026-08-29."""
    calls = {"n": 0}

    def _fake_place_raw_order(ticker, api_side, price, count, tif, client_order_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"order_id": "step1-order", "fill_count": 0}  # step 1: unfilled
        return {"order_id": "step2-order", "fill_count": count}   # step 2: fills

    monkeypatch.setattr(ke, "_place_raw_order", _fake_place_raw_order)
    monkeypatch.setattr(ke, "_classify_fill", lambda *a, **kw: ("taker", 0.03))
    monkeypatch.setattr(ke, "_cancel_order", lambda *a, **kw: True)
    monkeypatch.setattr(ke, "_get_order_status", lambda *a, **kw: {"fill_count_fp": 0})
    monkeypatch.setattr(time_module, "sleep", lambda *_: None)

    # Fast-forward time.time() so the poll loop's deadline passes after one check,
    # instead of a real (up to 900s) wait.
    fake_clock = {"t": 1000.0}
    def _fake_time():
        fake_clock["t"] += 1000.0
        return fake_clock["t"]
    monkeypatch.setattr(time_module, "time", _fake_time)

    seen_order_ids = []
    order_id, status, reason, stake, fill_type, fee = ke.place_order(
        ticker="KXTEST-6", side="yes", stake_dollars=4.0, market_price=0.40,
        commence_time=None, on_order_placed=seen_order_ids.append,
    )

    assert status == "submitted"
    assert seen_order_ids == ["step1-order", "step2-order"], (
        "expected exactly one callback per real order, step 1 then step 2"
    )
    assert order_id == "step2-order"


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


# ── update_pending_order_id: the step-1-then-step-2 phantom-row regression ──────
#
# Discovered 2026-08-29: place_order() can place TWO real orders for one logical
# trade attempt -- step 1 (GTC at mid) times out and is cancelled ($0 filled), then
# step 2 (GTC at ask) places a fresh order that actually fills. on_order_placed
# fired once per real order, so main.py's pending_holder wrote a SECOND row for
# step 2 and forgot about the first -- which sat as phantom 'pending' exposure
# forever, since nothing ever finalized it. A real instance: id was never
# finalized, Kalshi showed the true 2-contract position while the bot's own
# count_open_positions/total_at_risk double-counted an extra ~2.1 contracts that
# were never actually bought.

def test_update_pending_order_id_repoints_without_a_second_row(fresh_db):
    pos_id = _add_pending(fresh_db, order_id="step1-order-id")
    fresh_db.update_pending_order_id(pos_id, "step2-order-id")

    rows = fresh_db.get_connection().execute(
        "SELECT id, order_id, execution_status FROM positions"
    ).fetchall()
    assert len(rows) == 1, "step 2 must not create a second row for the same trade"
    assert rows[0]["order_id"] == "step2-order-id"
    assert rows[0]["execution_status"] == "pending"


def test_the_step1_cancel_then_step2_fill_sequence_ends_with_one_finalized_row(fresh_db):
    """Reproduces the exact 2026-08-29 sequence: step 1 gets a pending row, times
    out and is cancelled with $0 filled, step 2 places a new order that fills --
    the SAME row must end up finalized with step 2's real numbers, no orphan."""
    pos_id = _add_pending(fresh_db, order_id="step1-order-id", stake=0.90)
    # step 2 places a fresh order for the same trade attempt
    fresh_db.update_pending_order_id(pos_id, "step2-order-id")
    # step 2 fills for less than originally requested (2 contracts @ 0.42, not 2.14)
    fresh_db.finalize_pending_position(
        pos_id, stake=0.84, execution_status="submitted", fill_type="taker",
        entry_fee_paid=0.0342, failure_reason=None,
    )

    rows = fresh_db.get_connection().execute("SELECT * FROM positions").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["order_id"] == "step2-order-id"
    assert row["execution_status"] == "submitted"
    assert row["stake"] == 0.84
    # Exposure tracking must reflect the real $0.84, not the stale $0.90 request
    assert fresh_db.get_open_positions(is_paper=False)[0]["stake"] == 0.84
