"""Tests for db.get_mm_pairing() — paired vs UNPAIRED market-making fills.

WHY THIS MATTERS
----------------
Market making is only near-riskless when BOTH legs fill. A matched pair (1 YES +
1 NO) costs under $1 and pays exactly $1, so who wins is irrelevant. A leg that
fills alone is not market making at all — it is a naked directional position
wearing a market-making label, exposed to the full $1 swing on an outcome the bot
never formed an opinion about.

This is the shape of a real incident, not a hypothetical. An orphaned quote on
KXMLSGAME-26AUG19RSLDAL-RSL took 9 NO fills against 1 YES fill: ONE matched pair
worth +$0.04, and EIGHT naked contracts worth $3.84 of directional risk. It sat
untracked for two days because nothing measured pairing.
"""
from __future__ import annotations

import sqlite3

import pytest

from storage import db


def _pos(ticker: str, side: str, stake: float, price: float,
         strategy: str = "market_making") -> dict:
    return {"market_ticker": ticker, "side": side, "stake": stake,
            "market_price": price, "strategy": strategy}


@pytest.fixture
def positions(monkeypatch):
    """Stub get_open_positions with sqlite3.Row-like dicts."""
    store: list[dict] = []
    monkeypatch.setattr(db, "get_open_positions", lambda is_paper=False: store)
    return store


def test_perfectly_paired_market_reports_no_naked_exposure(positions):
    positions += [_pos("T1", "yes", 4.60, 0.46), _pos("T1", "no", 4.60, 0.46)]
    r = db.get_mm_pairing()[0]
    assert r["paired"] == pytest.approx(10)
    assert r["unpaired"] == pytest.approx(0)
    assert r["unpaired_dollars"] == 0.0
    assert r["naked_side"] == ""


def test_the_rsldal_case(positions):
    """The real incident: 9 NO against 1 YES. One pair, eight naked."""
    positions += [_pos("RSLDAL", "no", 4.32, 0.48),    # 9 contracts
                  _pos("RSLDAL", "yes", 0.48, 0.48)]   # 1 contract
    r = db.get_mm_pairing()[0]
    assert r["no_contracts"] == pytest.approx(9)
    assert r["yes_contracts"] == pytest.approx(1)
    assert r["paired"] == pytest.approx(1)
    assert r["unpaired"] == pytest.approx(8)
    assert r["naked_side"] == "no"
    assert r["unpaired_dollars"] == pytest.approx(3.84)


def test_one_sided_fill_is_all_naked(positions):
    """The common failure: one leg fills, the other never does."""
    positions += [_pos("T1", "yes", 5.00, 0.50)]
    r = db.get_mm_pairing()[0]
    assert r["paired"] == 0
    assert r["unpaired"] == pytest.approx(10)
    assert r["naked_side"] == "yes"
    assert r["unpaired_dollars"] == pytest.approx(5.00)


def test_directional_positions_are_excluded(positions):
    """Only strategy='market_making' counts. A value_edge bet is SUPPOSED to be
    one-sided — counting it as naked MM exposure would be meaningless noise."""
    positions += [_pos("T1", "yes", 5.00, 0.50, strategy="value_edge"),
                  _pos("T2", "no", 3.00, 0.50, strategy="value_edge")]
    assert db.get_mm_pairing() == []


def test_multiple_tickers_ranked_by_naked_dollars(positions):
    positions += [
        _pos("SMALL", "yes", 1.00, 0.50),                      # $1 naked
        _pos("BIG", "no", 9.00, 0.50),                         # $9 naked
        _pos("FLAT", "yes", 2.00, 0.50), _pos("FLAT", "no", 2.00, 0.50),
    ]
    out = db.get_mm_pairing()
    assert [r["ticker"] for r in out][:2] == ["BIG", "SMALL"], "worst first"
    flat = next(r for r in out if r["ticker"] == "FLAT")
    assert flat["unpaired"] == 0


def test_zero_price_does_not_divide_by_zero(positions):
    """Failed/placeholder rows can carry market_price 0."""
    positions += [_pos("T1", "yes", 0.0, 0.0)]
    r = db.get_mm_pairing()[0]
    assert r["yes_contracts"] == 0
    assert r["unpaired_dollars"] == 0.0
