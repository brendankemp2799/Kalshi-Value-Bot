"""MLB player props: Kalshi's "N+" ladder against Pinnacle's single line.

MEASURED SHAPE (2026-08-22), which dictates the whole design:

  Kalshi lists a LADDER per player:   Jared Jones 1+,3+,5+,6+,7+,8+ strikeouts
  Pinnacle posts exactly ONE line:    Over 4.5
  -> at most one rung per player is priceable; 27% of tradable Kalshi player
     markets had a matching line.

Pinnacle populates NO *_alternate player markets, so the ladder trick that rescued
totals does not exist here. It also carries none of batter_hits / batter_rbis /
batter_hits_runs_rbis / batter_stolen_bases, which is why only three series are wired.

TWO TRAPS these tests pin:

1. THE OFF-BY-HALF. "2+" means 2 or more, i.e. sportsbook "Over 1.5". Reading it as
   Over 2.5 (or 2.0) prices a different bet while looking perfectly reasonable.

2. CROSS-PLAYER DE-VIG. One player market holds many players, often at the SAME line.
   De-vig pairs by absolute point, so without a participant filter one player's Over
   gets paired against another player's Under -- silently, producing a plausible
   wrong number. This is the same class as the #930 wrong-team bet.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
from core.odds_converter import consensus_stats
from core.value_detector import Outcome, _detect_player_prop
from data.kalshi_client import _parse_player_prop, PLAYER_PROP_MARKET


# ── 1. the off-by-half ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("sub,player,line", [
    ("Zach Neto: 2+", "Zach Neto", 1.5),
    ("Hunter Dobbins: 8+", "Hunter Dobbins", 7.5),
    ("Tarik Skubal: 9+", "Tarik Skubal", 8.5),
    ("Jared Jones: 1+", "Jared Jones", 0.5),
])
def test_n_plus_maps_to_over_n_minus_half(sub, player, line):
    assert _parse_player_prop(sub) == (player, line)


def test_names_with_punctuation_survive():
    """Roster names carry apostrophes and periods -- d'Arnaud, J.T. Realmuto."""
    assert _parse_player_prop("Travis d'Arnaud: 2+") == ("Travis d'Arnaud", 1.5)
    assert _parse_player_prop("J.T. Realmuto: 1+") == ("J.T. Realmuto", 0.5)


@pytest.mark.parametrize("sub", ["Over 8.5 runs scored", "Both Teams To Score",
                                 "Yes", "", "Zach Neto", "Zach Neto: many+"])
def test_non_player_subtitles_are_refused(sub):
    """A market we cannot parse must be skipped, never guessed at."""
    assert _parse_player_prop(sub) is None


# ── 2. cross-player de-vig ──────────────────────────────────────────────────────

def _two_pitchers_same_line():
    """Both pitchers at 4.5 -- the collision that breaks point-only pairing."""
    return [{"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
        {"name": "Over",  "description": "Pitcher A", "price": -184, "point": 4.5},
        {"name": "Under", "description": "Pitcher A", "price": 137,  "point": 4.5},
        {"name": "Over",  "description": "Pitcher B", "price": 150,  "point": 4.5},
        {"name": "Under", "description": "Pitcher B", "price": -180, "point": 4.5},
    ]}]}]


def test_each_player_is_priced_off_his_own_pair():
    b = _two_pitchers_same_line()
    a, _, _ = consensus_stats(b, "Over", market_key="pitcher_strikeouts",
                              point=4.5, participant="Pitcher A")
    c, _, _ = consensus_stats(b, "Over", market_key="pitcher_strikeouts",
                              point=4.5, participant="Pitcher B")
    assert a is not None and c is not None
    # A is the favourite (-184), B the underdog (+150) -- they must differ
    assert a > 0.55 and c < 0.45, f"cross-player contamination: A={a} B={c}"


def test_each_players_pair_sums_to_one():
    b = _two_pitchers_same_line()
    for who in ("Pitcher A", "Pitcher B"):
        o, _, _ = consensus_stats(b, "Over", market_key="pitcher_strikeouts",
                                  point=4.5, participant=who)
        u, _, _ = consensus_stats(b, "Under", market_key="pitcher_strikeouts",
                                  point=4.5, participant=who)
        assert o + u == pytest.approx(1.0, abs=1e-6)


def test_an_absent_player_returns_nothing_rather_than_someone_else():
    b = _two_pitchers_same_line()
    v, n, _ = consensus_stats(b, "Over", market_key="pitcher_strikeouts",
                              point=4.5, participant="Pitcher C")
    assert v is None and n == 0


# ── 3. the detector ─────────────────────────────────────────────────────────────

def _km(player="Pitcher A", line=4.5, ask=0.45, series="KXMLBKS"):
    return SimpleNamespace(
        ticker=f"{series}-26AUG221915PITLAD-X-5", event_ticker=f"{series}-26AUG221915PITLAD",
        bet_type="player_prop", threshold=line, participant=player,
        yes_ask=ask, yes_price=ask, spread=0.01, volume=900, yes_team=f"{player}: 5+",
        title=f"{player}: 5+ strikeouts?")


def _me(km, books):
    ev = SimpleNamespace(home_team="Los Angeles Dodgers", away_team="Pittsburgh Pirates",
                         sport_key="baseball_mlb", bookmakers=books,
                         commence_time=datetime(2026, 8, 22, tzinfo=timezone.utc))
    return SimpleNamespace(odds_event=ev, kalshi_market=km, kalshi_outcome="yes"), ev


def test_a_priceable_rung_becomes_an_opportunity():
    km = _km(ask=0.45)
    me, ev = _me(km, _two_pitchers_same_line())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert len(opps) == 1
    assert opps[0].outcome == Outcome.PLAYER
    assert "Pitcher A" in opps[0].team_name and "5+" in opps[0].team_name


def test_a_rung_pinnacle_does_not_quote_is_skipped_not_guessed():
    """THE common case -- Kalshi lists 6 rungs, Pinnacle quotes one. The other five
    must log no_consensus, not fall back to a nearby line."""
    km = _km(line=7.5)          # Pinnacle only has 4.5
    me, ev = _me(km, _two_pitchers_same_line())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert opps == []
    assert log[0]["status"] == "no_consensus"


def test_an_unparseable_market_is_skipped():
    km = _km()
    km.participant, km.threshold = None, None
    me, ev = _me(km, _two_pitchers_same_line())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert opps == [] and log[0]["status"] == "no_consensus"


# ── 4. config coherence ─────────────────────────────────────────────────────────

def test_only_markets_pinnacle_quotes_are_wired():
    """Measured: Pinnacle carries no batter_hits / batter_rbis / hits_runs_rbis /
    stolen_bases. Wiring one would fetch per-event credits for nothing."""
    assert set(PLAYER_PROP_MARKET.values()) == {
        "pitcher_strikeouts", "batter_home_runs", "batter_total_bases"}


def test_player_prop_is_enabled_with_a_quality_tier():
    assert "player_prop" in config.ENABLED_BET_TYPES
    assert "player_prop" in config.QUALITY_FILTERS


# ── 3. accented names ───────────────────────────────────────────────────────────
#
# Kalshi writes "Ronald Acuña Jr."; The Odds API's `description` may carry a different
# accent convention. The participant filter compares these for EXACT equality after
# normalisation, so an unfolded accent is not a fuzzy near-miss -- it is a total
# lookup failure, and the rung is logged "no consensus" forever. Every accented
# player in MLB was unpriceable until _norm_team started folding.

def _accented_book(kalshi_name, book_name):
    return [{"key": "pinnacle", "markets": [{"key": "batter_total_bases", "outcomes": [
        {"name": "Over",  "description": book_name, "point": 1.5, "price": -140},
        {"name": "Under", "description": book_name, "point": 1.5, "price": 115},
    ]}]}]


@pytest.mark.parametrize("kalshi_name,book_name", [
    ("Ronald Acuña Jr.", "Ronald Acuna Jr."),
    ("Ronald Acuna Jr.", "Ronald Acuña Jr."),
    ("José Ramírez",     "Jose Ramirez"),
    ("Eugenio Suárez",   "Eugenio Suarez"),
])
def test_an_accent_difference_does_not_hide_the_line(kalshi_name, book_name):
    prob, n, _ = consensus_stats(_accented_book(kalshi_name, book_name), "Over",
                                 market_key="batter_total_bases", point=1.5,
                                 participant=kalshi_name)
    assert prob is not None and n == 1, (
        f"{kalshi_name!r} could not find {book_name!r} -- the rung would be "
        f"logged 'no consensus' against data we paid for"
    )


def test_folding_does_not_merge_genuinely_different_players():
    books = _accented_book("Jose Ramirez", "Jose Ramirez")
    prob, n, _ = consensus_stats(books, "Over", market_key="batter_total_bases",
                                 point=1.5, participant="Jose Altuve")
    assert prob is None and n == 0
