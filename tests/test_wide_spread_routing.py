"""Tests for how a wide-spread market is routed (2026-08-15).

THE BUG THESE PIN DOWN
----------------------
_quality_check() ran BEFORE _eval_edge() and `continue`d on a too-wide spread, so
a wide market's directional edge was never computed at all. Spread width silently
became the routing decision: every wide market went to the market maker, whether or
not it was a good bet. After MM's centering gate landed (2026-08-14), a wide market
whose consensus sat outside the book was then rejected by MM too -- so it was
evaluated by nobody and traded by nobody.

THE RULE NOW
------------
Edge is computed first, then routed:
  - passive (maker_only) edge in a wide market -> a real directional bet. It rests
    at the mid and expires costing nothing if unfilled, so trying is free.
  - edge that needs CROSSING a wide spread    -> refused, routed to MM. A wide
    spread signals thin/stale pricing, and _eval_edge only proves +EV *if the
    consensus is right* -- the assumption an untraded book most undermines.
  - no edge at all                            -> routed to MM, as before.
"""
from datetime import datetime, timezone

import pytest

import config
from core.market_matcher import MatchedEvent
from core.value_detector import detect_value, _resolve_wide_spread, _spread_too_wide, Outcome
from data.kalshi_client import KalshiMarket
from data.odds_fetcher import OddsEvent


@pytest.fixture(autouse=True)
def _pin_config(monkeypatch):
    monkeypatch.setattr(config, "ENABLED_BET_TYPES", {"h2h", "totals", "spread", "btts"})
    monkeypatch.setattr(config, "ALLOW_WIDE_SPREAD_MAKER", True)


# ~58% consensus on Milwaukee.
BOOKS = [
    {"key": "draftkings", "title": "DraftKings", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -145}, {"name": "Miami Marlins", "price": +121}]}]},
    {"key": "fanduel", "title": "FanDuel", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -148}, {"name": "Miami Marlins", "price": +124}]}]},
    {"key": "betmgm", "title": "BetMGM", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Milwaukee Brewers", "price": -146}, {"name": "Miami Marlins", "price": +122}]}]},
]


def _km(yes_bid: float, yes_ask: float) -> KalshiMarket:
    return KalshiMarket(
        ticker="KXMLBGAME-26JUL22TEST", title="Miami Marlins at Milwaukee Brewers Winner?",
        yes_team="Milwaukee Brewers", no_team="Miami Marlins",
        yes_price=(yes_bid + yes_ask) / 2, no_price=1 - (yes_bid + yes_ask) / 2,
        yes_bid=yes_bid, yes_ask=yes_ask, volume=10000,
        close_time="2026-07-23T00:00:00Z", category="sports",
        event_ticker="KXMLBGAME-26JUL22TEST", bet_type="h2h",
    )


def _run(yes_bid, yes_ask):
    event = OddsEvent(event_id="e", sport_key="baseball_mlb",
                      home_team="Milwaukee Brewers", away_team="Miami Marlins",
                      commence_time=datetime(2026, 7, 22, 20, tzinfo=timezone.utc),
                      bookmakers=BOOKS)
    km = _km(yes_bid, yes_ask)
    matched = [MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")]
    scan_log, mm = [], []
    opps = detect_value(matched, min_edge=config.MIN_EDGE, scan_log=scan_log,
                        mm_candidates=mm)
    return opps, mm, scan_log


# ── the decision table, tested directly ───────────────────────────────────────

def test_narrow_spread_is_unaffected():
    assert _resolve_wide_spread(maker_only=False, wide_reason=None)[0] is True
    assert _resolve_wide_spread(maker_only=True, wide_reason=None)[0] is True


def test_wide_spread_allows_passive_but_never_crossing():
    allow_passive, _, _ = _resolve_wide_spread(True, "spread 11.0¢ > max 5¢")
    allow_cross, status, _ = _resolve_wide_spread(False, "spread 11.0¢ > max 5¢")
    assert allow_passive is True, "resting at mid costs nothing if unfilled"
    assert allow_cross is False, "crossing a wide spread must stay blocked"
    assert status == "spread_too_wide_take"


def test_flag_reverts_to_old_behaviour(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_WIDE_SPREAD_MAKER", False)
    allow, status, _ = _resolve_wide_spread(True, "spread 11.0¢ > max 5¢")
    assert allow is False and status == "spread_too_wide"


def test_spread_check_is_no_longer_part_of_quality_check():
    """_quality_check must NOT reject on spread any more -- that is precisely what
    caused the edge to go uncomputed."""
    from core.value_detector import _quality_check
    km = _km(0.45, 0.56)                       # 11c spread
    assert _quality_check(km, book_count=5, std_dev=0.01, bet_type="h2h") is None
    assert _spread_too_wide(km, "h2h") is not None


# ── end-to-end through detect_value ───────────────────────────────────────────

def test_wide_market_with_passive_edge_becomes_a_bet_not_an_mm_candidate():
    """Consensus ~58% against a 0.45/0.56 book. The ask (56) is too expensive, but
    the mid (50.5) carries real edge. Before the fix this was never evaluated."""
    opps, mm, _ = _run(0.45, 0.56)
    assert len(opps) == 1, "the passive edge must be found"
    assert opps[0].maker_only is True, "it must be passive-only, never a cross"
    assert mm == [], "a market we are betting must not also be quoted against"


def test_wide_market_with_no_edge_still_goes_to_mm():
    """Nothing to bet on EITHER side, so MM should get it.

    Both sides matter: the away side is priced off (1 - yes_bid), so its passive
    price is 1 - mid while the home side's is mid. Straddling consensus is not
    enough — the book has to be centred tightly enough that neither 0.5722 - mid
    nor 0.4278 - (1 - mid) clears MIN_EDGE. That is mid in (0.5622, 0.5822); 0.57
    with an 11c spread sits inside it.
    """
    opps, mm, _ = _run(0.515, 0.625)
    assert opps == [], "neither side should have passive edge here"
    assert len(mm) == 1
    assert mm[0]["kalshi_spread"] == pytest.approx(0.11)


def test_narrow_market_never_produces_an_mm_candidate():
    opps, mm, _ = _run(0.50, 0.53)
    assert mm == [], "a crossable spread is the directional strategy's job"


def test_wide_market_is_never_both_a_bet_and_an_mm_candidate():
    """Quoting a two-sided MM quote on a ticker we hold a directional bet in would
    put the bot on both sides of its own position.

    This is not hypothetical — it is the regression that this test caught while the
    fix was being written. Both _detect_h2h loop iterations price the SAME ticker
    (the away side off 1 - yes_bid), so the away side found a passive bet while the
    home side, finding none, emitted an MM candidate for the very same market. The
    MM decision therefore has to be deferred until every side has been evaluated.
    """
    for bid, ask in [(0.45, 0.56), (0.58, 0.69), (0.515, 0.625),
                     (0.40, 0.52), (0.30, 0.44), (0.62, 0.75), (0.20, 0.35)]:
        opps, mm, _ = _run(bid, ask)
        assert not (opps and mm), f"both routes fired for {bid}/{ask}"


def test_scan_log_records_why_a_wide_market_was_refused():
    """Visibility: the operator must be able to see a take was blocked, and that
    it was blocked for spread width rather than for lack of edge."""
    opps, mm, scan_log = _run(0.45, 0.56)
    entry = next(e for e in scan_log if e["team_name"] == "Milwaukee Brewers")
    assert entry["status"] == "value"
    assert "passive only" in entry["reason"]
    assert entry["edge"] is not None, "the edge must now be recorded, not discarded"


# ── totals: the same rule, checked directly against _detect_totals (2026-08-24) ──
#
# THE BUG THIS SECTION PINS. Unlike _detect_h2h above, _detect_totals appended the
# opportunity UNCONDITIONALLY after computing `allow` -- the `if not allow:` branch
# logged the refusal and set up an MM candidate, then execution fell through and
# appended the bet anyway regardless of what `allow` said. A market _resolve_wide_
# spread had just said NOT to cross (wide spread, maker_only=False) was placed as a
# real crossing order. Found while extending this exact wide-spread logic to
# player props/BTTS/RFI and using _detect_totals as the template. Checked against
# real production data before fixing: zero totals positions have ever combined a
# wide Kalshi spread with a crossing (maker_only=0) fill, so this had not yet cost
# money -- but nothing before this section would have caught it if it had.

TOTALS_BOOKS = [
    {"key": "draftkings", "title": "DraftKings", "markets": [{"key": "totals", "outcomes": [
        {"name": "Over", "point": 8.5, "price": -145}, {"name": "Under", "point": 8.5, "price": +121}]}]},
    {"key": "fanduel", "title": "FanDuel", "markets": [{"key": "totals", "outcomes": [
        {"name": "Over", "point": 8.5, "price": -148}, {"name": "Under", "point": 8.5, "price": +124}]}]},
    {"key": "betmgm", "title": "BetMGM", "markets": [{"key": "totals", "outcomes": [
        {"name": "Over", "point": 8.5, "price": -146}, {"name": "Under", "point": 8.5, "price": +122}]}]},
]  # same shape/prices as BOOKS above -- ~58% consensus, this time on Over 8.5.


def _totals_km(yes_bid: float, yes_ask: float) -> KalshiMarket:
    return KalshiMarket(
        ticker="KXMLBTOTAL-26JUL22TEST", title="Miami at Milwaukee Over 8.5?",
        yes_team="Over 8.5 runs scored", no_team="Under",
        yes_price=(yes_bid + yes_ask) / 2, no_price=1 - (yes_bid + yes_ask) / 2,
        yes_bid=yes_bid, yes_ask=yes_ask, volume=10000,
        close_time="2026-07-23T00:00:00Z", category="sports",
        event_ticker="KXMLBTOTAL-26JUL22TEST", bet_type="totals", threshold=8.5,
    )


def _run_totals(yes_bid, yes_ask):
    event = OddsEvent(event_id="e", sport_key="baseball_mlb",
                      home_team="Milwaukee Brewers", away_team="Miami Marlins",
                      commence_time=datetime(2026, 7, 22, 20, tzinfo=timezone.utc),
                      bookmakers=TOTALS_BOOKS)
    km = _totals_km(yes_bid, yes_ask)
    matched = [MatchedEvent(odds_event=event, kalshi_market=km, kalshi_outcome="yes")]
    scan_log, mm = [], []
    opps = detect_value(matched, min_edge=config.MIN_EDGE, scan_log=scan_log,
                        mm_candidates=mm)
    return opps, mm, scan_log


def test_wide_spread_totals_crossing_edge_is_not_a_bet():
    """THE REGRESSION. ~58% consensus vs a 0.20/0.50 market: ask_edge (0.08) clears
    even the fee-adjusted bar (maker_only=False, a crossing order), and the 30-cent
    spread is far past max_kalshi_spread. _resolve_wide_spread must refuse this,
    and the refusal must actually stick."""
    opps, mm, scan_log = _run_totals(0.20, 0.50)
    assert opps == [], "a crossing order was placed into a spread flagged too wide to cross"
    entry = next(e for e in scan_log if "Over" in e["team_name"])
    assert entry["status"] == "spread_too_wide_take"


def test_wide_spread_totals_is_never_both_a_bet_and_an_mm_candidate():
    for bid, ask in [(0.20, 0.50), (0.15, 0.45), (0.45, 0.56), (0.30, 0.44)]:
        opps, mm, _ = _run_totals(bid, ask)
        assert not (opps and mm), f"both routes fired for {bid}/{ask}"


def test_narrow_spread_totals_is_unaffected_by_the_fix():
    """A narrow, crossable spread must still become a real bet -- the fix must not
    have made _detect_totals refuse everything."""
    opps, mm, _ = _run_totals(0.50, 0.53)
    assert len(opps) == 1
    assert opps[0].outcome == Outcome.OVER
