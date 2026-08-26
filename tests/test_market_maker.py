"""Tests for the market-maker orphan sweeper.

execution/market_maker.py had no test coverage at all. These cover
sweep_orphaned_quotes(), added after an audit found 9 real resting orders across 5
tickers (oldest 3 days). One of them kept filling while unobserved, growing from 1 to
8 contracts of exposure that the positions table recorded as status='failed', $0.00.

The first version of the sweeper had two defects that these tests pin down so they
cannot come back:
  - it read the in-memory _resting_quotes dict, which is keyed {ticker: {yes, no}} and
    therefore collapses two orders on the same ticker+side into one;
  - it lived inside run_mm_tick(), which only runs when candidates exist -- gating an
    orphan sweep on having candidates reproduces the blind spot it exists to close.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import execution.market_maker as mm


def _order(order_id: str, ticker: str, age_seconds: float,
           filled: float = 0.0, remaining: float = 9.0) -> dict:
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "order_id": order_id,
        "ticker": ticker,
        "created_time": created.isoformat().replace("+00:00", "Z"),
        "fill_count_fp": filled,
        "remaining_count_fp": remaining,
    }


@pytest.fixture
def kalshi(monkeypatch):
    """Stub Kalshi's resting-order list and cancel endpoint."""
    import execution.kalshi_executor as ke
    state = {"orders": [], "cancelled": [], "cancel_ok": True}
    monkeypatch.setattr(ke, "list_resting_orders", lambda: state["orders"])

    def _cancel(oid, ticker):
        # ticker is required for shard routing -- a cancel that omits it 404s
        # and leaves the order LIVE, so record it and prove it was supplied.
        assert ticker, f"cancel_quote({oid!r}) called without a ticker"
        state.setdefault("cancel_tickers", []).append(ticker)
        if state["cancel_ok"]:
            state["cancelled"].append(oid)
        return state["cancel_ok"]

    monkeypatch.setattr(ke, "cancel_quote", _cancel)
    monkeypatch.setattr(mm.db, "position_exists_for_order_id", lambda oid: False)
    mm._resting_quotes.clear()
    return state


def test_cancels_old_order_on_a_ticker_no_longer_quoted(kalshi):
    kalshi["orders"] = [_order("o1", "GONE", age_seconds=86400)]
    assert mm.sweep_orphaned_quotes(active_tickers=set(), is_paper=False) == 1
    assert kalshi["cancelled"] == ["o1"]


def test_never_touches_a_ticker_still_being_quoted(kalshi):
    kalshi["orders"] = [_order("o1", "LIVE", age_seconds=86400)]
    assert mm.sweep_orphaned_quotes({"LIVE"}, is_paper=False) == 0
    assert kalshi["cancelled"] == []


def test_age_gate_protects_in_flight_directional_orders(kalshi):
    """The directional path rests GTC orders for up to 900s while polling them. A
    sweep must not cancel those out from under it, so only orders older than the
    threshold are eligible."""
    kalshi["orders"] = [
        _order("fresh", "SOMETICKER", age_seconds=120),    # a live GTC mid order
        _order("old", "SOMETICKER", age_seconds=7200),     # genuinely abandoned
    ]
    assert mm.sweep_orphaned_quotes(set(), is_paper=False) == 1
    assert kalshi["cancelled"] == ["old"]


def test_duplicate_orders_on_same_ticker_and_side_are_all_cancelled(kalshi):
    """The real orphan set contained PAIRS of 9-contract orders on one ticker+side.
    A dict keyed {ticker: {yes, no}} keeps only the last of those, so the earlier
    implementation left one live. Working from Kalshi's order list fixes it."""
    kalshi["orders"] = [
        _order("dup1", "CINNYC", age_seconds=86400),
        _order("dup2", "CINNYC", age_seconds=86400),
    ]
    assert mm.sweep_orphaned_quotes(set(), is_paper=False) == 2
    assert sorted(kalshi["cancelled"]) == ["dup1", "dup2"]


def test_works_with_no_candidates_at_all(kalshi):
    """The blind spot being closed: an orphan is BY DEFINITION not a candidate, so an
    empty candidate list must not disable the sweep."""
    kalshi["orders"] = [_order("o1", "T1", age_seconds=86400)]
    assert mm.sweep_orphaned_quotes(active_tickers=set(), is_paper=False) == 1


def test_partially_filled_orphan_alerts_at_critical_and_still_cancels(kalshi, caplog):
    kalshi["orders"] = [_order("part", "RSLDAL", age_seconds=86400, filled=8.0)]
    with caplog.at_level("CRITICAL"):
        mm.sweep_orphaned_quotes(set(), is_paper=False)
    assert "ORPHAN FILL" in caplog.text
    assert "part" in caplog.text, "order_id must be in the alert to reconcile by hand"
    assert kalshi["cancelled"] == ["part"]


def test_already_recorded_fill_does_not_re_alert(kalshi, caplog, monkeypatch):
    monkeypatch.setattr(mm.db, "position_exists_for_order_id", lambda oid: True)
    kalshi["orders"] = [_order("known", "T1", age_seconds=86400, filled=9.0)]
    with caplog.at_level("CRITICAL"):
        mm.sweep_orphaned_quotes(set(), is_paper=False)
    assert "ORPHAN FILL" not in caplog.text


def test_failed_cancel_is_logged_as_error_not_silently_swallowed(kalshi, caplog):
    """A cancel that fails leaves a live order that can still fill -- the exact
    condition that created the untracked position, so it must be loud."""
    kalshi["cancel_ok"] = False
    kalshi["orders"] = [_order("stuck", "T1", age_seconds=86400)]
    with caplog.at_level("ERROR"):
        assert mm.sweep_orphaned_quotes(set(), is_paper=False) == 0
    assert "FAILED to cancel" in caplog.text


def test_paper_mode_never_touches_live_orders(kalshi):
    kalshi["orders"] = [_order("o1", "T1", age_seconds=86400)]
    assert mm.sweep_orphaned_quotes(set(), is_paper=True) == 0
    assert kalshi["cancelled"] == []


def test_listing_failure_is_survivable(monkeypatch):
    """A Kalshi outage must not take down the tick loop."""
    import execution.kalshi_executor as ke
    monkeypatch.setattr(ke, "list_resting_orders",
                        lambda: (_ for _ in ()).throw(RuntimeError("api down")))
    assert mm.sweep_orphaned_quotes(set(), is_paper=False) == 0


# ── startup recovery: duplicate quotes ────────────────────────────────────────

@pytest.fixture
def recovery(monkeypatch):
    """Stub Kalshi for _sync_resting_quotes_from_kalshi()."""
    import execution.kalshi_executor as ke
    state = {"orders": [], "cancelled": [], "cancel_ok": True}
    monkeypatch.setattr(ke, "list_resting_orders", lambda: state["orders"])

    def _cancel(oid, ticker):
        # ticker is required for shard routing -- a cancel that omits it 404s
        # and leaves the order LIVE, so record it and prove it was supplied.
        assert ticker, f"cancel_quote({oid!r}) called without a ticker"
        state.setdefault("cancel_tickers", []).append(ticker)
        if state["cancel_ok"]:
            state["cancelled"].append(oid)
        return state["cancel_ok"]

    monkeypatch.setattr(ke, "cancel_quote", _cancel)
    mm._resting_quotes.clear()
    monkeypatch.setattr(mm, "_startup_synced", False, raising=False)
    return state


def _resting(order_id: str, ticker: str, side: str, count: float = 9.0) -> dict:
    return {
        "order_id": order_id, "ticker": ticker, "outcome_side": side,
        "remaining_count_fp": count,
        "yes_price_dollars": "0.48", "no_price_dollars": "0.52",
    }


def test_recovery_keeps_one_quote_per_side(recovery):
    recovery["orders"] = [
        _resting("y1", "T1", "yes"),
        _resting("n1", "T1", "no"),
    ]
    mm._sync_resting_quotes_from_kalshi()
    assert mm._resting_quotes["T1"]["yes"]["order_id"] == "y1"
    assert mm._resting_quotes["T1"]["no"]["order_id"] == "n1"
    assert recovery["cancelled"] == []


def test_recovery_cancels_duplicate_on_same_ticker_and_side(recovery, caplog):
    """The real orphan set had PAIRS on one ticker+side. _resting_quotes can only hold
    one, and the previous version let the later order silently overwrite the earlier —
    leaving it live and untracked forever."""
    recovery["orders"] = [
        _resting("keep", "CINNYC", "yes"),
        _resting("dup", "CINNYC", "yes"),
    ]
    with caplog.at_level("CRITICAL"):
        mm._sync_resting_quotes_from_kalshi()
    assert mm._resting_quotes["CINNYC"]["yes"]["order_id"] == "keep"
    assert recovery["cancelled"] == ["dup"], "the extra must be cancelled, not dropped"
    assert "DUPLICATE" in caplog.text


def test_recovery_failed_duplicate_cancel_is_logged_as_error(recovery, caplog):
    recovery["cancel_ok"] = False
    recovery["orders"] = [
        _resting("keep", "T1", "yes"),
        _resting("stuck", "T1", "yes"),
    ]
    with caplog.at_level("ERROR"):
        mm._sync_resting_quotes_from_kalshi()
    assert "FAILED to cancel duplicate" in caplog.text
