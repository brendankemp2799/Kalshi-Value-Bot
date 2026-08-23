"""The matcher must not pair a market from a different fixture to our event.

On 2026-08-21..23 the live bot placed three real orders on New York Mets @ Chicago
White Sox markets while pricing Toronto Blue Jays @ New York Yankees ($4.44 staked,
+$1.48 by luck). Two things had to go wrong together:

  1. Kalshi changed h2h titles from "Team1 at Team2 Winner?" to "Team1 wins", so the
     no_team parse produced "" for 100% of MLB and ~47% of soccer h2h markets.
  2. The opponent cross-check was written `if km.no_team:` — so it did not fail, it
     simply stopped running, leaving the match to one fuzzy score against a name
     Kalshi truncates to ~10 characters. "New York M" scores 94.7 against "New York
     Yankees", well over the threshold of 80.

These tests pin both the trigger and the repair.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.market_matcher import _fixture_carries, _team_score, match_events
from data.kalshi_client import KalshiMarket, matchup_key
from data.odds_fetcher import OddsEvent


def _event(home: str, away: str, sport: str = "baseball_mlb") -> OddsEvent:
    return OddsEvent(
        event_id=f"{home[:3]}-{away[:3]}",
        sport_key=sport,
        home_team=home,
        away_team=away,
        commence_time=datetime(2026, 8, 23, 17, 36, tzinfo=timezone.utc),
        bookmakers=[],
    )


def _km(ticker: str, event_ticker: str, yes_team: str,
        event_teams: tuple[str, ...] = (), no_team: str = "",
        title: str = "") -> KalshiMarket:
    """A market in Kalshi's CURRENT shape: title names only the YES team, no_team empty."""
    return KalshiMarket(
        ticker=ticker,
        title=title or f"{yes_team} wins",
        yes_team=yes_team,
        no_team=no_team,
        yes_price=0.52, no_price=0.48,
        yes_bid=0.51, yes_ask=0.53,
        volume=5000,
        close_time="2026-08-24T00:00:00Z",
        category="sports",
        event_ticker=event_ticker,
        bet_type="h2h",
        event_teams=event_teams,
    )


# ── the conditions that made the bug possible ────────────────────────────────

def test_truncated_crosstown_name_still_scores_over_threshold():
    """The trigger. If this ever drops below 80 the bug becomes unreachable by luck,
    but the fixture check is what we actually rely on."""
    assert _team_score("New York Yankees", "New York M") >= 80


def test_opponents_do_not_score_alike():
    """And the signal that separates them, which the fixture check reads."""
    assert _team_score("Toronto Blue Jays", "Chicago W") < 80


# ── _fixture_carries ─────────────────────────────────────────────────────────

def test_fixture_carries_rejects_a_different_game():
    mets = _km("KXMLBGAME-26AUG231410NYMCWS-NYM", "KXMLBGAME-26AUG231410NYMCWS",
               "New York M", event_teams=("Chicago W", "New York M"))
    assert not _fixture_carries(mets, "Toronto Blue Jays", "New York M", 80)


def test_fixture_carries_accepts_the_real_game():
    yanks = _km("KXMLBGAME-26AUG231335TORNYY-TOR", "KXMLBGAME-26AUG231335TORNYY",
                "Toronto", event_teams=("New York Y", "Toronto"))
    assert _fixture_carries(yanks, "New York Yankees", "Toronto", 80)


def test_fixture_carries_falls_back_to_no_team():
    """Legacy title format: event_teams absent but the opponent is still known."""
    km = _km("X-1", "X", "Toronto", no_team="New York Yankees")
    assert _fixture_carries(km, "New York Yankees", "Toronto", 80)
    assert not _fixture_carries(km, "Chicago White Sox", "Toronto", 80)


def test_fixture_carries_refuses_when_opponent_is_unknowable():
    """The heart of the bug: no event_teams AND no no_team must REFUSE, not pass.

    Written as `if km.no_team:` this returned "fine" and the trade went through."""
    km = _km("X-1", "X", "New York M")
    assert not _fixture_carries(km, "Toronto Blue Jays", "New York M", 80)


def test_fixture_carries_ignores_the_matched_label_itself():
    """A one-sided fixture must not satisfy itself by re-reading its own YES label."""
    km = _km("X-1", "X", "New York M", event_teams=("New York M",))
    assert not _fixture_carries(km, "New York Yankees", "New York M", 80)


# ── end to end ───────────────────────────────────────────────────────────────

def test_matcher_rejects_the_mets_market_for_a_yankees_event():
    """The exact live failure, replayed."""
    yankees_event = _event("New York Yankees", "Toronto Blue Jays")
    mets_market = _km(
        "KXMLBGAME-26AUG231410NYMCWS-NYM", "KXMLBGAME-26AUG231410NYMCWS",
        "New York M", event_teams=("Chicago W", "New York M"),
    )
    assert match_events([yankees_event], [mets_market]) == []


def test_matcher_still_pairs_the_correct_market():
    """And the fix must not cost us the real bet — here the YES label is the AWAY
    team, so the event pairs through the NO side."""
    yankees_event = _event("New York Yankees", "Toronto Blue Jays")
    real_market = _km(
        "KXMLBGAME-26AUG231335TORNYY-TOR", "KXMLBGAME-26AUG231335TORNYY",
        "Toronto", event_teams=("New York Y", "Toronto"),
    )
    matched = match_events([yankees_event], [real_market])
    assert len(matched) == 1
    assert matched[0].kalshi_market.ticker == real_market.ticker
    assert matched[0].kalshi_outcome == "no"


def test_matcher_picks_the_right_one_when_both_are_offered():
    """Both markets in the pool, as they are on a real scan day."""
    yankees_event = _event("New York Yankees", "Toronto Blue Jays")
    mets = _km("KXMLBGAME-26AUG231410NYMCWS-NYM", "KXMLBGAME-26AUG231410NYMCWS",
               "New York M", event_teams=("Chicago W", "New York M"))
    real = _km("KXMLBGAME-26AUG231335TORNYY-TOR", "KXMLBGAME-26AUG231335TORNYY",
               "Toronto", event_teams=("New York Y", "Toronto"))
    matched = match_events([yankees_event], [mets, real])
    assert [m.kalshi_market.ticker for m in matched] == [real.ticker]


# ── matchup_key ──────────────────────────────────────────────────────────────

def test_matchup_key_is_shared_across_market_types():
    """Why one map built from the GAME series can also identify a totals fixture,
    which carries no team names at all."""
    assert (matchup_key("KXMLBGAME-26AUG231410NYMCWS")
            == matchup_key("KXMLBTOTAL-26AUG231410NYMCWS")
            == "26AUG231410NYMCWS")


def test_matchup_key_separates_the_two_new_york_games():
    assert matchup_key("KXMLBGAME-26AUG231410NYMCWS") != \
           matchup_key("KXMLBGAME-26AUG231335TORNYY")


# ── name aliases ──────────────────────────────────────────────────────────────

def test_athletics_alias_survives_the_fixture_check():
    """Kalshi calls them "A's"; the sportsbooks say "Athletics". Unaliased that scores
    50, so the fixture check would have thrown away a legitimate Twins @ Athletics
    match — caught against live data before this shipped, not after."""
    assert _team_score("Athletics", "A's") >= 80
    km = _km("KXMLBGAME-26AUG242140MINATH-MIN", "KXMLBGAME-26AUG242140MINATH",
             "Minnesota", event_teams=("A's", "Minnesota"))
    assert _fixture_carries(km, "Athletics", "Minnesota", 80)


def test_alias_does_not_swallow_clubs_that_merely_start_with_as():
    """AS Roma and AS Monaco must not collapse into the Athletics."""
    assert _team_score("AS Roma", "Athletics") < 80
    assert _team_score("AS Monaco", "Athletics") < 80
