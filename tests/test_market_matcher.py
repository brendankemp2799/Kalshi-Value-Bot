"""Unit tests for fuzzy matching and event pairing logic."""
from datetime import datetime, timezone

from core.market_matcher import _kalshi_game_date, _team_score, match_events
from data.kalshi_client import KalshiMarket
from data.odds_fetcher import OddsEvent


def _mlb_event(home: str, away: str, day: int = 22) -> OddsEvent:
    return OddsEvent(
        event_id=f"mlb-{home[:3]}-{away[:3]}",
        sport_key="baseball_mlb",
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 7, day, 18, 0, tzinfo=timezone.utc),
        bookmakers=[],
    )


def _mls_event(home: str, away: str, day: int = 22) -> OddsEvent:
    return OddsEvent(
        event_id=f"mls-{home[:3]}-{away[:3]}",
        sport_key="soccer_usa_mls",
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 7, day, 18, 0, tzinfo=timezone.utc),
        bookmakers=[],
    )


def _h2h_km(ticker: str, event_ticker: str, yes_team: str, no_team: str,
             series: str = "KXMLBGAME") -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        title=f"{no_team} at {yes_team} Winner?",
        yes_team=yes_team,
        no_team=no_team,
        yes_price=0.60, no_price=0.40,
        yes_bid=0.58, yes_ask=0.62,
        volume=5000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker=event_ticker,
        bet_type="h2h",
    )


# ── _team_score ───────────────────────────────────────────────────────────────

def test_team_score_same_team_high():
    assert _team_score("Tampa Bay Rays", "Tampa Bay") >= 80


def test_team_score_abbreviation_high():
    assert _team_score("Tampa Bay Rays", "TB Rays") >= 80


def test_team_score_different_teams_low():
    assert _team_score("Tampa Bay Rays", "Boston Red Sox") < 70


def test_team_score_partial_city_high():
    assert _team_score("New York Mets", "New York") >= 80


# ── _kalshi_game_date ─────────────────────────────────────────────────────────

def test_kalshi_game_date_mlb():
    dt = _kalshi_game_date("KXMLBGAME-26APR08PITNYY")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 4
    assert dt.day == 8


def test_kalshi_game_date_mls():
    dt = _kalshi_game_date("KXMLSGAME-26JUL22MIACHI")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 22


def test_kalshi_game_date_returns_none_on_bad_ticker():
    assert _kalshi_game_date("INVALID") is None
    assert _kalshi_game_date("") is None


# ── match_events: H2H ─────────────────────────────────────────────────────────

def test_h2h_match_home_team():
    event = _mlb_event("Tampa Bay Rays", "Boston Red Sox")
    km = _h2h_km("KXMLBGAME-26JUL22TBABOS-TBA",
                 "KXMLBGAME-26JUL22TBABOS",
                 yes_team="Tampa Bay", no_team="Boston")
    matched = match_events([event], [km])
    assert len(matched) == 1
    assert matched[0].kalshi_outcome == "yes"  # Tampa Bay = home


def test_h2h_match_away_team():
    event = _mlb_event("Tampa Bay Rays", "Boston Red Sox")
    km = _h2h_km("KXMLBGAME-26JUL22TBABOS-BOS",
                 "KXMLBGAME-26JUL22TBABOS",
                 yes_team="Boston", no_team="Tampa Bay")
    matched = match_events([event], [km])
    assert len(matched) == 1
    assert matched[0].kalshi_outcome == "no"  # Boston = away


def test_h2h_no_match_wrong_sport():
    event = _mlb_event("Tampa Bay Rays", "Boston Red Sox")
    # Kalshi market for NBA — should not match MLB event
    km = KalshiMarket(
        ticker="KXNBAGAME-26JUL22TBABOS-TBA",
        title="Boston at Tampa Bay Winner?",
        yes_team="Tampa Bay", no_team="Boston",
        yes_price=0.60, no_price=0.40,
        yes_bid=0.58, yes_ask=0.62,
        volume=5000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker="KXNBAGAME-26JUL22TBABOS",
        bet_type="h2h",
    )
    matched = match_events([event], [km])
    assert len(matched) == 0


def test_h2h_no_match_wrong_date():
    # Event is on July 22, but Kalshi ticker encodes July 25
    event = _mlb_event("Tampa Bay Rays", "Boston Red Sox", day=22)
    km = _h2h_km("KXMLBGAME-26JUL25TBABOS-TBA",
                 "KXMLBGAME-26JUL25TBABOS",
                 yes_team="Tampa Bay", no_team="Boston")
    matched = match_events([event], [km])
    assert len(matched) == 0


def test_h2h_no_cross_game_mismatch():
    # Two games: TB vs BOS and TB vs NYY. The BOS market should not match NYY event.
    event_bos = _mlb_event("Tampa Bay Rays", "Boston Red Sox")
    event_nyy = _mlb_event("Tampa Bay Rays", "New York Yankees")
    km_bos = _h2h_km("KXMLBGAME-26JUL22TBABOS-TBA",
                     "KXMLBGAME-26JUL22TBABOS",
                     yes_team="Tampa Bay", no_team="Boston")
    matched = match_events([event_nyy], [km_bos])
    # no_team="Boston" must match away_team="New York Yankees" — it won't → no match
    assert len(matched) == 0


# ── match_events: MLS totals suffix lookup ────────────────────────────────────

def test_mls_totals_suffix_lookup():
    """Soccer totals with no teams in title should match via shared event_ticker suffix."""
    event = _mls_event("Inter Miami CF", "Chicago Fire")

    h2h_km = KalshiMarket(
        ticker="KXMLSGAME-26JUL22MIACHI-MIA",
        title="Chicago at Miami Winner?",
        yes_team="Miami", no_team="Chicago",
        yes_price=0.60, no_price=0.40,
        yes_bid=0.58, yes_ask=0.62,
        volume=5000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker="KXMLSGAME-26JUL22MIACHI",
        bet_type="h2h",
    )
    # "Will over 3.5 goals be scored?" — no team names in title
    totals_km = KalshiMarket(
        ticker="KXMLSTOTAL-26JUL22MIACHI-3",
        title="Will over 3.5 goals be scored?",
        yes_team="Over", no_team="Under",
        yes_price=0.35, no_price=0.65,
        yes_bid=0.34, yes_ask=0.36,
        volume=2000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker="KXMLSTOTAL-26JUL22MIACHI",  # same suffix as H2H
        bet_type="totals",
        threshold=3.5,
    )

    matched = match_events([event], [h2h_km, totals_km])
    tickers = {me.kalshi_market.ticker for me in matched}

    assert totals_km.ticker in tickers, (
        "MLS totals with no team names should be matched via event_ticker suffix"
    )
    totals_me = next(me for me in matched if me.kalshi_market.ticker == totals_km.ticker)
    assert totals_me.odds_event.event_id == event.event_id
    assert totals_me.kalshi_outcome == "yes"


def test_mls_totals_no_match_without_h2h():
    """Without the H2H match establishing the suffix lookup, totals stay unmatched."""
    event = _mls_event("Inter Miami CF", "Chicago Fire")
    totals_km = KalshiMarket(
        ticker="KXMLSTOTAL-26JUL22MIACHI-3",
        title="Will over 3.5 goals be scored?",
        yes_team="Over", no_team="Under",
        yes_price=0.35, no_price=0.65,
        yes_bid=0.34, yes_ask=0.36,
        volume=2000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker="KXMLSTOTAL-26JUL22MIACHI",
        bet_type="totals",
        threshold=3.5,
    )
    # Pass only the totals market — no H2H to build suffix lookup from
    matched = match_events([event], [totals_km])
    assert all(me.kalshi_market.ticker != totals_km.ticker for me in matched), (
        "Without H2H providing suffix lookup, soccer totals should not match"
    )
