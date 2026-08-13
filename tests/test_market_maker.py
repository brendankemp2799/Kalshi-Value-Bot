"""Tests for the market-maker orphan sweeper.

execution/market_maker.py had no test coverage at all, despite evaluate_mm_candidate()
and friends being written as pure functions specifically so they could be tested. These
cover _sweep_orphaned_quotes(), added after an audit found 9 real resting orders across
5 tickers (oldest 3 days) that the per-candidate loop could never revisit.
"""
from __future__ import annotations

import execution.market_maker as mm


def _leg(order_id: str, price: float = 0.50, count: float = 9.0) -> dict:
    return {"order_id": order_id, "price": price, "count": count}


def _reset(state: dict) -> None:
    mm._resting_quotes.clear()
    mm._resting_quotes.update(state)


def test_sweep_is_noop_when_every_ticker_is_still_a_candidate():
    _reset({"T1": {"yes": _leg("a"), "no": _leg("b")}})
    assert mm._sweep_orphaned_quotes({"T1"}, is_paper=False) == 0
    assert "T1" in mm._resting_quotes, "a live candidate must not be swept"


def test_paper_mode_clears_state_without_touching_kalshi(monkeypatch):
    called = []
    monkeypatch.setattr(mm, "_resting_quotes", {}, raising=False)
    _reset({"T1": {"yes": _leg("a"), "no": None},
            "T2": {"yes": None, "no": _leg("b")}})
    mm._sweep_orphaned_quotes(set(), is_paper=True)
    assert mm._resting_quotes == {}, "paper mode should drop all tracked quotes"
    assert not called


def test_orphan_is_cancelled_and_dropped(monkeypatch):
    cancelled: list[str] = []
    import execution.kalshi_executor as ke
    monkeypatch.setattr(ke, "get_order_status", lambda oid: {"fill_count_fp": 0})
    monkeypatch.setattr(ke, "cancel_quote", lambda oid: cancelled.append(oid) or True)

    _reset({
        "LIVE": {"yes": _leg("keep"), "no": None},
        "GONE": {"yes": _leg("o1"), "no": _leg("o2")},
    })
    mm._sweep_orphaned_quotes({"LIVE"}, is_paper=False)

    assert sorted(cancelled) == ["o1", "o2"], "both legs of the orphan must be cancelled"
    assert "GONE" not in mm._resting_quotes
    assert "LIVE" in mm._resting_quotes, "the still-quoted ticker must survive"


def test_partially_filled_orphan_logs_critical_and_still_cancels(monkeypatch, caplog):
    """A fill on an orphan cannot be turned into a position row (no candidate), so it
    must be surfaced loudly rather than silently dropped -- and the remainder cancelled."""
    cancelled: list[str] = []
    import execution.kalshi_executor as ke
    monkeypatch.setattr(ke, "get_order_status", lambda oid: {"fill_count_fp": 4.0})
    monkeypatch.setattr(ke, "cancel_quote", lambda oid: cancelled.append(oid) or True)
    monkeypatch.setattr(mm.db, "position_exists_for_order_id", lambda oid: False)

    _reset({"GONE": {"yes": _leg("part", price=0.42, count=9.0), "no": None}})
    with caplog.at_level("CRITICAL"):
        mm._sweep_orphaned_quotes(set(), is_paper=False)

    assert "ORPHAN FILL" in caplog.text
    assert "part" in caplog.text, "the order_id must be in the alert to reconcile by hand"
    assert cancelled == ["part"], "unfilled remainder must still be cancelled"
    assert mm._resting_quotes == {}


def test_already_recorded_fill_does_not_re_alert(monkeypatch, caplog):
    """A fill already backfilled into positions must not raise a second alarm."""
    import execution.kalshi_executor as ke
    monkeypatch.setattr(ke, "get_order_status", lambda oid: {"fill_count_fp": 9.0})
    monkeypatch.setattr(ke, "cancel_quote", lambda oid: True)
    monkeypatch.setattr(mm.db, "position_exists_for_order_id", lambda oid: True)

    _reset({"GONE": {"yes": _leg("known", count=9.0), "no": None}})
    with caplog.at_level("CRITICAL"):
        mm._sweep_orphaned_quotes(set(), is_paper=False)

    assert "ORPHAN FILL" not in caplog.text
