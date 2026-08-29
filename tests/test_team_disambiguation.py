"""A team match that is AMBIGUOUS must be refused, not guessed.

THE INCIDENT (2026-08-18, position #930, real money). Chicago White Sox @ Chicago
Cubs. Kalshi's covering team on KXMLBSPREAD-...CWSCHC-CWS2 is "Chicago WS":

    _sb_team_match("Chicago WS", home="Chicago Cubs", away="Chicago White Sox")

Word-overlap scored BOTH sides 25 -- only "chicago" is shared, because "WS" matches
neither "white" nor "sox" as a whole word. The old tie-break was

    return home if _score(home) >= _score(away) else away

which silently picked HOME. The Cubs were home. The bot bought 19 contracts of
"Chicago WS wins by over 1.5 runs" while labelling it "Chicago Cubs -1.5" -- the
opposite outcome.

WHY THIS IS WORSE THAN A MISLABELLED BET. The returned name is what consensus_stats()
looks up, so the wrong answer priced the CUBS' cover probability (41.2%) against the
WHITE SOX' market price (29c), manufacturing a 12.2% "edge" out of two unrelated
events. Kelly sizes on edge, so the bad match also produced the third-largest
position ever placed -- $5.51 against a typical $0.88. A matching bug and a sizing
rule that trusts it amplify each other.

The fix is to return None when the two candidates score equally, and skip.
"""
from __future__ import annotations

import pytest

from core.value_detector import _sb_team_match


# ── the incident ────────────────────────────────────────────────────────────────

def test_chicago_ws_is_ambiguous_and_must_not_resolve_to_the_cubs():
    """THE bug, exactly as it occurred."""
    got = _sb_team_match("Chicago WS", home="Chicago Cubs", away="Chicago White Sox")
    assert got != "Chicago Cubs", "resolved to the OPPOSITE team — this is the #930 bug"
    assert got is None, "ambiguous match must be refused, not guessed"


def test_the_reverse_fixture_is_also_ambiguous():
    """Home/away swapped: the answer must not flip to whichever team is home."""
    assert _sb_team_match("Chicago WS", home="Chicago White Sox", away="Chicago Cubs") is None


# ── other same-city pairs that would fail the same way ──────────────────────────

@pytest.mark.parametrize("kalshi,home,away", [
    ("New York", "New York Yankees", "New York Mets"),
    ("Los Angeles", "Los Angeles Dodgers", "Los Angeles Angels"),
    ("Chicago", "Chicago Cubs", "Chicago White Sox"),
])
def test_city_only_names_are_ambiguous_in_a_same_city_matchup(kalshi, home, away):
    """A city-only Kalshi name in a derby identifies nothing. Refuse it."""
    assert _sb_team_match(kalshi, home=home, away=away) is None


# ── the normal cases must keep working ──────────────────────────────────────────

def test_exact_name_still_matches():
    assert _sb_team_match("Chicago Cubs", home="Chicago Cubs",
                          away="Chicago White Sox") == "Chicago Cubs"


def test_full_kalshi_name_resolves_the_same_city_matchup():
    """'Chicago White Sox' is unambiguous even though the city is shared."""
    assert _sb_team_match("Chicago White Sox", home="Chicago Cubs",
                          away="Chicago White Sox") == "Chicago White Sox"


def test_shortened_kalshi_name_still_matches_the_right_team():
    """The whole reason this function exists: Kalshi abbreviates."""
    assert _sb_team_match("Minnesota", home="Minnesota Twins",
                          away="Detroit Tigers") == "Minnesota Twins"
    assert _sb_team_match("Portland", home="Portland Timbers",
                          away="San Diego FC") == "Portland Timbers"


def test_away_team_resolves_when_it_is_the_better_match():
    assert _sb_team_match("Detroit", home="Minnesota Twins",
                          away="Detroit Tigers") == "Detroit Tigers"


def test_substring_match_beats_word_overlap():
    """'Sox' is a substring of 'Red Sox' -> 80, vs word overlap with the other side."""
    assert _sb_team_match("Boston Red Sox", home="Boston Red Sox",
                          away="New York Yankees") == "Boston Red Sox"


# ── no-signal cases ─────────────────────────────────────────────────────────────

def test_a_name_matching_neither_team_is_refused():
    """Previously returned home, i.e. a bet on a team with no evidence at all."""
    assert _sb_team_match("Toronto Raptors", home="Minnesota Twins",
                          away="Detroit Tigers") is None


def test_empty_kalshi_name_is_refused():
    assert _sb_team_match("", home="Minnesota Twins", away="Detroit Tigers") is None


# ── the A's incident (2026-08-29) ────────────────────────────────────────────────
# Kalshi's spread subtitle for the Athletics is "A's wins by X.Y runs or more".
# Stripping the apostrophe before splitting on whitespace turned "A's" into the
# two single-letter tokens "A" and "s", both filtered out by the word-overlap
# scorer's len > 1 check -- so "A's" scored 0 against BOTH sides of every one of
# its own games, not just a same-city collision, and was refused as "ambiguous"
# every single time. See _NICKNAME_ALIASES in core/odds_converter.py.

def test_as_resolves_to_athletics_not_refused_as_ambiguous():
    assert _sb_team_match("A's", home="Athletics", away="Baltimore Orioles") == "Athletics"
    assert _sb_team_match("A's", home="Baltimore Orioles", away="Athletics") == "Athletics"


# ── the caller must skip, not crash, on None ────────────────────────────────────

def test_detect_spread_skips_an_ambiguous_market_without_placing_anything():
    """End-to-end: an ambiguous covering team must produce zero opportunities."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from core.value_detector import _detect_spread

    km = SimpleNamespace(
        threshold=1.5, yes_team="Chicago WS", ticker="KXMLBSPREAD-TEST-CWS2",
        title="Chicago WS wins by over 1.5 runs?", yes_ask=0.29, yes_price=0.29,
        spread=0.01, event_ticker="KXMLBSPREAD-TEST", bet_type="spread", volume=19,
    )
    event = SimpleNamespace(
        home_team="Chicago Cubs", away_team="Chicago White Sox", bookmakers=[],
        sport_key="baseball_mlb",
        commence_time=datetime(2026, 8, 19, 0, 5, tzinfo=timezone.utc),
    )
    me = SimpleNamespace(odds_event=event, kalshi_market=km, kalshi_outcome="yes")

    opportunities, scan_log = [], []
    _detect_spread(me, event, km, 0.01, opportunities, scan_log)

    assert opportunities == [], "placed a bet on an ambiguous team match"
    assert scan_log, "the skip must be logged, not silent"
    assert any("ambiguous_team" in str(r) for r in scan_log), \
        f"expected an ambiguous_team status, got {scan_log}"


# ── accent folding ────────────────────────────────────────────────────────────
#
# Kalshi writes "Malaga"; the sportsbooks write "Málaga". _sb_team_scores compared
# them on a bare .lower(), which shares no characters in the accented position and
# scores 0 -- so verify_market_identity refused the order outright. Logged live twice
# on 2026-08-23, in La Liga, one of the leagues added two days earlier.

@pytest.mark.parametrize("kalshi,sb", [
    ("Malaga", "Málaga"),
    ("Atletico Madrid", "Atlético Madrid"),
    ("Besiktas", "Beşiktaş"),
    ("Deportivo La Coruna", "Deportivo La Coruña"),
])
def test_accented_club_names_resolve(kalshi, sb):
    assert _sb_team_match(kalshi, home=sb, away="Getafe") == sb


def test_accent_folding_does_not_collapse_distinct_clubs():
    """Folding must not make two different clubs indistinguishable."""
    assert _sb_team_match("Málaga", home="Málaga", away="Real Madrid") == "Málaga"
