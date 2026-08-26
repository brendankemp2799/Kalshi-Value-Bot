"""Shard collateral planning.

The planner is pure so the money-moving decisions can be tested exhaustively
without touching the network. The I/O half is deliberately thin.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "arbitrage_betting_bot"
sys.path.insert(0, str(ROOT))

from execution.shard_balancer import (  # noqa: E402
    CENTICENTS_PER_DOLLAR, Transfer, parse_targets, plan_transfers,
)

PLAN = dict(min_transfer=5.0, max_transfer=100.0)


# ── target parsing ───────────────────────────────────────────────────────────

def test_parses_a_split():
    assert parse_targets("0:50,3:50") == {0: 0.5, 3: 0.5}


def test_uneven_split():
    assert parse_targets("0:40,3:60") == {0: 0.4, 3: 0.6}


def test_empty_spec_disables_rebalancing():
    assert parse_targets("") == {}


def test_a_spec_that_does_not_total_100_is_refused():
    """Silently normalising a typo would move real money to a split nobody chose."""
    with pytest.raises(ValueError):
        parse_targets("0:50,3:40")


# ── planning ─────────────────────────────────────────────────────────────────

def test_moves_cash_toward_target():
    plans = plan_transfers({0: 180.0, 3: 0.0}, {}, {0: 0.5, 3: 0.5}, **PLAN)
    assert plans == [Transfer(source=0, destination=3, dollars=90.0)]


def test_nothing_to_do_when_already_on_target():
    assert plan_transfers({0: 90.0, 3: 90.0}, {}, {0: 0.5, 3: 0.5}, **PLAN) == []


def test_small_drift_is_left_alone():
    """Below the floor we do not churn."""
    assert plan_transfers({0: 92.0, 3: 88.0}, {}, {0: 0.5, 3: 0.5}, **PLAN) == []


def test_resting_orders_are_not_swept_out_from_under_working_orders():
    """Cash backing a live resting order is spoken for and must not be moved."""
    # shard 0 holds 180 but 150 of it is committed to resting orders.
    plans = plan_transfers({0: 180.0, 3: 0.0}, {0: 150.0}, {0: 0.5, 3: 0.5}, **PLAN)
    assert plans == [Transfer(source=0, destination=3, dollars=30.0)], (
        "planner tried to move collateral that is backing live orders"
    )


def test_a_fully_committed_shard_sends_nothing():
    assert plan_transfers({0: 180.0, 3: 0.0}, {0: 180.0}, {0: 0.5, 3: 0.5}, **PLAN) == []


def test_single_transfer_is_capped():
    plans = plan_transfers({0: 1000.0, 3: 0.0}, {}, {0: 0.5, 3: 0.5},
                           min_transfer=5.0, max_transfer=100.0)
    assert all(t.dollars <= 100.0 for t in plans)


def test_neediest_shard_is_served_first_when_funds_are_short():
    cash = {0: 300.0, 2: 0.0, 3: 0.0}
    targets = {0: 0.34, 2: 0.33, 3: 0.33}
    plans = plan_transfers(cash, {}, targets, min_transfer=5.0, max_transfer=20.0)
    assert plans, "expected some movement"
    assert plans[0].destination in (2, 3)


def test_zero_balance_account_plans_nothing():
    assert plan_transfers({0: 0.0, 3: 0.0}, {}, {0: 0.5, 3: 0.5}, **PLAN) == []


def test_no_targets_means_disabled():
    assert plan_transfers({0: 180.0, 3: 0.0}, {}, {}, **PLAN) == []


def test_planner_never_moves_more_than_exists():
    plans = plan_transfers({0: 10.0, 3: 0.0}, {}, {0: 0.5, 3: 0.5}, **PLAN)
    assert sum(t.dollars for t in plans) <= 10.0


# ── the unit trap ────────────────────────────────────────────────────────────

def test_dollars_convert_to_centicents_not_cents():
    """Kalshi's transfer amount is in CENTICENTS -- 1/100 of a cent.

    The subaccount-transfer endpoint next door uses integer CENTS, so mixing
    them up is a 100x error in either direction on live money.
    """
    assert CENTICENTS_PER_DOLLAR == 10_000
    assert int(round(10.00 * CENTICENTS_PER_DOLLAR)) == 100_000
    assert int(round(0.11 * CENTICENTS_PER_DOLLAR)) == 1_100


def test_submitted_body_uses_shard_fields_not_the_instance_enum(monkeypatch):
    """source/destination are the instance enum; the shard number is separate.

    Putting the shard in `source`/`destination` is the natural misreading of
    this API and would either error or move money somewhere unintended.
    """
    import execution.shard_balancer as sb
    sent = {}

    class _R:
        status_code = 200
        text = ""
        def json(self): return {"transfer_id": "tid-1"}

    class _S:
        def post(self, url, **kw):
            sent.update(kw.get("json", {}))
            return _R()

    import data.kalshi_auth as ka
    monkeypatch.setattr(ka, "auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(ka, "session", lambda: _S())

    assert sb.submit_transfer(Transfer(0, 3, 10.0)) == "tid-1"
    assert sent["source"] == "event_contract"
    assert sent["destination"] == "event_contract"
    assert sent["source_exchange_shard"] == 0
    assert sent["destination_exchange_shard"] == 3
    assert sent["amount"] == 100_000


def test_a_rejected_transfer_returns_none_and_is_not_retried(monkeypatch):
    import execution.shard_balancer as sb
    calls = []

    class _R:
        status_code = 400
        text = '{"error":"nope"}'
        def json(self): return {}

    class _S:
        def post(self, url, **kw):
            calls.append(1)
            return _R()

    import data.kalshi_auth as ka
    monkeypatch.setattr(ka, "auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(ka, "session", lambda: _S())

    assert sb.submit_transfer(Transfer(0, 3, 10.0)) is None
    assert calls == [1], "a failed transfer was retried — it can move money twice"
