"""The correlation rules, which decide whether real money goes down.

Rule 1 was "at most one open position per game" until 2026-08-23. It was replaced by a
dollar cap because the count was measurably the wrong proxy -- of nine same-game
refusals in one live scan, two blocked mutually exclusive outcomes that HEDGE each
other, four refused a better edge than the position they were protecting, and about
one was the correlated-doubling case the rule exists for.

These are the first tests this module has ever had. The rule it replaced also had
none, which is how it kept a hole for months: is_allowed() reads the positions table,
but live entries are not written until after main.py's whole approval loop, so nothing
queued earlier in the SAME scan was visible. Same-game stacking within one scan was
therefore always permitted. That is the `pending_*` half of these tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from core.correlation_tracker import CorrelationTracker


BANKROLL = 100.0          # cap = MAX_GAME_EXPOSURE_PCT * 100 = $2.00 at the 2% default


def opp(home="Newcastle United", away="Liverpool", sport="soccer_epl",
        ticker="KX-NEW", hours_out=6.0):
    ev = SimpleNamespace(
        home_team=home, away_team=away, sport_key=sport,
        commence_time=datetime.now(timezone.utc) + timedelta(hours=hours_out))
    me = SimpleNamespace(odds_event=ev,
                         kalshi_market=SimpleNamespace(ticker=ticker),
                         kalshi_outcome="yes")
    return SimpleNamespace(matched_event=me, outcome=None, team_name="X",
                           consensus_prob=0.5)


def position(home="Newcastle United", away="Liverpool", stake=1.0,
             ticker="KX-OTHER", strategy="value_edge", commence=None):
    """A row shaped like sqlite3.Row usage in the tracker (subscript access)."""
    return {
        "market_ticker": ticker, "home_team": home, "away_team": away,
        "stake": stake, "strategy": strategy,
        "commence_time": (commence or datetime.now(timezone.utc)
                          + timedelta(hours=6)).isoformat(),
    }


@pytest.fixture
def tracker(monkeypatch):
    """A tracker whose bankroll and open positions we control."""
    def _make(open_positions, bankroll=BANKROLL):
        import core.correlation_tracker as ct
        monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: open_positions)
        bm = SimpleNamespace(
            bankroll=bankroll, is_paper=False,
            can_add_exposure=lambda d, s, is_mm=False: (True, "OK"))
        return CorrelationTracker(bm)
    return _make


def cap():
    return config.MAX_GAME_EXPOSURE_PCT * BANKROLL


# ── the first bet on a game is never blocked by this rule ───────────────────────

def test_a_large_first_bet_still_goes_through(tracker):
    """A single Kelly-sized bet is not the concentration Rule 1 guards against, and
    MAX_PCT_BANKROLL/MAX_BET_DOLLARS already bound it. Capping it here would refuse
    every bet on a high-conviction game including the first."""
    t = tracker([])
    allowed, reason = t.is_allowed(opp(), cap() * 5)
    assert allowed, reason


def test_rule_2_does_not_quietly_own_the_same_game_case(tracker):
    """Rule 2's team test matches BOTH clubs of the same fixture, so before this was
    separated it blocked every same-game second bet on the same date -- making Rule 1
    redundant and any cap replacing it inert. Pin the boundary: Rule 2 is for a
    DIFFERENT game sharing a team."""
    t = tracker([position(stake=0.01)])           # same fixture, trivial stake
    allowed, reason = t.is_allowed(opp(ticker="KX-BTTS"), 0.01)
    assert allowed, f"Rule 2 is still swallowing the same-game case: {reason}"


# ── stacking on one game is capped by dollars, not by count ────────────────────

def test_a_second_bet_on_the_same_game_is_allowed_under_the_cap(tracker):
    """THE POINT OF THE CHANGE. The old rule refused this outright."""
    t = tracker([position(stake=0.50)])
    allowed, reason = t.is_allowed(opp(ticker="KX-BTTS"), 0.60)
    assert allowed, reason


def test_stacking_past_the_cap_is_refused(tracker):
    t = tracker([position(stake=cap() - 0.10)])
    allowed, reason = t.is_allowed(opp(ticker="KX-BTTS"), 1.00)
    assert not allowed
    assert "Game exposure" in reason


def test_the_cap_counts_every_bet_type_on_that_game(tracker):
    """Over 8.5 + First Inning Run + a hitter's total bases are three bets on 'this
    game scores'. Kelly sized each as independent; the cap is what notices."""
    held = [position(stake=0.70, ticker="KX-TOT"),
            position(stake=0.70, ticker="KX-RFI"),
            position(stake=0.70, ticker="KX-TB")]
    t = tracker(held)
    allowed, reason = t.is_allowed(opp(ticker="KX-HR"), 0.50)
    assert not allowed and "Game exposure" in reason


def test_the_cap_scales_with_bankroll(tracker):
    """A percentage, not a fixed dollar figure -- it must loosen as the bankroll grows."""
    stake = cap() * 0.9
    assert tracker([position(stake=stake)]).is_allowed(opp(ticker="KX-2"), stake)[0] is False
    rich = tracker([position(stake=stake)], bankroll=BANKROLL * 10)
    assert rich.is_allowed(opp(ticker="KX-2"), stake)[0] is True


def test_a_different_game_is_untouched_by_this_rule(tracker):
    t = tracker([position(home="Arsenal", away="Chelsea", stake=cap() * 3)])
    allowed, reason = t.is_allowed(opp(home="Newcastle United", away="Liverpool",
                                       sport="soccer_epl"), 1.0)
    assert allowed, reason


# ── the within-scan hole the old rule also had ─────────────────────────────────

def test_bets_approved_earlier_in_this_scan_count_against_the_cap(tracker):
    """Live entries reach the DB only after main.py's approval loop finishes, so
    open_positions shows nothing for a game several props were just approved on.
    Without pending_game_stakes every one of them passes."""
    t = tracker([])
    pending = {("Newcastle United", "Liverpool"): cap() - 0.10}
    allowed, reason = t.is_allowed(opp(ticker="KX-BTTS"), 1.00,
                                   pending_game_stakes=pending)
    assert not allowed and "Game exposure" in reason


def test_pending_stakes_on_another_game_do_not_block(tracker):
    t = tracker([])
    pending = {("Arsenal", "Chelsea"): cap() * 3}
    assert t.is_allowed(opp(), 1.0, pending_game_stakes=pending)[0] is True


# ── the rules the change must not have disturbed ───────────────────────────────

def test_the_same_ticker_is_still_never_bet_twice(tracker):
    t = tracker([position(ticker="KX-NEW", stake=0.01)])
    allowed, reason = t.is_allowed(opp(ticker="KX-NEW"), 0.10)
    assert not allowed and "KX-NEW" in reason


def test_same_team_same_day_is_still_blocked(tracker):
    """Rule 2, on a DIFFERENT game involving one of these teams the same day."""
    t = tracker([position(home="Liverpool", away="Everton", stake=0.10,
                          ticker="KX-OTHER")])
    allowed, reason = t.is_allowed(opp(home="Newcastle United", away="Liverpool"), 0.10)
    assert not allowed and "Correlated bet blocked" in reason


def test_arb_pairs_still_bypass_the_game_cap(tracker):
    """Both legs of a true arb are meant to go on together; the cap must not split
    them and leave one side naked."""
    t = tracker([position(stake=cap() * 3)])
    allowed, reason = t.is_allowed(
        opp(ticker="KX-BTTS"), 1.00,
        arb_game_keys={("Newcastle United", "Liverpool")})
    assert allowed, reason


def test_market_making_still_bypasses_the_game_cap(tracker):
    t = tracker([position(stake=cap() * 3, strategy="market_making")])
    allowed, reason = t.is_allowed(opp(ticker="KX-BTTS"), 1.00, is_mm=True)
    assert allowed, reason


def test_bankroll_exposure_is_still_enforced_for_everyone(tracker, monkeypatch):
    """Rule 3 is the backstop and applies even to arb/MM."""
    import core.correlation_tracker as ct
    monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: [])
    bm = SimpleNamespace(bankroll=BANKROLL, is_paper=False,
                         can_add_exposure=lambda d, s, is_mm=False: (False, "too much"))
    t = CorrelationTracker(bm)
    allowed, reason = t.is_allowed(opp(), 1.0, arb_game_keys={("Newcastle United", "Liverpool")})
    assert not allowed and reason == "too much"


def test_a_missing_stake_does_not_crash_the_gate(tracker):
    """Old rows can carry NULL stake; the rule must degrade, not raise."""
    bad = position(stake=None)
    t = tracker([bad])
    allowed, _ = t.is_allowed(opp(ticker="KX-BTTS"), 0.10)
    assert allowed is True
