"""DraftKings alternate ladder for MLB player props (2026-08-24).

Pinnacle prices exactly one rung per player and carries no *_alternate player
markets at all (verified live 2026-08-22 and again 2026-08-24 across 8 games) --
so with Pinnacle alone, 224 of 227 no_consensus rows in one scan were simply
"Kalshi lists a rung nobody was ever asked about." DraftKings does carry the full
ladder for all three MLB prop markets (also verified live).

This buys DraftKings' ladder through a second, narrowly-scoped path
(enrich_with_prop_alternates / PROP_ALTERNATE_MARKETS / PROP_ALTERNATE_BOOKMAKERS)
that does NOT touch config.ODDS_API_BOOKMAKERS -- h2h, totals, and the featured
prop line stay Pinnacle-only, so test_single_book_panel.py's invariants
(KELLY_FRACTION, min_bookmaker_count, MM_MIN_BOOKMAKERS) are untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import data.odds_fetcher as of
from data.odds_fetcher import OddsAPIClient, OddsEvent
from core.odds_converter import consensus_stats


@pytest.fixture(autouse=True)
def _clean_cache():
    of._ALT_CACHE.clear()
    yield
    of._ALT_CACHE.clear()


def ev(event_id="e1", hours_out=6.0, sport="baseball_mlb", books=None):
    return OddsEvent(
        event_id=event_id, sport_key=sport, home_team="H", away_team="A",
        commence_time=datetime.now(timezone.utc) + timedelta(hours=hours_out),
        bookmakers=books if books is not None else [],
    )


# ── config coherence ─────────────────────────────────────────────────────────────

def test_prop_alternates_are_scoped_to_draftkings_not_the_main_panel():
    """The point of the whole design: h2h/totals/the featured prop line must stay on
    Pinnacle alone. Only the alternate-ladder fetch reaches for a second book."""
    assert config.ODDS_API_BOOKMAKERS == "pinnacle"
    assert config.PROP_ALTERNATE_BOOKMAKERS != config.ODDS_API_BOOKMAKERS


def test_all_three_mlb_prop_markets_have_an_alternate_wired():
    keys = set(config.PROP_ALTERNATE_MARKETS["baseball_mlb"].split(","))
    assert keys == {"pitcher_strikeouts_alternate", "batter_home_runs_alternate",
                    "batter_total_bases_alternate"}


# ── consensus_stats reads the merged ladder ───────────────────────────────────────

def test_consensus_finds_a_rung_only_draftkings_quotes():
    """The payoff: a Kalshi rung Pinnacle never prices becomes priceable once
    DraftKings' alternate ladder is merged in."""
    base = [{"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
        {"name": "Over", "description": "Logan Gilbert", "price": -184, "point": 5.5},
        {"name": "Under", "description": "Logan Gilbert", "price": 137, "point": 5.5},
    ]}]}]
    assert consensus_stats(base, "Over", market_key="pitcher_strikeouts",
                           point=7.5, participant="Logan Gilbert")[0] is None

    extra = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "Logan Gilbert", "price": 400, "point": 7.5},
            {"name": "Under", "description": "Logan Gilbert", "price": -700, "point": 7.5},
        ]}]}]
    merged = OddsAPIClient._merge_bookmakers(base, extra)
    v, n, _ = consensus_stats(merged, "Over", market_key="pitcher_strikeouts",
                              point=7.5, participant="Logan Gilbert")
    assert v is not None and n == 1, "DraftKings' alternate rung did not become priceable"


def test_a_rung_neither_book_quotes_still_finds_no_consensus():
    base = [{"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
        {"name": "Over", "description": "Logan Gilbert", "price": -184, "point": 5.5}]}]}]
    extra = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "Logan Gilbert", "price": 400, "point": 7.5}]}]}]
    merged = OddsAPIClient._merge_bookmakers(base, extra)
    v, _, _ = consensus_stats(merged, "Over", market_key="pitcher_strikeouts",
                              point=11.5, participant="Logan Gilbert")
    assert v is None, "invented consensus for a rung nobody quotes"


# ── fetch_event_alternates: the bookmakers override ───────────────────────────────

def test_bookmakers_override_replaces_the_global_scope(monkeypatch):
    captured = {}

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return {"bookmakers": []}

    def fake_get(self, path, params):
        captured.update(params)
        return {"bookmakers": []}

    monkeypatch.setattr(OddsAPIClient, "_get", fake_get)
    client = OddsAPIClient.__new__(OddsAPIClient)
    client.fetch_event_alternates("baseball_mlb", "e1",
                                  markets="pitcher_strikeouts_alternate",
                                  bookmakers="draftkings")
    assert captured.get("bookmakers") == "draftkings"


def test_no_override_falls_back_to_the_global_scope(monkeypatch):
    captured = {}

    def fake_get(self, path, params):
        captured.update(params)
        return {"bookmakers": []}

    monkeypatch.setattr(OddsAPIClient, "_get", fake_get)
    client = OddsAPIClient.__new__(OddsAPIClient)
    client.fetch_event_alternates("baseball_mlb", "e1", markets="alternate_totals")
    assert captured.get("bookmakers") == config.ODDS_API_BOOKMAKERS


# ── _enrich stays backward compatible with existing callers ──────────────────────

def test_enrich_omits_bookmakers_kwarg_when_not_given(monkeypatch):
    """enrich_with_alternates/enrich_with_props call _enrich without a bookmakers
    override -- fetch_event_alternates must be called the old (sport, id, markets=)
    way, or every test double standing in for it across the suite breaks."""
    calls = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="alternate_totals": calls.append(
                            "no bookmakers kwarg") or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    client._enrich(ev("e1", 6), "e1", "alternate_totals", datetime.now(timezone.utc))
    assert calls == ["no bookmakers kwarg"]


# ── enrich_with_prop_alternates ────────────────────────────────────────────────────

def test_prop_alternates_use_the_configured_bookmaker(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", True)
    monkeypatch.setattr(config, "PROP_ALTERNATE_MARKETS",
                        {"baseball_mlb": "pitcher_strikeouts_alternate"})
    monkeypatch.setattr(config, "PROP_ALTERNATE_BOOKMAKERS", "draftkings")
    seen = {}
    monkeypatch.setattr(
        OddsAPIClient, "fetch_event_alternates",
        lambda self, s, e, markets="", bookmakers=None: seen.update(
            markets=markets, bookmakers=bookmakers) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    client.enrich_with_prop_alternates([ev("e1", 6)], {"e1"})
    assert seen == {"markets": "pitcher_strikeouts_alternate", "bookmakers": "draftkings"}


def test_prop_alternates_use_a_separate_cache_key_from_featured_props(monkeypatch):
    """Sharing a cache key with enrich_with_props would make each overwrite the
    other's payload instead of both merging into ev.bookmakers."""
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", True)
    monkeypatch.setattr(config, "ENABLE_PROP_MARKETS", True)
    monkeypatch.setattr(config, "PROP_MARKETS", {"baseball_mlb": "pitcher_strikeouts"})
    monkeypatch.setattr(config, "PROP_ALTERNATE_MARKETS",
                        {"baseball_mlb": "pitcher_strikeouts_alternate"})
    featured = [{"key": "pinnacle", "markets": [
        {"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "P", "price": -184, "point": 5.5}]}]}]
    alternates = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "P", "price": 400, "point": 7.5}]}]}]
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="", bookmakers=None:
                            alternates if "alternate" in markets else featured)
    client = OddsAPIClient.__new__(OddsAPIClient)
    e = ev("e1", 6)
    client.enrich_with_props([e], {"e1"})
    client.enrich_with_prop_alternates([e], {"e1"})

    assert of._ALT_CACHE["prop:e1"][1] == featured
    assert of._ALT_CACHE["prop_alt:e1"][1] == alternates
    keys = [m["key"] for b in e.bookmakers for m in b["markets"]]
    assert "pitcher_strikeouts" in keys and "pitcher_strikeouts_alternate" in keys


def test_prop_alternates_no_op_for_a_sport_without_a_ladder_configured(monkeypatch):
    """PROP_ALTERNATE_MARKETS only lists baseball_mlb -- a BTTS/RFI soccer event
    must not spend anything, the same way enrich_with_props already skips sports
    outside PROP_MARKETS."""
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", True)
    called = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="", bookmakers=None:
                            called.append(e) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    spent = client.enrich_with_prop_alternates(
        [ev("e1", 6, sport="soccer_epl")], {"e1"})
    assert called == [] and spent == 0


def test_prop_alternates_respect_the_kill_switch(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", False)
    called = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="", bookmakers=None:
                            called.append(e) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    assert client.enrich_with_prop_alternates([ev("e1", 6)], {"e1"}) == 0
    assert called == []


def test_prop_alternates_only_pay_for_requested_events(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", True)
    monkeypatch.setattr(config, "PROP_ALTERNATE_MARKETS",
                        {"baseball_mlb": "pitcher_strikeouts_alternate"})
    calls = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="", bookmakers=None:
                            calls.append(e) or [])
    client = OddsAPIClient.__new__(OddsAPIClient)
    events = [ev("wanted", 6), ev("ignored", 6)]
    client.enrich_with_prop_alternates(events, {"wanted"})
    assert calls == ["wanted"]


def test_prop_alternates_survive_between_refreshes(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_PROP_ALTERNATE_LINES", True)
    monkeypatch.setattr(config, "PROP_ALTERNATE_MARKETS",
                        {"baseball_mlb": "pitcher_strikeouts_alternate"})
    payload = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "P", "price": 400, "point": 7.5}]}]}]
    calls = []
    monkeypatch.setattr(OddsAPIClient, "fetch_event_alternates",
                        lambda self, s, e, markets="", bookmakers=None:
                            calls.append(e) or payload)
    client = OddsAPIClient.__new__(OddsAPIClient)

    first = ev("e1", 6)
    client.enrich_with_prop_alternates([first], {"e1"})
    later = ev("e1", 5.75)
    spent = client.enrich_with_prop_alternates([later], {"e1"})

    assert calls == ["e1"], "re-bought a ladder that was still fresh"
    assert spent == 0, "served from cache, so nothing should be billed"
    assert any(m["key"] == "pitcher_strikeouts_alternate"
              for b in later.bookmakers for m in b["markets"])
