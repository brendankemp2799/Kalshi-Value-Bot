"""The correlation rules, which decide whether real money goes down.

Rule 1 has been through three shapes. Each test below pins the reason for the
current one.

  1. "one open position per game." Of nine same-game refusals in one live scan, two
     blocked mutually exclusive outcomes that HEDGE each other, four refused a better
     edge than the position they were protecting, and about one was the correlated-
     doubling case the rule exists for.

  2. A flat MAX_GAME_EXPOSURE_PCT = 2% of bankroll. Incoherent: the largest single bet
     allowed (MAX_PCT_BANKROLL, 5%) was 2.5x the whole game budget, so one big bet
     locked the game forever while three small correlated bets got refused at the
     third. 5 of 195 filled bets were larger than the entire game budget.

  3. Current. Bets sharing a FACTOR must not together exceed one max-size bet; the
     game overall must not exceed two. Both derive from MAX_PCT_BANKROLL, so a
     max-size single bet always fits.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from core.correlation_tracker import CorrelationTracker, bet_factor
from core.value_detector import Outcome


BANKROLL = 100.0


def max_bet():
    return config.MAX_PCT_BANKROLL * BANKROLL              # $5.00 at the 5% default


def factor_cap():
    return config.MAX_FACTOR_EXPOSURE_MULTIPLE * max_bet()  # $5.00


def game_cap():
    return config.MAX_GAME_EXPOSURE_MULTIPLE * max_bet()    # $10.00


_OUTCOME_FOR = {
    "h2h": Outcome.HOME, "spread": Outcome.COVER, "totals": Outcome.OVER,
    "btts": Outcome.BTTS, "rfi": Outcome.RFI, "player_prop": Outcome.PLAYER,
}


def opp(bet_type="totals", team_name="Over 8.5", ticker="KX-NEW",
        home="Newcastle United", away="Liverpool", sport="soccer_epl",
        outcome=None, kalshi_outcome="yes"):
    ev = SimpleNamespace(home_team=home, away_team=away, sport_key=sport,
                         commence_time=datetime.now(timezone.utc) + timedelta(hours=6))
    me = SimpleNamespace(
        odds_event=ev,
        kalshi_market=SimpleNamespace(ticker=ticker, bet_type=bet_type),
        kalshi_outcome=kalshi_outcome)
    return SimpleNamespace(matched_event=me,
                           outcome=outcome or _OUTCOME_FOR[bet_type],
                           team_name=team_name, consensus_prob=0.5)


def position(bet_type="totals", team_name="Over 8.5", stake=1.0, side="yes",
             home="Newcastle United", away="Liverpool", ticker="KX-OTHER",
             strategy="value_edge"):
    return {
        "market_ticker": ticker, "home_team": home, "away_team": away,
        "stake": stake, "strategy": strategy, "bet_type": bet_type,
        "team_name": team_name, "side": side,
        "commence_time": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
    }


@pytest.fixture
def tracker(monkeypatch, request):
    def _make(open_positions, bankroll=BANKROLL, also_closed=()):
        """also_closed: positions we filled and have since CLOSED. Invisible to
        get_open_positions, which is exactly how the same-ticker re-entry loop got
        through -- so Rule 0 has to see them."""
        import core.correlation_tracker as ct
        monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: open_positions)

        # Every position here was filled, open or not, so it is "ever filled" too.
        ever = {}
        for p in list(open_positions) + list(also_closed):
            ever.setdefault(p["market_ticker"], set()).add(p.get("strategy", "value_edge"))
        monkeypatch.setattr(ct.db, "strategies_ever_filled_on",
                            lambda tk, paper=False: ever.get(tk, set()))
        bm = SimpleNamespace(bankroll=bankroll, is_paper=False,
                             can_add_exposure=lambda d, s, is_mm=False, pending_total=0.0,
                                                     pending_sport=0.0: (True, "OK"))
        return CorrelationTracker(bm)
    return _make


# ── the incoherence that motivated the rewrite ─────────────────────────────────

def test_the_game_budget_is_never_smaller_than_a_single_allowed_bet():
    """THE BUG IN THE PREVIOUS VERSION. A 2% game cap against a 5% per-bet cap meant
    the largest legal single bet was 2.5x the entire game budget."""
    assert config.MAX_FACTOR_EXPOSURE_MULTIPLE >= 1.0
    assert config.MAX_GAME_EXPOSURE_MULTIPLE >= config.MAX_FACTOR_EXPOSURE_MULTIPLE


def test_a_max_size_bet_does_not_lock_the_game(tracker):
    """An $8.46-scale bet must still leave room for a bet on a different factor."""
    big = position(bet_type="totals", stake=max_bet())
    t = tracker([big])
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Newcastle United", ticker="KX-H2H"), 1.0)
    assert allowed, reason


def test_a_large_first_bet_still_goes_through(tracker):
    t = tracker([])
    allowed, reason = t.is_allowed(opp(), game_cap() * 5)
    assert allowed, reason


# ── the factor cap: correlated bets count as one position ──────────────────────

def test_scoring_bets_share_one_budget(tracker):
    """Over 8.5 + First Inning Run + a hitter's total bases are three bets on 'this
    game scores'. Kelly sized each as independent; this is what notices."""
    held = [position(bet_type="totals", stake=factor_cap() * 0.5),
            position(bet_type="rfi", team_name="First Inning Run",
                     stake=factor_cap() * 0.4, ticker="KX-RFI")]
    t = tracker(held)
    allowed, reason = t.is_allowed(
        opp(bet_type="player_prop", team_name="Bobby Witt Jr. 2+", ticker="KX-TB"),
        factor_cap() * 0.3)
    assert not allowed and "scoring exposure" in reason


def test_a_different_factor_has_its_own_budget(tracker):
    """A full scoring budget must not block a result bet."""
    t = tracker([position(bet_type="totals", stake=factor_cap())])
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Newcastle United", ticker="KX-H2H"), 1.0)
    assert allowed, reason


def test_two_bets_on_one_factor_are_fine_below_the_cap(tracker):
    t = tracker([position(bet_type="totals", stake=1.0)])
    allowed, reason = t.is_allowed(
        opp(bet_type="btts", team_name="Both Teams To Score", ticker="KX-BTTS"), 1.0)
    assert allowed, reason


def test_every_enabled_bet_type_has_an_explicit_factor():
    """An unmapped type gets a bucket of its own rather than inheriting a correlation
    assumption -- the same fallthrough that put 11 positions on the wrong side."""
    for bt in config.ENABLED_BET_TYPES:
        assert not bet_factor(bt).startswith("unmapped:"), (
            f"bet type {bt!r} is enabled but has no factor in _BET_FACTOR")


# ── mutually exclusive outcomes hedge, so they skip the FACTOR cap ──────────────

def test_opposing_h2h_runners_do_not_consume_each_others_budget(tracker):
    """Liverpool win and Newcastle win cannot both pay. Stacking them is not the
    correlated over-betting the factor cap exists to stop."""
    t = tracker([position(bet_type="h2h", team_name="Newcastle United",
                          stake=factor_cap(), ticker="KX-NEW")])
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Liverpool", ticker="KX-LIV"), 1.0)
    assert allowed, reason


def test_but_they_still_count_against_the_game_cap(tracker):
    """They cannot both WIN, but they can all LOSE -- so the concentration cap
    still applies."""
    t = tracker([position(bet_type="h2h", team_name="Newcastle United",
                          stake=game_cap() - 0.10, ticker="KX-NEW")])
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Liverpool", ticker="KX-LIV"), 1.0)
    assert not allowed and "Game exposure" in reason


def test_a_no_bet_is_not_mutually_exclusive_with_the_other_runner(tracker):
    """NO on Newcastle means 'Newcastle does not win', which OVERLAPS with Liverpool
    YES rather than excluding it -- so it must consume the factor budget."""
    t = tracker([position(bet_type="h2h", team_name="Newcastle United",
                          side="no", stake=factor_cap(), ticker="KX-NEW")])
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Liverpool", ticker="KX-LIV"), 1.0)
    assert not allowed and "result exposure" in reason


# ── the game cap ───────────────────────────────────────────────────────────────

def test_the_game_cap_sums_across_factors(tracker):
    """Isolated from the factor cap on purpose: the incoming bet is exempt from its
    OWN factor budget (mutually exclusive h2h), so only the game total can stop it."""
    held = [position(bet_type="totals", stake=factor_cap() - 0.05),
            position(bet_type="h2h", team_name="Newcastle United",
                     stake=factor_cap() - 0.05, ticker="KX-H2H")]
    t = tracker(held)                       # game used = 2 x $4.95 = $9.90 of $10.00
    allowed, reason = t.is_allowed(
        opp(bet_type="h2h", team_name="Liverpool", ticker="KX-LIV"), 0.50)
    assert not allowed, "the game cap did not sum across factors"
    assert "Game exposure" in reason


def test_the_factor_cap_is_reported_when_it_is_the_binding_one(tracker):
    """Both caps can be live at once; the message must name the one that actually
    stopped the bet, or a tightening gets attributed to the wrong dial."""
    held = [position(bet_type="totals", stake=factor_cap()),
            position(bet_type="h2h", team_name="Newcastle United",
                     stake=factor_cap() - 0.10, ticker="KX-H2H")]
    t = tracker(held)
    allowed, reason = t.is_allowed(
        opp(bet_type="spread", team_name="Newcastle United -1.5", ticker="KX-SPR"), 1.0)
    assert not allowed and "result exposure" in reason


def test_the_caps_scale_with_bankroll(tracker):
    held = [position(bet_type="totals", stake=factor_cap() * 0.9)]
    assert tracker(held).is_allowed(opp(bet_type="btts", ticker="KX-B"),
                                    factor_cap() * 0.5)[0] is False
    rich = tracker(held, bankroll=BANKROLL * 10)
    assert rich.is_allowed(opp(bet_type="btts", ticker="KX-B"),
                           factor_cap() * 0.5)[0] is True


def test_a_different_game_is_untouched(tracker):
    t = tracker([position(home="Arsenal", away="Chelsea", stake=game_cap() * 3)])
    allowed, reason = t.is_allowed(opp(), 1.0)
    assert allowed, reason


# ── the within-scan hole ───────────────────────────────────────────────────────

def test_bets_approved_earlier_in_this_scan_count(tracker):
    """Live entries reach the DB only after main.py's approval loop, so open_positions
    shows nothing for a game several props were just approved on."""
    t = tracker([])
    pending = {("Newcastle United", "Liverpool"): [
        {"bet_type": "totals", "team_name": "Over 8.5", "side": "yes",
         "stake": factor_cap() - 0.10}]}
    allowed, reason = t.is_allowed(opp(bet_type="btts", ticker="KX-B"), 1.0,
                                   pending_game_stakes=pending)
    assert not allowed and "scoring exposure" in reason


def test_pending_on_another_game_does_not_block(tracker):
    t = tracker([])
    pending = {("Arsenal", "Chelsea"): [
        {"bet_type": "totals", "team_name": "Over 8.5", "side": "yes",
         "stake": game_cap() * 3}]}
    assert t.is_allowed(opp(), 1.0, pending_game_stakes=pending)[0] is True


# ── rules the change must not have disturbed ───────────────────────────────────

def test_the_same_ticker_is_still_never_bet_twice(tracker):
    t = tracker([position(ticker="KX-NEW", stake=0.01)])
    allowed, reason = t.is_allowed(opp(ticker="KX-NEW"), 0.10)
    assert not allowed and "KX-NEW" in reason


def test_same_team_same_day_on_a_different_game_is_still_blocked(tracker):
    t = tracker([position(home="Liverpool", away="Everton", stake=0.10,
                          ticker="KX-OTHER")])
    allowed, reason = t.is_allowed(opp(bet_type="h2h", team_name="Newcastle United"), 0.10)
    assert not allowed and "Correlated bet blocked" in reason


def test_rule_2_does_not_own_the_same_game_case(tracker):
    """Rule 2's team test matches BOTH clubs of the same fixture, so before this was
    separated it blocked every same-game second bet -- making Rule 1 inert."""
    t = tracker([position(stake=0.01)])
    allowed, reason = t.is_allowed(opp(bet_type="btts", ticker="KX-B"), 0.01)
    assert allowed, f"Rule 2 is still swallowing the same-game case: {reason}"


def test_arb_pairs_still_bypass(tracker):
    t = tracker([position(stake=game_cap() * 3)])
    allowed, reason = t.is_allowed(
        opp(bet_type="btts", ticker="KX-B"), 1.00,
        arb_game_keys={("Newcastle United", "Liverpool")})
    assert allowed, reason


def test_market_making_still_bypasses(tracker):
    t = tracker([position(stake=game_cap() * 3, strategy="market_making")])
    allowed, reason = t.is_allowed(opp(bet_type="btts", ticker="KX-B"), 1.00, is_mm=True)
    assert allowed, reason


def test_bankroll_exposure_is_still_enforced_for_everyone(monkeypatch):
    import core.correlation_tracker as ct
    monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: [])
    bm = SimpleNamespace(bankroll=BANKROLL, is_paper=False,
                         can_add_exposure=lambda d, s, is_mm=False, pending_total=0.0,
                                                 pending_sport=0.0: (False, "too much"))
    t = CorrelationTracker(bm)
    allowed, reason = t.is_allowed(opp(), 1.0,
                                   arb_game_keys={("Newcastle United", "Liverpool")})
    assert not allowed and reason == "too much"


def test_a_missing_stake_does_not_crash_the_gate(tracker):
    t = tracker([position(stake=None)])
    allowed, _ = t.is_allowed(opp(bet_type="btts", ticker="KX-B"), 0.10)
    assert allowed is True


# ── re-entry after a close ────────────────────────────────────────────────────
#
# Rule 0 asked get_open_positions(), so a closed position freed its ticker and the
# next scan re-bought the identical market at the identical price -- the edge had not
# moved, so the opportunity was still sitting there. KXMLBBTTS-26AUG22SJMIN-BTTS went
# round that loop four times on 2026-08-21..22 for -$3.91, each re-entry following a
# stop-loss exit at 20:59, 03:01, 06:02 and 09:03.

def test_a_closed_position_still_blocks_its_ticker(tracker):
    """The San Jose BTTS loop, replayed: nothing open, but we have been here before."""
    prior = position(ticker="KXMLBBTTS-26AUG22SJMIN-BTTS", bet_type="btts",
                     team_name="Both Teams To Score", stake=1.98)
    t = tracker([], also_closed=[prior])
    allowed, reason = t.is_allowed(
        opp(ticker="KXMLBBTTS-26AUG22SJMIN-BTTS", bet_type="btts",
            team_name="Both Teams To Score"), 1.98)
    assert not allowed, "re-bought a market we already closed out of"
    assert "Already bet" in reason


def test_re_entry_is_blocked_however_the_position_closed(tracker):
    """Stop-losses are off, but they were never the rule -- any early exit reopens it."""
    for strategy in ("value_edge", "market_making"):
        prior = position(ticker="KX-CLOSED", strategy=strategy)
        t = tracker([], also_closed=[prior])
        allowed, _ = t.is_allowed(opp(ticker="KX-CLOSED"), 1.0)
        assert not allowed, f"re-entered a closed {strategy} position"


def test_a_ticker_never_touched_is_still_allowed(tracker):
    """The rule must not become a blanket refusal."""
    t = tracker([], also_closed=[position(ticker="KX-SOMETHING-ELSE")])
    allowed, reason = t.is_allowed(opp(ticker="KX-FRESH"), 1.0)
    assert allowed, reason


def test_market_making_may_still_re_quote_its_own_ticker(tracker):
    """MM works by quoting in and out of the same market; that is the strategy, not a
    correlation failure. It stays exempt -- but only against its own inventory."""
    prior = position(ticker="KX-MM", strategy="market_making")
    t = tracker([], also_closed=[prior])
    allowed, reason = t.is_allowed(opp(ticker="KX-MM"), 1.0, is_mm=True)
    assert allowed, reason


def test_market_making_may_not_quote_over_a_closed_value_bet(tracker):
    """The exemption is for MM's own inventory only, matching the open-position rule
    it replaces."""
    prior = position(ticker="KX-VALUE", strategy="value_edge")
    t = tracker([], also_closed=[prior])
    allowed, reason = t.is_allowed(opp(ticker="KX-VALUE"), 1.0, is_mm=True)
    assert not allowed, reason
