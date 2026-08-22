"""Every Outcome must resolve to a side DELIBERATELY.

THE BUG (2026-08-22, live money). BTTS, RFI and PLAYER were added as Outcome members
without being added to resolve_side. They fell through to the AWAY branch, which
returns "no" — so every prop bet bought the OPPOSITE side from the one the edge was
computed on. 10 positions and $13.95 went on before it was noticed, because a NO bet at
a plausible price looks completely normal in the logs, the dashboard and the DB.

Nothing downstream caught it: verify_market_identity had no case for these outcomes
either, so it returned None (pass).

The guard is exhaustiveness. A new Outcome now raises instead of silently inheriting a
side, and this file fails the moment one is added without a decision.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.value_detector import Outcome
from execution.trade_executor import resolve_side, _YES_SIDE_OUTCOMES


def opp(outcome, kalshi_outcome="yes"):
    return SimpleNamespace(
        matched_event=SimpleNamespace(kalshi_outcome=kalshi_outcome),
        outcome=outcome, team_name="x")


def test_every_outcome_is_handled():
    """THE exhaustiveness guard. Add an Outcome, decide its side, or this fails."""
    for oc in Outcome:
        side = resolve_side(opp(oc))
        assert side in ("yes", "no"), f"{oc} resolved to {side!r}"


@pytest.mark.parametrize("oc", [Outcome.BTTS, Outcome.RFI, Outcome.PLAYER])
def test_the_props_buy_yes(oc):
    """THE regression. Their edge is computed on Kalshi's YES side; buying NO bets
    against our own signal."""
    assert resolve_side(opp(oc)) == "yes"


@pytest.mark.parametrize("oc,expected", [
    (Outcome.DRAW, "yes"), (Outcome.OVER, "yes"), (Outcome.UNDER, "yes"),
    (Outcome.COVER, "yes"), (Outcome.NO_OVER, "no"),
])
def test_pre_existing_outcomes_are_unchanged(oc, expected):
    assert resolve_side(opp(oc)) == expected


def test_h2h_still_follows_the_markets_own_orientation():
    assert resolve_side(opp(Outcome.HOME, "yes")) == "yes"
    assert resolve_side(opp(Outcome.HOME, "no")) == "no"
    assert resolve_side(opp(Outcome.AWAY, "yes")) == "no"
    assert resolve_side(opp(Outcome.AWAY, "no")) == "yes"


def test_an_unknown_outcome_raises_rather_than_defaulting():
    """The whole point: silence is what cost $13.95."""
    with pytest.raises(ValueError, match="no case for"):
        resolve_side(opp("some_future_market_type"))


def test_the_yes_set_and_the_enum_stay_in_sync():
    """A member in neither the YES set nor an explicit branch is a latent inversion."""
    explicit = {Outcome.NO_OVER, Outcome.HOME, Outcome.AWAY}
    unhandled = set(Outcome) - _YES_SIDE_OUTCOMES - explicit
    assert not unhandled, f"Outcome(s) with no deliberate side: {unhandled}"


# ── the identity check must also catch an inverted side ─────────────────────────

def test_identity_check_rejects_a_prop_bought_on_the_wrong_side(monkeypatch):
    import execution.trade_executor as te
    from datetime import datetime, timezone

    km = SimpleNamespace(ticker="KXEPLBTTS-X", yes_team="Both Teams To Score",
                         threshold=None, bet_type="btts", participant=None)
    ev = SimpleNamespace(home_team="Arsenal", away_team="Coventry",
                         sport_key="soccer_epl",
                         commence_time=datetime(2026, 8, 23, tzinfo=timezone.utc))
    o = SimpleNamespace(matched_event=SimpleNamespace(odds_event=ev, kalshi_market=km,
                                                      kalshi_outcome="yes"),
                        outcome=Outcome.BTTS, team_name="Both Teams To Score",
                        market_price=0.5, edge=0.02, maker_only=False)
    assert te.verify_market_identity(o) is None          # correct side passes

    monkeypatch.setattr(te, "resolve_side", lambda _o: "no")   # simulate the bug
    reason = te.verify_market_identity(o)
    assert reason is not None and "YES side" in reason
