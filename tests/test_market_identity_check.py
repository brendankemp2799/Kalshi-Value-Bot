"""The last gate before real money: does the market we're buying pay what we priced?

Position #930 bought "Chicago WS wins by over 1.5 runs" while pricing "Chicago Cubs
-1.5" -- the opposite team -- because a name lookup resolved a tie in favour of home.

The ambiguity guard added alongside this (tests/test_team_disambiguation.py) refuses
that specific case, but it only catches lookups the matcher KNOWS are uncertain. A
lookup that is confidently WRONG passes it silently. This check is the independent
one: resolve our label and Kalshi's own label separately, and refuse if they name
different clubs. Kalshi's yes_sub_title cannot be wrong about its own market.

The tests below are deliberately split:
  - the incident, replayed with the real strings
  - confidently-wrong matches, which ONLY this check can catch
  - every normal shape (h2h yes/no, draw, totals, spread) must still pass
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.value_detector import Outcome
from execution.trade_executor import verify_market_identity


def opp(outcome, team_name, yes_team, home, away, *, kalshi_outcome="yes",
        threshold=None, ticker="KX-TEST", bet_type="h2h", event_teams=None):
    # event_teams defaults to the fixture actually being priced, so these cases
    # exercise the wrong-fixture gate rather than skipping past it. Pass it
    # explicitly to model a market belonging to some other game.
    km = SimpleNamespace(ticker=ticker, yes_team=yes_team, threshold=threshold,
                         bet_type=bet_type,
                         event_teams=(home, away) if event_teams is None else event_teams)
    ev = SimpleNamespace(home_team=home, away_team=away, sport_key="baseball_mlb",
                         commence_time=datetime(2026, 8, 19, tzinfo=timezone.utc))
    me = SimpleNamespace(odds_event=ev, kalshi_market=km, kalshi_outcome=kalshi_outcome)
    return SimpleNamespace(matched_event=me, outcome=outcome, team_name=team_name,
                           market_price=0.29, edge=0.12, maker_only=False)


# ── the incident ────────────────────────────────────────────────────────────────

def test_the_930_trade_is_refused():
    """Real strings from the live position. This must not be placeable."""
    o = opp(Outcome.COVER, "Chicago Cubs -1.5", yes_team="Chicago WS",
            home="Chicago Cubs", away="Chicago White Sox",
            threshold=1.5, ticker="KXMLBSPREAD-26AUG182005CWSCHC-CWS2",
            bet_type="spread")
    reason = verify_market_identity(o)
    assert reason is not None, "the #930 trade would still be placed"
    assert "Chicago WS" in reason


# ── confidently-wrong matches: ONLY this check catches these ────────────────────

def test_a_confidently_wrong_team_is_refused():
    """Kalshi's YES is unambiguously the White Sox; we priced the Cubs. The ambiguity
    guard passes this (no tie) -- only comparing the two resolutions catches it."""
    o = opp(Outcome.COVER, "Chicago Cubs -1.5", yes_team="Chicago White Sox",
            home="Chicago Cubs", away="Chicago White Sox",
            threshold=1.5, bet_type="spread")
    reason = verify_market_identity(o)
    assert reason is not None
    assert "pays" in reason


def test_buying_yes_when_our_team_is_the_no_side_is_refused():
    o = opp(Outcome.HOME, "Detroit Tigers", yes_team="Minnesota Twins",
            home="Detroit Tigers", away="Minnesota Twins", kalshi_outcome="yes")
    assert verify_market_identity(o) is not None


def test_a_spread_line_mismatch_is_refused():
    """Right team, wrong handicap -- a different bet with a different probability."""
    o = opp(Outcome.COVER, "Minnesota Twins -1.5", yes_team="Minnesota",
            home="Minnesota Twins", away="Detroit Tigers",
            threshold=2.5, bet_type="spread")
    reason = verify_market_identity(o)
    assert reason is not None and "line mismatch" in reason


def test_a_totals_line_mismatch_is_refused():
    o = opp(Outcome.OVER, "Over 8.5", yes_team="Over 9.5 runs scored",
            home="Minnesota Twins", away="Detroit Tigers",
            threshold=9.5, bet_type="totals")
    reason = verify_market_identity(o)
    assert reason is not None and "line mismatch" in reason


def test_a_draw_priced_against_a_team_market_is_refused():
    o = opp(Outcome.DRAW, "Draw", yes_team="Portland",
            home="Portland Timbers", away="San Diego FC")
    assert verify_market_identity(o) is not None


# ── normal trades must still go through ─────────────────────────────────────────

def test_a_correct_home_yes_trade_passes():
    o = opp(Outcome.HOME, "Minnesota Twins", yes_team="Minnesota",
            home="Minnesota Twins", away="Detroit Tigers", kalshi_outcome="yes")
    assert verify_market_identity(o) is None


def test_a_correct_away_no_trade_passes():
    """Buying NO on a market whose YES is the home team pays the away team."""
    o = opp(Outcome.AWAY, "Detroit Tigers", yes_team="Minnesota",
            home="Minnesota Twins", away="Detroit Tigers", kalshi_outcome="yes")
    assert verify_market_identity(o) is None


def test_a_correct_spread_trade_passes():
    """Kalshi's threshold is a positive margin; our label carries a signed handicap."""
    o = opp(Outcome.COVER, "Minnesota Twins -1.5", yes_team="Minnesota",
            home="Minnesota Twins", away="Detroit Tigers",
            threshold=1.5, bet_type="spread")
    assert verify_market_identity(o) is None


def test_a_correct_totals_trade_passes():
    o = opp(Outcome.OVER, "Over 8.5", yes_team="Over 8.5 runs scored",
            home="Minnesota Twins", away="Detroit Tigers",
            threshold=8.5, bet_type="totals")
    assert verify_market_identity(o) is None


def test_buying_no_on_an_over_market_passes():
    """NO_OVER buys the Under side of an Over market — the label still says Over."""
    o = opp(Outcome.NO_OVER, "Over 8.5", yes_team="Over 8.5 runs scored",
            home="Minnesota Twins", away="Detroit Tigers",
            threshold=8.5, bet_type="totals")
    assert verify_market_identity(o) is None


def test_a_correct_draw_trade_passes():
    o = opp(Outcome.DRAW, "Draw", yes_team="Tie",
            home="Portland Timbers", away="San Diego FC")
    assert verify_market_identity(o) is None


def test_shortened_kalshi_names_still_pass():
    """Kalshi abbreviates constantly; the check must not reject that."""
    for ours, theirs, home, away in [
        ("Vancouver Whitecaps FC", "Vancouver", "Vancouver Whitecaps FC", "FC Dallas"),
        ("New England Revolution", "New England", "D.C. United", "New England Revolution"),
        ("Newcastle United", "Newcastle", "Newcastle United", "Liverpool"),
    ]:
        o = opp(Outcome.HOME if home == ours else Outcome.AWAY, ours, theirs, home, away,
                kalshi_outcome="yes" if theirs.split()[0] in home else "no")
        assert verify_market_identity(o) is None, f"rejected a valid match: {ours}/{theirs}"


# ── the gate is actually wired into the order path ──────────────────────────────

def test_execute_trade_refuses_and_never_reaches_kalshi(monkeypatch):
    """The check must BLOCK the order, not just log about it."""
    import execution.kalshi_executor as ke
    import execution.trade_executor as te

    called = []
    monkeypatch.setattr(ke, "place_order",
                        lambda **kw: called.append(kw) or ("x", "submitted", "", 1.0, "taker", 0.0))
    monkeypatch.setattr(te, "log_ambiguous_match", lambda **kw: None, raising=False)

    o = opp(Outcome.COVER, "Chicago Cubs -1.5", yes_team="Chicago WS",
            home="Chicago Cubs", away="Chicago White Sox",
            threshold=1.5, ticker="KXMLBSPREAD-26AUG182005CWSCHC-CWS2",
            bet_type="spread")
    sizing = SimpleNamespace(recommended_dollars=5.44)

    order_id, status, side, reason, stake, fill_type, fee = te.execute_trade(o, sizing)

    assert called == [], "order reached Kalshi despite failing the identity check"
    assert status == "failed"
    assert stake == 0.0
    assert "identity check failed" in reason


# ── props: the guard that used to agree with itself ─────────────────────────────
#
# The first version compared km.participant against opp.team_name. value_detector
# BUILDS team_name from km.participant, so the comparison was a tautology and could
# never refuse anything. These re-derive from Kalshi's own subtitle and ticker, which
# are the only fields that cannot be wrong about the market they describe.

def _player_opp(label, yes_sub_title, ticker="KXMLBKS-26AUG231910ATLMIL-MILSDROHAN73-8"):
    return opp(Outcome.PLAYER, label, yes_team=yes_sub_title,
               home="Milwaukee Brewers", away="Atlanta Braves",
               ticker=ticker, bet_type="player_prop")


def test_a_player_prop_priced_against_the_wrong_rung_is_refused():
    """'Drohan 8+' filled on the 7+ contract is a different bet at a price that looks
    entirely plausible afterwards."""
    o = _player_opp("Shane Drohan 8+", "Shane Drohan: 7+")
    reason = verify_market_identity(o)
    assert reason is not None, "an off-by-one rung would be bought"
    assert "threshold" in reason


def test_a_player_prop_for_a_different_player_is_refused():
    o = _player_opp("Shane Drohan 8+", "Freddy Peralta: 8+")
    reason = verify_market_identity(o)
    assert reason is not None and "player mismatch" in reason


def test_a_ticker_that_disagrees_with_its_own_subtitle_is_refused():
    """Third independent source. Subtitle and ticker both encode the rung."""
    o = _player_opp("Shane Drohan 8+", "Shane Drohan: 8+",
                    ticker="KXMLBKS-26AUG231910ATLMIL-MILSDROHAN73-6")
    reason = verify_market_identity(o)
    assert reason is not None and "6+" in reason


def test_a_correct_player_prop_passes():
    assert verify_market_identity(_player_opp("Shane Drohan 8+", "Shane Drohan: 8+")) is None


def test_an_accented_player_name_still_passes():
    """Kalshi writes 'Ronald Acuna Jr.'; the sportsbook may not. Folding happens in
    _norm_team, so the guard must not reject on the accent alone."""
    o = opp(Outcome.PLAYER, "Ronald Acuna Jr. 2+", yes_team="Ronald Acuña Jr.: 2+",
            home="Milwaukee Brewers", away="Atlanta Braves",
            ticker="KXMLBTB-26AUG231910ATLMIL-ATLRACUNA13-2", bet_type="player_prop")
    assert verify_market_identity(o) is None


def test_an_rfi_priced_against_some_other_market_is_refused():
    """RFI's yes_sub_title is the bare word 'Yes' -- only the title says what it is."""
    o = opp(Outcome.RFI, "First Inning Run", yes_team="Yes",
            home="St. Louis Cardinals", away="Baltimore Orioles",
            ticker="KXMLBRFI-26AUG251945BALSTL", bet_type="rfi")
    o.matched_event.kalshi_market.title = "Baltimore vs St. Louis Total Runs?"
    assert verify_market_identity(o) is not None


def test_a_correct_rfi_passes():
    o = opp(Outcome.RFI, "First Inning Run", yes_team="Yes",
            home="St. Louis Cardinals", away="Baltimore Orioles",
            ticker="KXMLBRFI-26AUG251945BALSTL", bet_type="rfi")
    o.matched_event.kalshi_market.title = "Baltimore vs St. Louis First Inning Run?"
    assert verify_market_identity(o) is None


# ── NO side of the props (2026-08-24) ────────────────────────────────────────────
#
# _detect_player_prop/_detect_binary_prop now also evaluate the NO side of each
# ticker (the player does NOT clear the threshold, both teams do NOT score, no run
# in the 1st). Same discipline as the YES side: side must match what the edge was
# computed on, or this is the 2026-08-22 inversion again on new outcomes.

def _no_player_opp(label, yes_sub_title, ticker="KXMLBKS-26AUG231910ATLMIL-MILSDROHAN73-8"):
    o = opp(Outcome.NO_PLAYER, label, yes_team=yes_sub_title,
            home="Milwaukee Brewers", away="Atlanta Braves",
            ticker=ticker, bet_type="player_prop")
    return o


def test_a_correct_no_player_prop_passes():
    """Label carries 'Under 8' -- the SAME boundary number as Kalshi's '8+' YES
    subtitle, just phrased for the other side."""
    o = _no_player_opp("Shane Drohan Under 8", "Shane Drohan: 8+")
    assert verify_market_identity(o) is None


def test_a_no_player_prop_bought_on_yes_is_refused(monkeypatch):
    """The inversion, replayed on the new outcome: NO_PLAYER's edge is computed on
    Kalshi's NO side, so an order that actually buys YES is the wrong bet -- same
    shape as test_resolve_side.py's test_identity_check_rejects_a_prop_bought_on_
    the_wrong_side, just for the outcome added 2026-08-24."""
    import execution.trade_executor as te
    o = _no_player_opp("Shane Drohan Under 8", "Shane Drohan: 8+")
    monkeypatch.setattr(te, "resolve_side", lambda _o: "yes")
    reason = te.verify_market_identity(o)
    assert reason is not None and "NO side" in reason


def test_a_correct_no_btts_passes():
    o = opp(Outcome.NO_BTTS, "Not Both Teams To Score", yes_team="Both Teams To Score",
            home="Crystal Palace", away="Manchester City")
    assert verify_market_identity(o) is None


def test_a_correct_no_rfi_passes():
    o = opp(Outcome.NO_RFI, "No First Inning Run", yes_team="Yes",
            home="St. Louis Cardinals", away="Baltimore Orioles",
            ticker="KXMLBRFI-26AUG251945BALSTL", bet_type="rfi")
    o.matched_event.kalshi_market.title = "Baltimore vs St. Louis First Inning Run?"
    assert verify_market_identity(o) is None


# ── wrong fixture ─────────────────────────────────────────────────────────────
#
# The check below every other check: is this even the right game? On 2026-08-21..23
# three orders priced Blue Jays @ Yankees and executed on Mets @ White Sox markets,
# and this function passed all three, because _resolve_club scores 25 per shared word
# so "New York" alone carries "New York Mets" onto "New York Yankees".

def test_a_market_from_another_game_is_refused():
    o = opp(Outcome.HOME, "New York Yankees", "New York M",
            home="New York Yankees", away="Toronto Blue Jays",
            ticker="KXMLBGAME-26AUG231410NYMCWS-NYM",
            event_teams=("Chicago W", "New York M"))
    reason = verify_market_identity(o)
    assert reason is not None, "the Mets market must not pass as a Yankees bet"
    assert "wrong fixture" in reason


def test_a_totals_market_from_another_game_is_refused():
    """Totals never look at teams at all, so before this the branch had nothing
    that could reject a wrong-game order."""
    o = opp(Outcome.UNDER, "Under 7.5", "Over 7.5 runs scored",
            home="New York Yankees", away="Toronto Blue Jays",
            threshold=7.5, bet_type="totals",
            ticker="KXMLBTOTAL-26AUG231410NYMCWS-8",
            event_teams=("Chicago W", "New York M"))
    assert "wrong fixture" in (verify_market_identity(o) or "")


def test_the_same_bet_on_the_right_game_passes():
    """The real TORNYY market: Kalshi's YES is Toronto, the AWAY team, so backing
    Toronto buys YES (kalshi_outcome="no" means YES != home)."""
    o = opp(Outcome.AWAY, "Toronto Blue Jays", "Toronto",
            home="New York Yankees", away="Toronto Blue Jays",
            kalshi_outcome="no",
            ticker="KXMLBGAME-26AUG231335TORNYY-TOR",
            event_teams=("New York Y", "Toronto"))
    assert verify_market_identity(o) is None


def test_a_crosstown_market_is_refused_for_the_other_club():
    """Cubs vs White Sox is the same trap with a different city — and it was live
    on the MLB board on 2026-08-23."""
    o = opp(Outcome.HOME, "Chicago White Sox", "Chicago C",
            home="Chicago White Sox", away="Texas Rangers",
            ticker="KXMLBGAME-26AUG242140CHCAZ-CHC",
            event_teams=("Arizona", "Chicago C"))
    assert "wrong fixture" in (verify_market_identity(o) or "")
