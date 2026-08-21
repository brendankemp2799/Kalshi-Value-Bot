"""Odds API credit controls: billing scope, the 48h horizon, and alternate lines.

All three exist because of measurements taken 2026-08-20:

  BILLING.  cost = markets x units, where units is the region count OR ceil(books/10)
  if `bookmakers` is sent instead. Measured live: regions=us,eu x 3 markets = 6
  credits; 10 books x 3 markets = 3; 1 book x 3 markets = 3 (identical); 11 books = 2
  units. So naming <=10 books halves every request AND keeps Pinnacle, which lives in
  the `eu` region and would be lost by regions=us alone.

  48h HORIZON.  Orders placed >48h from kickoff filled 57/751 = 7.6% of the time, vs
  45.8% in the 3-12h window. That tail was most of our order traffic and almost none
  of it converted.

  ALTERNATE LINES.  The bulk endpoint returns one FEATURED totals line per book and
  books disagree on it (one MLB game: 18 books at 9.0, 4 at 9.5). Kalshi picks its own
  strikes, so 20 of 29 totals candidates we would have bet had NO book quoting
  Kalshi's number. alternate_totals carries the whole ladder but is per-event only --
  the bulk endpoint 422s -- so it bills per game and cadence is the cost dial.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import data.odds_fetcher as of
from data.odds_fetcher import OddsAPIClient, OddsEvent


@pytest.fixture(autouse=True)
def _clean_cache():
    of._ALT_FETCHED_AT.clear()
    yield
    of._ALT_FETCHED_AT.clear()


def ev(event_id="e1", hours_out=6.0, sport="baseball_mlb", books=None):
    return OddsEvent(
        event_id=event_id, sport_key=sport, home_team="H", away_team="A",
        commence_time=datetime.now(timezone.utc) + timedelta(hours=hours_out),
        bookmakers=books if books is not None else [],
    )


# ── billing scope ───────────────────────────────────────────────────────────────

def test_bookmakers_replace_regions_when_configured():
    """THE 50% saving. Sending both would be billed on regions, so it must be either/or."""
    p = OddsAPIClient._scope_params()
    assert "bookmakers" in p
    assert "regions" not in p, "regions alongside bookmakers would be billed at 2 units"


def test_the_book_list_stays_within_one_billing_unit():
    """11 books costs 2 units -- the same as regions=us,eu, i.e. the saving vanishes."""
    n = len([b for b in config.ODDS_API_BOOKMAKERS.split(",") if b.strip()])
    assert 0 < n <= 10, f"{n} books would bill as {-(-n // 10)} units, not 1"


def test_pinnacle_is_retained():
    """Pinnacle is EU-only: `regions=us` drops it. The 10th slot is free, so there is
    no reason to lose the sharpest book."""
    assert "pinnacle" in config.ODDS_API_BOOKMAKERS


def test_falling_back_to_regions_when_no_books_configured(monkeypatch):
    monkeypatch.setattr(config, "ODDS_API_BOOKMAKERS", "")
    p = OddsAPIClient._scope_params()
    assert p == {"regions": config.ODDS_API_REGIONS}


# ── the 48h horizon ─────────────────────────────────────────────────────────────

def test_window_params_span_the_configured_horizon():
    p = OddsAPIClient._window_params()
    lo = datetime.strptime(p["commenceTimeFrom"], "%Y-%m-%dT%H:%M:%SZ")
    hi = datetime.strptime(p["commenceTimeTo"], "%Y-%m-%dT%H:%M:%SZ")
    assert abs((hi - lo).total_seconds() / 3600 - config.MAX_TIME_TO_EVENT_HOURS) < 0.1


def test_horizon_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "MAX_TIME_TO_EVENT_HOURS", 0)
    assert OddsAPIClient._window_params() == {}


# ── alternate-line refresh cadence (this is the cost dial) ──────────────────────

def test_a_game_is_fetched_once_then_not_again_immediately():
    """Without this, a 45-min scan loop re-buys every ladder every cycle."""
    now = datetime.now(timezone.utc)
    c = now + timedelta(hours=6)
    assert OddsAPIClient._alternates_due("e1", c, now) is True
    of._ALT_FETCHED_AT["e1"] = now
    assert OddsAPIClient._alternates_due("e1", c, now + timedelta(minutes=45)) is False


def test_near_games_refresh_hourly():
    now = datetime.now(timezone.utc)
    c = now + timedelta(hours=6)          # inside the 12h tier -> hourly
    of._ALT_FETCHED_AT["e1"] = now
    assert OddsAPIClient._alternates_due("e1", c, now + timedelta(minutes=59)) is False
    assert OddsAPIClient._alternates_due("e1", c, now + timedelta(hours=1, minutes=1)) is True


def test_distant_games_refresh_far_less_often():
    """A game 36h out sits in the 6-hourly tier; hourly would triple the bill."""
    now = datetime.now(timezone.utc)
    c = now + timedelta(hours=36)
    of._ALT_FETCHED_AT["e1"] = now
    assert OddsAPIClient._alternates_due("e1", c, now + timedelta(hours=3)) is False
    assert OddsAPIClient._alternates_due("e1", c, now + timedelta(hours=6, minutes=1)) is True


def test_games_beyond_the_horizon_are_never_fetched():
    now = datetime.now(timezone.utc)
    assert OddsAPIClient._alternates_due("e1", now + timedelta(hours=72), now) is False


def test_games_already_started_are_never_fetched():
    now = datetime.now(timezone.utc)
    assert OddsAPIClient._alternates_due("e1", now - timedelta(minutes=1), now) is False


# ── merging ladders into existing bookmaker data ────────────────────────────────

def test_alternate_markets_are_added_without_losing_featured_ones():
    base = [{"key": "pinnacle", "markets": [{"key": "totals", "outcomes": [{"point": 8.0}]}]}]
    extra = [{"key": "pinnacle",
              "markets": [{"key": "alternate_totals", "outcomes": [{"point": 8.5}]}]}]
    merged = OddsAPIClient._merge_bookmakers(base, extra)
    keys = [m["key"] for b in merged for m in b["markets"]]
    assert "totals" in keys and "alternate_totals" in keys


def test_a_book_present_only_in_the_alternates_response_is_kept():
    base = [{"key": "pinnacle", "markets": []}]
    extra = [{"key": "fanduel",
              "markets": [{"key": "alternate_totals", "outcomes": [{"point": 9.5}]}]}]
    merged = OddsAPIClient._merge_bookmakers(base, extra)
    assert {b["key"] for b in merged} == {"pinnacle", "fanduel"}


def test_consensus_can_read_the_merged_ladder():
    """The payoff: consensus_stats already searches alternate_totals, so a Kalshi
    strike no book features becomes priceable."""
    from core.odds_converter import consensus_stats
    base = [{"key": "pinnacle",
             "markets": [{"key": "totals", "outcomes": [
                 {"name": "Over", "price": -110, "point": 8.0},
                 {"name": "Under", "price": -110, "point": 8.0}]}]}]
    assert consensus_stats(base, "Over", market_key="totals", point=9.5)[0] is None

    extra = [{"key": "pinnacle",
              "markets": [{"key": "alternate_totals", "outcomes": [
                  {"name": "Over", "price": 120, "point": 9.5},
                  {"name": "Under", "price": -140, "point": 9.5}]}]}]
    merged = OddsAPIClient._merge_bookmakers(base, extra)
    v, n, _ = consensus_stats(merged, "Over", market_key="totals", point=9.5)
    assert v is not None and n == 1, "merged ladder did not become priceable"


# ── enrichment spends only where it should ──────────────────────────────────────

def test_enrichment_only_pays_for_requested_events(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "ENABLE_ALTERNATE_LINES", True)
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="alternate_totals": calls.append(e) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    events = [ev("wanted", 6), ev("ignored", 6)]
    spent = client.enrich_with_alternates(events, {"wanted"})
    assert calls == ["wanted"]
    assert spent == 1, "credits spent must equal events fetched"


def test_enrichment_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_ALTERNATE_LINES", False)
    called = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="alternate_totals": called.append(e) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    assert client.enrich_with_alternates([ev()], None) == 0
    assert called == []


def test_a_failed_alternate_fetch_leaves_the_event_usable(monkeypatch):
    """Enrichment is an upgrade, not a dependency -- a failure must degrade to
    featured lines, not lose the event's existing odds."""
    monkeypatch.setattr(config, "ENABLE_ALTERNATE_LINES", True)
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="alternate_totals": [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    e = ev(books=[{"key": "pinnacle", "markets": [{"key": "totals", "outcomes": []}]}])
    client.enrich_with_alternates([e], None)
    assert e.bookmakers and e.bookmakers[0]["key"] == "pinnacle"
