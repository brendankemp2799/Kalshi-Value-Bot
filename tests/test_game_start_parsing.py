"""Kalshi tickers encode the first-pitch time; the matcher must use it.

THE BUG (found 2026-08-22). _kalshi_game_date read only the DATE segment, so a
19:15 ET first pitch was treated as midnight UTC. The "has it started?" guard then
used date + 12h, meaning every same-day market was dropped from 12:00 UTC onward --
which for MLB, where first pitch is usually 23:00Z or later, is the ENTIRE pre-game
window.

Measured cost in one scan: 1,509 player-prop markets skipped outright, and total
matches falling 235 -> 155 simply because the scan ran two hours later.

Two things are pinned here:
  1. the clock segment is read, and read as EASTERN
  2. pytz's localize() is used -- replace(tzinfo=...) on a pytz zone silently yields
     LMT (-4:56 for New York), which shifted every start by ~5 minutes and looked
     entirely plausible while doing it
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.market_matcher import _kalshi_game_start, _kalshi_game_date


@pytest.mark.parametrize("ticker,expected_utc", [
    # August = EDT (UTC-4)
    ("KXMLBKS-26AUG221915PITLAD",   "2026-08-22 23:15"),
    ("KXMLBGAME-26AUG221610CHCSEA", "2026-08-22 20:10"),
    ("KXMLBRFI-26AUG231910ATLMIL",  "2026-08-23 23:10"),
    # after midnight ET rolls the UTC date forward
    ("KXMLBGAME-26AUG222040MINSD",  "2026-08-23 00:40"),
])
def test_clock_time_is_read_and_treated_as_eastern(ticker, expected_utc):
    got = _kalshi_game_start(ticker)
    assert got is not None, f"{ticker} carries a time and must parse"
    assert got.strftime("%Y-%m-%d %H:%M") == expected_utc


def test_january_uses_standard_time_not_daylight():
    """A fixed -4 would be wrong for half the year."""
    got = _kalshi_game_start("KXNFLGAME-27JAN101300KCBUF")
    assert got.strftime("%Y-%m-%d %H:%M") == "2027-01-10 18:00"   # 13:00 EST = 18:00Z


@pytest.mark.parametrize("ticker", [
    "KXMLSGAME-26AUG19PHIMIA",      # date only, no clock
    "KXEPLBTTS-26AUG28CRYMCI",
    "bogus", "", "KXMLBGAME",
])
def test_tickers_without_a_time_return_none(ticker):
    """Callers fall back to date logic; returning a fabricated midnight would
    reintroduce the exact bug this fixes."""
    assert _kalshi_game_start(ticker) is None


def test_a_malformed_clock_is_refused_rather_than_guessed():
    assert _kalshi_game_start("KXMLBGAME-26AUG229999PITLAD") is None


def test_the_date_helper_still_works_for_date_only_logic():
    d = _kalshi_game_date("KXMLBKS-26AUG221915PITLAD")
    assert d is not None and d.strftime("%Y-%m-%d") == "2026-08-22"


def test_an_evening_game_is_not_considered_started_during_the_afternoon():
    """THE regression, stated directly: at 22:27 UTC a 23:15 UTC first pitch is still
    in the future. The old guard called it started at 12:00 UTC."""
    start = _kalshi_game_start("KXMLBKS-26AUG221915PITLAD")
    afternoon = datetime(2026, 8, 22, 22, 27, tzinfo=timezone.utc)
    assert start > afternoon
