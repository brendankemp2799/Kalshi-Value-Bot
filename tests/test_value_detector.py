"""Unit tests for edge detection logic."""
from datetime import datetime, timezone

import pytest

from core.market_matcher import MatchedEvent
from core.value_detector import detect_value
from data.kalshi_client import KalshiMarket
from data.odds_fetcher import OddsEvent


def _mlb_event(home: str, away: str, bookmakers: list) -> OddsEvent:
    return OddsEvent(
        event_id="test-event",
        sport_key="baseball_mlb",
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc),
        bookmakers=bookmakers,
    )


def _soccer_event(home: str, away: str, bookmakers: list) -> OddsEvent:
    return OddsEvent(
        event_id="test-soccer-event",
        sport_key="soccer_usa_mls",
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc),
        bookmakers=bookmakers,
    )


def _h2h_km(yes_team: str, no_team: str, yes_bid: float, yes_ask: float,
             series: str = "KXMLBGAME") -> KalshiMarket:
    return KalshiMarket(
        ticker=f"{series}-26JUL22TEST",
        title=f"{no_team} at {yes_team} Winner?",
        yes_team=yes_team,
        no_team=no_team,
        yes_price=(yes_bid + yes_ask) / 2,
        no_price=1.0 - (yes_bid + yes_ask) / 2,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=10000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker=f"{series}-26JUL22TEST",
        bet_type="h2h",
    )


def _totals_km(threshold: float, yes_bid: float, yes_ask: float,
               series: str = "KXMLBTOTAL") -> KalshiMarket:
    return KalshiMarket(
        ticker=f"{series}-26JUL22TEST",
        title=f"Will over {threshold} runs be scored?",
        yes_team=f"Over {threshold}",
        no_team=f"Under {threshold}",
        yes_price=(yes_bid + yes_ask) / 2,
        no_price=1.0 - (yes_bid + yes_ask) / 2,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume=10000,
        close_time="2026-07-23T00:00:00Z",
        category="sports",
        event_ticker=f"{series}-26JUL22TEST",
        bet_type="totals",
        threshold=threshold,
    )


# Bookmakers fixtures
# Two DraftKings-weight books with consistent lines giving ~58% consensus for home.
BOOKMAKERS_H2H_HOME_FAVORED = [
    {
        "key": "draftkings",
        "title": "DraftKings",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Milwaukee Brewers", "price": -145},
                    {"name": "Miami Marlins",     "price": +121},
                ],
            }
        ],
    },
    {
        "key": "fanduel",
        "title": "FanDuel",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Milwaukee Brewers", "price": -148},
                    {"name": "Miami Marlins",     "price": +124},
                ],
            }
        ],
    },
]

# Soccer 3-way bookmakers: Miami heavily favored
BOOKMAKERS_SOCCER_3WAY = [
    {
        "key": "draftkings",
        "title": "DraftKings",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Inter Miami CF", "price": -130},
                    {"name": "Chicago Fire",   "price": +330},
                    {"name": "Draw",           "price": +260},
                ],
            }
        ],
    },
    {
        "key": "fanduel",
        "title": "FanDuel",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Inter Miami CF", "price": -135},
                    {"name": "Chicago Fire",   "price": +320},
                    {"name": "Draw",           "price": +255},
                ],
            }
        ],
    },
]

# Over/Under totals bookmakers
BOOKMAKERS_TOTALS = [
    {
        "key": "draftkings",
        "title": "DraftKings",
        "markets": [
            {
                "key": "totals",
                "outcomes": [
                    {"name": "Over",  "price": -115, "point": 8.5},
                    {"name": "Under", "price": -105, "point": 8.5},
                ],
            }
        ],
    },
    {
        "key": "fanduel",
        "title": "FanDuel",
        "markets": [
            {
                "key": "totals",
                "outcomes": [
                    {"name": "Over",  "price": -115, "point": 8.5},
                    {"name": "Under", "price": -105, "point": 8.5},
                ],
            }
        ],
    },
]


# ── H2H edge detection ────────────────────────────────────────────────────────

def test_h2h_value_found_when_edge_positive():
    """detect_value surfaces an opportunity when consensus > Kalshi ask."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_H2H_HOME_FAVORED)
    # Kalshi has Milwaukee at 52¢ ask; consensus ≈ 57% → edge ≈ +5%
    km = _h2h_km("Milwaukee Brewers", "Miami Marlins", yes_bid=0.50, yes_ask=0.52)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    opps = detect_value([me], min_edge=0.0, scan_log=scan_log)

    value_entries = [e for e in scan_log if e["status"] == "value"]
    assert len(value_entries) >= 1, "Expected at least one value entry"
    assert value_entries[0]["edge"] > 0
    assert value_entries[0]["team_name"] == "Milwaukee Brewers"


def test_h2h_no_value_when_edge_negative():
    """detect_value does not surface an opportunity when Kalshi ask > consensus."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_H2H_HOME_FAVORED)
    # Kalshi overprices Milwaukee: ask = 68¢, consensus ≈ 57% → edge ≈ -11%
    km = _h2h_km("Milwaukee Brewers", "Miami Marlins", yes_bid=0.66, yes_ask=0.68)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    value_entries = [e for e in scan_log if e["status"] == "value" and e["team_name"] == "Milwaukee Brewers"]
    assert len(value_entries) == 0


def test_h2h_scan_log_populated_for_all_candidates():
    """Every candidate (win or loss) appears in scan_log when provided."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_H2H_HOME_FAVORED)
    km = _h2h_km("Milwaukee Brewers", "Miami Marlins", yes_bid=0.50, yes_ask=0.52)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    assert len(scan_log) >= 1
    for entry in scan_log:
        assert "status" in entry
        assert "edge" in entry
        assert "consensus_prob" in entry


# ── Soccer: away-team skip ────────────────────────────────────────────────────

def test_soccer_away_team_not_evaluated_when_yes_is_home():
    """For a soccer YES=home market, the away team must NOT be evaluated.

    In 3-way soccer, NO on 'Miami wins' means 'Miami does NOT win' (includes
    draws) — not 'Chicago wins'. Evaluating Chicago's probability against the
    NO price would produce phantom edge.
    """
    event = _soccer_event("Inter Miami CF", "Chicago Fire", BOOKMAKERS_SOCCER_3WAY)
    # YES = Miami (home). kalshi_outcome = "yes"
    km = _h2h_km("Miami", "Chicago", yes_bid=0.60, yes_ask=0.62, series="KXMLSGAME")
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    team_names = [e["team_name"] for e in scan_log]
    assert "Chicago Fire" not in team_names, (
        f"Away team evaluated for soccer YES=home market: {team_names}"
    )


def test_soccer_home_team_is_evaluated_when_yes_is_home():
    """The YES (home) team IS evaluated for soccer h2h."""
    event = _soccer_event("Inter Miami CF", "Chicago Fire", BOOKMAKERS_SOCCER_3WAY)
    km = _h2h_km("Miami", "Chicago", yes_bid=0.60, yes_ask=0.62, series="KXMLSGAME")
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    team_names = [e["team_name"] for e in scan_log]
    assert "Inter Miami CF" in team_names


# ── Totals: NO side ───────────────────────────────────────────────────────────

def test_totals_both_over_and_under_appear_in_scan_log():
    """An Over totals market evaluates both Over (YES) and Under (NO) sides."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_TOTALS)
    km = _totals_km(threshold=8.5, yes_bid=0.49, yes_ask=0.51)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    names = [e["team_name"] for e in scan_log]
    assert any("Over" in n for n in names), f"Over not in scan_log: {names}"
    assert any("Under" in n for n in names), f"Under not in scan_log: {names}"


def test_totals_under_edge_is_complement_of_over():
    """P(Under) = 1 - P(Over), so their edges should be opposites."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_TOTALS)
    # Set a clearly off-center yes_ask so one side is + and the other -
    km = _totals_km(threshold=8.5, yes_bid=0.45, yes_ask=0.48)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    over_entry  = next((e for e in scan_log if "Over"  in (e.get("team_name") or "")), None)
    under_entry = next((e for e in scan_log if "Under" in (e.get("team_name") or "")), None)

    assert over_entry  is not None, "Over entry missing from scan_log"
    assert under_entry is not None, "Under entry missing from scan_log"

    # For complementary markets: over_edge + under_edge = -(yes_ask - yes_bid) = -spread.
    # Over edge  = P(Over)  - yes_ask
    # Under edge = P(Under) - (1 - yes_bid) = (1-P(Over)) - 1 + yes_bid = yes_bid - P(Over)
    # Sum        = yes_bid - yes_ask = -0.03 for yes_bid=0.45, yes_ask=0.48
    over_edge  = over_entry["edge"]
    under_edge = under_entry["edge"]
    assert over_edge is not None and under_edge is not None
    expected_sum = -(0.48 - 0.45)  # -(yes_ask - yes_bid) = -0.03
    assert abs(over_edge + under_edge - expected_sum) < 0.005, (
        f"over_edge {over_edge:.4f} + under_edge {under_edge:.4f} should ≈ {expected_sum:.3f}"
    )


def test_totals_value_requires_positive_edge():
    """Only the side with positive edge gets status='value'."""
    event = _mlb_event("Milwaukee Brewers", "Miami Marlins", BOOKMAKERS_TOTALS)
    # With Over consensus ≈ 51% and yes_ask = 0.48, Over has +3% edge → value
    # Under consensus ≈ 49%, no_price = 1 - yes_bid = 0.55, Under has -6% → no_edge
    km = _totals_km(threshold=8.5, yes_bid=0.45, yes_ask=0.48)
    me = MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    scan_log: list[dict] = []
    detect_value([me], min_edge=0.0, scan_log=scan_log)

    value_names = [e["team_name"] for e in scan_log if e["status"] == "value"]
    no_edge_names = [e["team_name"] for e in scan_log if e["status"] == "no_edge"]

    # Exactly one side (Over) should be value, the other (Under) no_edge
    assert any("Over"  in n for n in value_names),   f"Over should be value; got value={value_names}"
    assert any("Under" in n for n in no_edge_names), f"Under should be no_edge; got no_edge={no_edge_names}"
