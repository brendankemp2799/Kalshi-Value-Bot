"""Tests for the order-status read retry, and the fee accounting that depends on it.

THE BUG
-------
Kalshi does not make an order queryable at GET /portfolio/orders/{id} the instant
the POST that created it returns. Reading the fee back immediately after an
at-placement fill therefore raced that propagation and 404'd — 26 times in 24h on
2026-08-15, roughly 8% of taker fills.

The failure was silent and one-directional: _fee_breakdown() returned (0, 0), so
the position recorded entry_fee_paid=$0.00 for a fill that really did pay a taker
fee. Costs understated, P&L overstated.

Confirmed to be a race and not a bad identifier: all 12 most recent stored
order_ids were queryable when retried later, including the exact id that had just
404'd in the logs.
"""
from __future__ import annotations

import pytest

import execution.kalshi_executor as ke


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep the backoff schedule out of the test runtime."""
    monkeypatch.setattr(ke.time, "sleep", lambda s: None)


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code, self._payload = code, payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error: Not Found for url")

    def json(self):
        return self._payload


def _stub_session(monkeypatch, responses):
    """Serve `responses` in order; repeat the last one once exhausted."""
    calls = {"n": 0}

    class _S:
        def get(self, url, headers=None, timeout=None):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return responses[i]

    monkeypatch.setattr("data.kalshi_auth.session", lambda: _S())
    monkeypatch.setattr("data.kalshi_auth.auth_headers", lambda *a, **k: {})
    return calls


_FEE_ORDER = {"order": {"maker_fees_dollars": "0.0000",
                        "taker_fees_dollars": "0.0350"}}


def test_status_read_succeeds_first_try_without_retrying(monkeypatch):
    calls = _stub_session(monkeypatch, [_Resp(200, _FEE_ORDER)])
    assert ke._get_order_status("o1", retries=3) is not None
    assert calls["n"] == 1, "a working call must not retry"


def test_404_then_success_is_recovered(monkeypatch):
    """THE fix: the order simply was not visible yet."""
    calls = _stub_session(monkeypatch, [_Resp(404), _Resp(404), _Resp(200, _FEE_ORDER)])
    out = ke._get_order_status("o1", retries=3)
    assert out is not None
    assert calls["n"] == 3


def test_fee_is_recovered_rather_than_silently_zeroed(monkeypatch):
    """The whole point. Before, a first-attempt 404 recorded a $0.00 fee on a fill
    that really paid 3.5 cents."""
    _stub_session(monkeypatch, [_Resp(404), _Resp(200, _FEE_ORDER)])
    maker, taker = ke._fee_breakdown("o1")
    assert taker == pytest.approx(0.035)
    assert ke._actual_fee_dollars("o1") > 0


def test_default_callers_do_not_retry(monkeypatch):
    """Polling loops call this every few seconds anyway; retrying would only slow
    them down. Default must stay 0."""
    calls = _stub_session(monkeypatch, [_Resp(404)])
    assert ke._get_order_status("o1") is None
    assert calls["n"] == 1


def test_persistent_failure_gives_up_and_is_loud(monkeypatch, caplog):
    """If the fee genuinely can't be read, that must not pass silently — the error
    only ever flatters P&L."""
    _stub_session(monkeypatch, [_Resp(404)])
    with caplog.at_level("ERROR"):
        maker, taker = ke._fee_breakdown("o1")
    assert (maker, taker) == (0.0, 0.0)
    assert "FEE UNKNOWN" in caplog.text
    assert "understated" in caplog.text


def test_retry_count_is_bounded_by_the_backoff_schedule(monkeypatch):
    calls = _stub_session(monkeypatch, [_Resp(404)])
    ke._fee_breakdown("o1")
    assert calls["n"] == len(ke._STATUS_RETRY_BACKOFF) + 1


def test_classification_still_correct_when_the_fee_is_unreadable(monkeypatch):
    """Belt and braces: even with no fee data, an at-placement fill must still be
    labelled a taker. This is what the circular `taker if fee>0 else maker` rule
    got wrong — it called an unreadable fee a free maker fill."""
    _stub_session(monkeypatch, [_Resp(404)])
    kind, fee = ke._classify_fill("o1", crossed_at_placement=True)
    assert kind == "taker"
    assert fee == 0.0
