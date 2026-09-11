"""Tests for the payout-floor price cap on _eval_edge() (2026-09-10).

THE INCIDENT THIS PINS DOWN
----------------------------
Two player-prop bets placed the same night -- William Contreras "Under 1 HR" at
87c and Elly De La Cruz "Under 1 HR" at 85c -- both lost, costing $6.12 and $6.84
(combined -$12.95, more than Sep 9's entire net P&L). Both were sized large (near
config.MAX_PCT_BANKROLL) because Kelly saw a high modeled win probability
(89.8%/88.4%) and a real edge (2.8-3.4%) and sized accordingly -- but the payout on
a win was tiny (13c/15c per dollar risked), so nearly the whole stake was at risk
for a sliver of upside. See config.MIN_PAYOUT_RATIO's docstring for the full
rationale.

THE RULE NOW
------------
_eval_edge() refuses any price beyond config.MAX_ENTRY_PRICE outright, regardless
of edge, on both the ask path and the mid (maker) path.
"""
from datetime import datetime, timezone

import pytest

import config
from core.market_matcher import MatchedEvent
from core.value_detector import detect_value, _eval_edge
from data.kalshi_client import KalshiMarket
from data.odds_fetcher import OddsEvent


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    monkeypatch.setattr(config, "ENABLED_BET_TYPES", {"h2h", "totals", "spread", "btts"})


def test_extreme_price_refused_even_with_large_edge():
    # Mirrors the Contreras bet: 87c price, consensus well above it (real edge).
    assert _eval_edge(consensus=0.95, ask_price=0.87, spread=0.01, min_edge=0.015) is None


def test_extreme_price_refused_on_mid_maker_path_too():
    # Wide spread makes the mid price still land beyond the cap -- must still refuse.
    result = _eval_edge(consensus=0.95, ask_price=0.90, spread=0.10, min_edge=0.015)
    assert result is None


def test_price_at_the_cap_boundary_is_allowed():
    price = config.MAX_ENTRY_PRICE
    edge, maker_only = _eval_edge(consensus=price + 0.05, ask_price=price, spread=0.01, min_edge=0.015)
    assert maker_only is False


def test_normal_priced_bet_unaffected():
    # A typical ~50c bet with real edge must still clear exactly as before.
    edge, maker_only = _eval_edge(consensus=0.55, ask_price=0.50, spread=0.01, min_edge=0.015)
    assert edge == pytest.approx(0.05)
    assert maker_only is False


# ── end-to-end through detect_value: the actual scan_log the operator reads ────

# Heavy favorite (~95% implied), matching the Contreras/De La Cruz shape.
_FAVORITE_BOOKS = [
    {"key": "draftkings", "title": "DraftKings", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -1900}, {"name": "Miami Marlins", "price": 700}]}]},
    {"key": "fanduel", "title": "FanDuel", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -1850}, {"name": "Miami Marlins", "price": 680}]}]},
    {"key": "betmgm", "title": "BetMGM", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -1950}, {"name": "Miami Marlins", "price": 720}]}]},
]


def _run_favorite(yes_bid, yes_ask):
    event = OddsEvent(event_id="e", sport_key="baseball_mlb",
                      home_team="Milwaukee Brewers", away_team="Miami Marlins",
                      commence_time=datetime(2026, 7, 22, 20, tzinfo=timezone.utc),
                      bookmakers=_FAVORITE_BOOKS)
    km = KalshiMarket(
        ticker="KXMLBGAME-26JUL22TEST", title="Miami Marlins at Milwaukee Brewers Winner?",
        yes_team="Milwaukee Brewers", no_team="Miami Marlins",
        yes_price=(yes_bid + yes_ask) / 2, no_price=1 - (yes_bid + yes_ask) / 2,
        yes_bid=yes_bid, yes_ask=yes_ask, volume=10000,
        close_time="2026-07-23T00:00:00Z", category="sports",
        event_ticker="KXMLBGAME-26JUL22TEST", bet_type="h2h",
    )
    matched = [MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")]
    scan_log, mm = [], []
    opps = detect_value(matched, min_edge=config.MIN_EDGE, scan_log=scan_log,
                        mm_candidates=mm)
    return opps, mm, scan_log


def test_scan_log_labels_payout_floor_rejection_distinctly_from_no_edge():
    """The whole point of this fix: an operator reading scan_log after the fact
    must be able to tell "refused for a thin payout" apart from "refused for
    insufficient edge" -- these are different problems with different remedies,
    and before this fix both showed up as the same generic "no_edge" reason."""
    opps, mm, scan_log = _run_favorite(0.86, 0.87)  # 87c, ~95% consensus: real edge, thin payout
    assert opps == [], "must not become a real bet"
    assert mm == [], "must not rest a passive quote at this price either"
    entry = next(e for e in scan_log if e["team_name"] == "Milwaukee Brewers")
    assert entry["status"] == "payout_too_thin"
    assert "payout too thin" in entry["reason"]
    assert "no_edge" not in entry["status"]
