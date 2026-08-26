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

def _km(player="Pitcher A", line=4.5, ask=0.45, series="KXMLBKS", bid=None):
    # bid defaults to ask (a zero-width test market): keeps existing single-sided
    # assertions holding without every call site needing to reason about the NO
    # price too. Override where a test specifically wants a NO-side result.
    # event_teams matches _me()'s ev below, so a ValueOpportunity built here can
    # also be run straight through the real verify_market_identity() -- see
    # test_the_no_side_opportunity_survives_the_real_pre_order_gate.
    return SimpleNamespace(
        ticker=f"{series}-26AUG221915PITLAD-X-5", event_ticker=f"{series}-26AUG221915PITLAD",
        bet_type="player_prop", threshold=line, participant=player,
        yes_ask=ask, yes_price=ask, yes_bid=ask if bid is None else bid,
        spread=0.01, volume=900, yes_team=f"{player}: 5+",
        title=f"{player}: 5+ strikeouts?",
        event_teams=("Los Angeles Dodgers", "Pittsburgh Pirates"))


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


# ── 5. the DraftKings anchor-and-scale fallback (2026-08-24) ────────────────────
#
# DraftKings' alternate ladder is Over-only at every rung (verified live), so
# consensus_stats() alone still only prices Pinnacle's one featured line directly.
# _detect_player_prop() falls back to scaled_alternate_diagnostics() for every other
# rung: anchor DraftKings' raw price against Pinnacle's real de-vig at the one point
# they share, apply that ratio elsewhere. These tests pin the INTEGRATION -- that the
# fallback actually fires, that the result is visibly marked, and specifically that
# it survives _quality_check() rather than being silently killed by it (the bug
# caught before shipping: a std_dev of 0.05 would have hard-rejected every one of
# these at quality_check()'s high_uncertainty gate, discovered only by running the
# full pipeline end to end rather than testing the scaling math in isolation).
#
# SHADOW MODE (added later the same day, after an adversarial review). By default
# (config.DK_SCALED_SHADOW_MODE=True) a DK-scaled estimate that clears every gate is
# NOT placed as a real bet -- it is recorded to dk_shadow_log instead. The tests below
# were rewritten to match: "the fallback fires" is now proven by dk_shadow_log and
# scan_log's "dk_shadow_value" status, not by `opps`. A separate section proves the
# graduation path (shadow mode off) still produces the original real-opportunity
# behaviour these tests originally pinned.

def _pinnacle_plus_draftkings_ladder():
    return [
        {"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "Pitcher A", "price": -154, "point": 4.5},
            {"name": "Under", "description": "Pitcher A", "price": 128, "point": 4.5},
        ]}]},
        {"key": "draftkings", "markets": [{"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "Pitcher A", "price": -154, "point": 4.5},
            {"name": "Over", "description": "Pitcher A", "price": 268, "point": 6.5},
        ]}]},
    ]


def test_a_rung_only_draftkings_quotes_now_reaches_a_real_decision():
    """Before 2026-08-24 this rung (Pinnacle silent, DraftKings Over-only) could only
    ever log no_consensus. The scaled fallback should let it reach a real edge
    decision -- but under shadow mode (the default) that decision must not become a
    real bet; it must be visible in dk_shadow_log and scan_log instead."""
    km = _km(player="Pitcher A", line=6.5, ask=0.20)  # Pinnacle has 4.5, not 6.5
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log, shadow = [], [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log, dk_shadow_log=shadow)
    assert opps == [], "shadow mode must never place real capital"
    yes_shadow = [s for s in shadow if s["kalshi_side"] == "yes"]
    assert yes_shadow, f"scaled fallback did not fire: {log}"
    assert yes_shadow[0]["would_bet"] == 1
    assert 0.0 < yes_shadow[0]["scaled_prob"] < 1.0
    yes_log = [e for e in log if e["kalshi_side"] == "yes"]
    assert yes_log[0]["status"] == "dk_shadow_value"
    assert yes_log[0]["bookmaker_count"] == 1
    assert yes_log[0]["consensus_std"] == pytest.approx(0.04)


def test_the_scaled_estimate_is_visibly_marked():
    km = _km(player="Pitcher A", line=6.5, ask=0.20)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    yes_log = [e for e in log if e["kalshi_side"] == "yes"]
    assert yes_log and "[DK-scaled]" in yes_log[0]["team_name"]


def test_scaled_estimates_are_not_killed_by_the_quality_gate():
    """THE BUG THIS TEST EXISTS TO PIN. The scaling math originally returned
    std_dev=0.05 (kelly_calculator's real max-uncertainty-discount value) -- which is
    ALSO above quality_check()'s shared high_uncertainty_std=0.04, checked with book_
    count=1 < high_uncertainty_min_books=4. That combination hard-rejects, so every
    scaled estimate silently became 'high_uncertainty' and never traded, discovered
    only by running the full detector rather than unit-testing the math alone."""
    km = _km(player="Pitcher A", line=6.5, ask=0.20)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert log, "the scaled rung should reach a decision, not vanish silently"
    assert log[0]["status"] != "high_uncertainty", (
        "scaled estimate was hard-rejected by the quality gate instead of "
        "reaching Kelly's soft uncertainty discount"
    )


def test_a_rung_neither_pinnacle_nor_draftkings_quotes_still_finds_no_consensus():
    """The fallback must not turn EVERY rung priceable -- one genuinely nobody
    quotes (11+ Ks, say) still has to log no_consensus, not guess."""
    km = _km(player="Pitcher A", line=10.5, ask=0.05)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert opps == [] and log[0]["status"] == "no_consensus"


# ── 5b. shadow mode itself (2026-08-24) ──────────────────────────────────────────

def test_shadow_mode_is_on_by_default():
    assert config.DK_SCALED_SHADOW_MODE is True


def test_dk_shadow_log_is_not_required_by_callers():
    """dk_shadow_log is optional -- callers that don't care about calibration (e.g.
    most existing tests) must not be forced to pass it."""
    km = _km(player="Pitcher A", line=6.5, ask=0.20)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)  # no dk_shadow_log kwarg
    assert opps == []  # shadow mode still suppresses the trade


def test_shadow_log_records_the_full_calibration_chain():
    km = _km(player="Pitcher A", line=6.5, ask=0.20)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log, shadow = [], [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log, dk_shadow_log=shadow)
    yes_row = next(s for s in shadow if s["kalshi_side"] == "yes")
    assert yes_row["anchor_point"] == pytest.approx(4.5)
    assert yes_row["target_point"] == pytest.approx(6.5)
    assert yes_row["distance"] == pytest.approx(2.0)
    assert yes_row["scaling_ratio"] is not None
    assert yes_row["participant"] == "Pitcher A"
    assert yes_row["kalshi_ticker"] == km.ticker


def test_shadow_log_also_records_rungs_with_no_edge():
    """Calibration needs the full evaluated population, not just the subset that
    would have been bet -- a rung DK-scales but doesn't clear the edge bar must
    still show up, with would_bet=False."""
    km = _km(player="Pitcher A", line=6.5, ask=0.99)  # priced far too rich to clear
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log, shadow = [], [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log, dk_shadow_log=shadow)
    yes_row = next(s for s in shadow if s["kalshi_side"] == "yes")
    assert yes_row["would_bet"] == 0


def test_shadow_mode_off_reproduces_the_original_live_behaviour(monkeypatch):
    """The graduation path: turning DK_SCALED_SHADOW_MODE off must restore exactly
    the pre-shadow-mode behaviour these tests originally pinned -- a cleared DK-scaled
    estimate becomes a real, visibly-marked opportunity."""
    monkeypatch.setattr(config, "DK_SCALED_SHADOW_MODE", False)
    km = _km(player="Pitcher A", line=6.5, ask=0.20)
    me, ev = _me(km, _pinnacle_plus_draftkings_ladder())
    opps, log, shadow = [], [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log, dk_shadow_log=shadow)
    assert opps, "shadow mode off must allow the estimate to become a real bet"
    assert "[DK-scaled]" in opps[0].team_name
    assert opps[0].bookmaker_count == 1
    assert opps[0].consensus_std == pytest.approx(0.04)
    # Still recorded for continued calibration tracking after graduation.
    yes_row = next(s for s in shadow if s["kalshi_side"] == "yes")
    assert yes_row["would_bet"] == 1


# ── 6. the symmetric NO side (2026-08-24) ────────────────────────────────────────
#
# Before this, only the YES/Over edge was ever computed -- a player UNLIKELY to
# clear a threshold, exactly the shape of a good NO bet, was invisible regardless
# of edge. Mirrors _detect_totals's Over/Under split.

def test_a_rich_yes_price_finds_edge_on_the_no_side():
    """Consensus favours Pitcher A clearing 4.5 (~0.58, see test_each_player_is_
    priced_off_his_own_pair) -- price the market so YES is too expensive to buy but
    NO (1 - bid) is cheap relative to 1 - consensus."""
    km = _km(player="Pitcher A", line=4.5, ask=0.95, bid=0.90)
    me, ev = _me(km, _two_pitchers_same_line())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert len(opps) == 1
    o = opps[0]
    assert o.outcome == Outcome.NO_PLAYER
    assert "Pitcher A" in o.team_name and "Under 5" in o.team_name
    assert o.edge > 0


def test_the_no_side_opportunity_survives_the_real_pre_order_gate():
    """Integration, not just unit math: build a NO_PLAYER opportunity through the
    real detector and run it through the real verify_market_identity(), the same
    way the [DK-scaled]-prefix bug was only caught by testing the label against the
    real regex rather than trusting the isolated pieces separately."""
    from execution.trade_executor import verify_market_identity

    km = _km(player="Pitcher A", line=4.5, ask=0.95, bid=0.90)
    me, ev = _me(km, _two_pitchers_same_line())
    opps, log = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, log)
    assert opps and opps[0].outcome == Outcome.NO_PLAYER
    assert verify_market_identity(opps[0]) is None


def test_neither_side_becomes_an_mm_candidate_once_one_side_is_a_real_bet():
    """Same deferred-MM discipline as _detect_h2h/_detect_totals: a resting quote
    must never open on a ticker already held directionally."""
    km = _km(player="Pitcher A", line=4.5, ask=0.45)  # YES clears easily
    me, ev = _me(km, _two_pitchers_same_line())
    opps, mm_cands = [], []
    _detect_player_prop(me, ev, km, 0.01, opps, [], mm_candidates=mm_cands)
    assert len(opps) == 1
    assert mm_cands == [], "MM quoted a ticker we already bet directionally"
