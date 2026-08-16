"""Tests for the daily market-making health check.

This exists to answer one question without a human having to remember to look:
is market making worth keeping? It asks three questions in dependency order --
does it quote, does it fill, do fills PAIR -- because each is only meaningful if
the previous one passed. The third is the real test: MM only profits when both
legs fill, so a fill that never pairs is an accidental directional bet, not
spread capture.
"""
from __future__ import annotations

import json

import pytest

import research.metrics as metrics
import storage.db as db


@pytest.fixture
def mm(monkeypatch):
    """Drive mm_health from stubbed rollup rows and pairing data."""
    state = {"daily": [], "pairing": []}
    monkeypatch.setattr(db, "get_mm_daily_stats", lambda days=7: state["daily"])
    monkeypatch.setattr(db, "get_mm_pairing", lambda is_paper=False: state["pairing"])
    return state


def _day(ticks=1, quoted=0, fills=0, **reasons):
    return {"day": "2026-08-15", "ticks": ticks, "candidates": 10, "quoted": quoted,
            "legs_placed": 0, "legs_kept": 0, "fills": fills,
            "reasons_json": json.dumps(reasons)}


def _naked(ticker="T1", unpaired=8.0, dollars=3.84, paired=0.0):
    return {"ticker": ticker, "yes_contracts": 0.0, "no_contracts": unpaired,
            "paired": paired, "unpaired": unpaired, "naked_side": "no",
            "unpaired_dollars": dollars}


def test_no_data_is_distinguished_from_not_quoting(mm):
    """'MM never ran' and 'MM ran and declined' need different actions."""
    assert mm_verdict(mm).startswith("NO DATA")


def mm_verdict(state):
    return metrics.mm_health()["verdict"]


def test_not_quoting_names_the_dominant_reason(mm):
    mm["daily"] = [_day(ticks=100, insufficient_volume=90, spread_too_narrow=10)]
    h = metrics.mm_health()
    assert h["verdict"].startswith("NOT QUOTING")
    assert "insufficient_volume" in h["verdict"]
    assert "ENABLE_MARKET_MAKING=false" in h["action"]


def test_quoting_but_not_filling_is_its_own_verdict(mm):
    """Different cause, different fix: this one is a pricing problem, not an
    empty-market problem."""
    mm["daily"] = [_day(ticks=100, quoted=40, fills=0, ok=40)]
    h = metrics.mm_health()
    assert h["verdict"].startswith("QUOTING BUT NOT FILLING")
    assert "ENABLE_MARKET_MAKING=false" not in h["action"]


def test_filling_but_never_pairing_is_flagged_as_accidental_bets(mm):
    """THE case that matters. Fills happening but zero matched pairs means the bot
    is accumulating directional positions it never chose to take -- exactly the
    RSLDAL/PORSD state on 2026-08-15."""
    mm["daily"] = [_day(ticks=100, quoted=40, fills=9, ok=40)]
    mm["pairing"] = [_naked("RSLDAL", 8, 3.84), _naked("PORSD", 5, 2.55)]
    h = metrics.mm_health()
    assert h["verdict"].startswith("FILLING BUT NEVER PAIRING")
    assert h["naked_exposure_dollars"] == pytest.approx(6.39)
    assert "directional" in h["action"]


def test_working_state_is_recognised(mm):
    mm["daily"] = [_day(ticks=100, quoted=40, fills=20, ok=40)]
    mm["pairing"] = [{"ticker": "T1", "yes_contracts": 10.0, "no_contracts": 10.0,
                      "paired": 10.0, "unpaired": 0.0, "naked_side": "",
                      "unpaired_dollars": 0.0}]
    h = metrics.mm_health()
    assert h["verdict"].startswith("WORKING")
    assert h["matched_pairs_open"] == pytest.approx(10)


def test_counts_aggregate_across_days(mm):
    mm["daily"] = [_day(ticks=10, quoted=1, fills=1, ok=1),
                   _day(ticks=20, quoted=2, fills=3, ok=2)]
    h = metrics.mm_health()
    assert h["ticks"] == 30
    assert h["quoted"] == 3
    assert h["fills_recorded"] == 4


def test_status_file_is_readable_and_leads_with_the_action(mm, tmp_path, monkeypatch):
    monkeypatch.setattr(metrics, "MM_STATUS_PATH", tmp_path / "MM_STATUS.md")
    monkeypatch.setattr(metrics, "FINDINGS_DIR", tmp_path)
    mm["daily"] = [_day(ticks=100, insufficient_volume=90)]
    mm["pairing"] = [_naked()]
    metrics._write_mm_status(metrics.mm_health())
    text = (tmp_path / "MM_STATUS.md").read_text()
    assert "NOT QUOTING" in text
    assert "What to do:" in text
    assert "insufficient_volume" in text
    assert "Naked (unpaired) exposure" in text


def test_malformed_reasons_json_does_not_crash(mm):
    mm["daily"] = [{"day": "d", "ticks": 1, "candidates": 1, "quoted": 0,
                    "legs_placed": 0, "legs_kept": 0, "fills": 0,
                    "reasons_json": "not json"}]
    assert metrics.mm_health()["ticks"] == 1
