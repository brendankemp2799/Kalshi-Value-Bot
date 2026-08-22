"""KalshiMarket.game_time must not put an evening game a day early.

THE BUG (2026-08-22). game_time rebuilt kickoff as (ticker DATE) + (close_time's UTC
hour:minute). The ticker date is EASTERN; the clock was UTC. For any game after 20:00
ET -- whose UTC date has already rolled forward -- that lands a full day early:

    KXMLBTOTAL-26AUG222040MINSD   Aug 22, 20:40 ET  ==  Aug 23, 00:40 UTC
    game_time produced            Aug 22, 00:40 UTC          <- 24 hours early

Measured across live markets: 30 of 135 timed markets (22%) were off by exactly 1440
minutes. game_time becomes positions.commence_time, which drives the stop-loss time
ramp (_elapsed_fraction), the market-maker kickoff stand-down, the 48h horizon and
every time-to-event figure -- so the error is silent and wide.

The fix reads the ET clock the ticker already encodes. The old close_time heuristic
survives only for tickers with no clock, where there is nothing better.
"""
from __future__ import annotations

import pytest

from data.kalshi_client import KalshiMarket, parse_ticker_start


def mk(event_ticker: str, close_time: str) -> KalshiMarket:
    return KalshiMarket(
        ticker=event_ticker + "-X", title="t", yes_team="", no_team="",
        yes_price=0.5, no_price=0.5, yes_bid=0.49, yes_ask=0.51, volume=1,
        close_time=close_time, category="", event_ticker=event_ticker)


# ── the 24-hour error ───────────────────────────────────────────────────────────

def test_an_evening_et_game_lands_on_the_next_utc_day():
    """THE bug. 20:40 ET on Aug 22 is 00:40 UTC on Aug 23, not Aug 22."""
    m = mk("KXMLBTOTAL-26AUG222040MINSD", "2026-09-05T00:40:00Z")
    assert m.game_time.startswith("2026-08-23T00:40")


def test_an_afternoon_game_stays_on_the_same_utc_day():
    m = mk("KXMLBGAME-26AUG221610CHCSEA", "2026-09-05T20:10:00Z")
    assert m.game_time.startswith("2026-08-22T20:10")


@pytest.mark.parametrize("ticker,expected", [
    ("KXMLBKS-26AUG221915PITLAD",   "2026-08-22T23:15"),   # 19:15 EDT
    ("KXMLBRFI-26AUG242140PITSD",   "2026-08-25T01:40"),   # 21:40 EDT -> next UTC day
    ("KXNFLGAME-27JAN101300KCBUF",  "2027-01-10T18:00"),   # 13:00 EST, not EDT
])
def test_start_times_convert_from_eastern_correctly(ticker, expected):
    assert mk(ticker, "2026-09-05T00:00:00Z").game_time.startswith(expected)


def test_daylight_saving_is_respected_not_a_fixed_offset():
    summer = parse_ticker_start("KXMLBGAME-26JUL041900AAABBB")   # EDT, -4
    winter = parse_ticker_start("KXNFLGAME-27JAN101900AAABBB")   # EST, -5
    assert summer.strftime("%H:%M") == "23:00"
    assert winter.strftime("%H:%M") == "00:00"


# ── the fallback, for tickers with no clock ─────────────────────────────────────

def test_a_ticker_without_a_clock_falls_back_to_close_time():
    """Soccer tickers carry no time. The old heuristic is still the best available."""
    m = mk("KXMLSGAME-26AUG19PHIMIA", "2026-09-02T23:30:00Z")
    assert m.game_time.startswith("2026-08-19T23:30")


def test_parse_returns_none_rather_than_a_fabricated_midnight():
    """Returning midnight would silently reintroduce the original class of bug."""
    for t in ("KXMLSGAME-26AUG19PHIMIA", "bogus", "", "KXMLBGAME",
              "KXMLBGAME-26AUG229999XXX"):
        assert parse_ticker_start(t) is None


def test_unparseable_everything_degrades_to_close_time():
    m = mk("garbage", "2026-09-02T23:30:00Z")
    assert m.game_time == "2026-09-02T23:30:00Z"


# ── one source of truth ─────────────────────────────────────────────────────────

def test_the_matcher_and_the_market_agree():
    """Two copies of ET/UTC handling would drift; the matcher delegates here."""
    from core.market_matcher import _kalshi_game_start
    for t in ("KXMLBTOTAL-26AUG222040MINSD", "KXMLBKS-26AUG221915PITLAD",
              "KXMLSGAME-26AUG19PHIMIA"):
        assert _kalshi_game_start(t) == parse_ticker_start(t)
