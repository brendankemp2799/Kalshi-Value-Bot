"""The two risk-management switches must work INDEPENDENTLY.

This exact coupling has broken twice, in opposite directions, and each time the
switch read the value the operator set while the code did the opposite:

  1. manage_open_positions was itself config.ENABLE_TRAILING_STOP, so setting
     ENABLE_TRAILING_STOP=false on 2026-08-09 silently disabled the stop-loss too
     -- the live bot ran with NO risk management at all. Fixed in 5bc403e.
  2. That fix decoupled them, but removed the only place ENABLE_TRAILING_STOP was
     ever read. The trailing stop then ran for 7 days while its switch said false,
     costing -$4.99 across 12 positions (8 cut winners short for $8.44 forgone,
     against 4 losses avoided worth $3.45).

Both failures were invisible: the config said one thing, the behaviour was another,
and only a settlement-outcome counterfactual revealed it. These tests assert on
what actually gets CALLED, so a future refactor cannot quietly drop a guard again.
"""
from __future__ import annotations

import pytest

import config
import execution.auto_settle as auto_settle


@pytest.fixture
def calls(monkeypatch):
    """Record which risk checks auto_settle_positions actually invokes."""
    seen = {"trailing": 0, "stop_loss": 0}

    monkeypatch.setattr(auto_settle, "_check_trailing_stop",
                        lambda pos, market, is_paper: seen.__setitem__(
                            "trailing", seen["trailing"] + 1) or False)
    monkeypatch.setattr(auto_settle, "_check_stop_loss",
                        lambda pos, market, is_paper: seen.__setitem__(
                            "stop_loss", seen["stop_loss"] + 1) or False)

    # One open position to iterate, and a market that is not yet resolved so the
    # natural-settlement path below the risk checks does nothing.
    pos = {"id": 1, "market_ticker": "T1", "side": "yes", "market_price": 0.5,
           "stake": 1.0, "peak_price": None, "commence_time": None,
           "sport": "baseball_mlb", "bet_type": "h2h", "is_paper": 0}

    class _Row(dict):
        def keys(self):
            return super().keys()

    monkeypatch.setattr(auto_settle, "get_open_positions",
                        lambda is_paper=False: [_Row(pos)])
    monkeypatch.setattr(auto_settle, "_fetch_market",
                        lambda ticker: {"ticker": ticker, "result": ""})
    return seen


def _run():
    auto_settle.auto_settle_positions(is_paper=False, manage_open_positions=True)


def test_trailing_stop_does_not_run_when_switched_off(calls, monkeypatch):
    """THE seven-day bug. The switch said false; the code ran anyway."""
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", False)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", True)
    _run()
    assert calls["trailing"] == 0, "trailing stop ran while ENABLE_TRAILING_STOP=False"


def test_stop_loss_still_runs_when_trailing_stop_is_off(calls, monkeypatch):
    """THE original bug, in the other direction: turning the trailing stop off must
    not take the stop-loss down with it. This is the live production config."""
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", False)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", True)
    _run()
    assert calls["stop_loss"] == 1, "stop-loss was silently disabled with the trailing stop"


def test_trailing_stop_runs_when_switched_on(calls, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", True)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", False)
    _run()
    assert calls["trailing"] == 1
    assert calls["stop_loss"] == 0


def test_both_off_means_neither_runs(calls, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", False)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", False)
    _run()
    assert calls == {"trailing": 0, "stop_loss": 0}


def test_both_on_means_both_run(calls, monkeypatch):
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", True)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", True)
    _run()
    assert calls == {"trailing": 1, "stop_loss": 1}


def test_manage_open_positions_false_skips_both(calls, monkeypatch):
    """The settlement-only path must not touch risk management at all."""
    monkeypatch.setattr(config, "ENABLE_TRAILING_STOP", True)
    monkeypatch.setattr(config, "ENABLE_STOP_LOSS", True)
    auto_settle.auto_settle_positions(is_paper=False, manage_open_positions=False)
    assert calls == {"trailing": 0, "stop_loss": 0}
