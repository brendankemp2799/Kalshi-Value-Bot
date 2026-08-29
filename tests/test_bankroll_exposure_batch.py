"""Regression coverage for the 2026-08-28 exposure-cap batch-blindness incident.

Rule 3 (BankrollManager.can_add_exposure — the account-wide 30%/15% exposure
caps) only ever checked the DB's current open positions. Live orders aren't
written to the DB until AFTER the whole scan's approval loop finishes (they're
recorded in the parallel-execution block, one loop later) — so every
opportunity approved within one scan was checked against the same pre-scan
baseline, blind to every other opportunity approved earlier in that same
batch. On 2026-08-28 a single scan's batch pushed real exposure to ~79% of
account value against a coded 30% cap.

Rule 1 (the per-game dollar cap, same file) already had an identical fix for
the identical blind spot (pending_game_stakes) — these tests pin the same
fix now applied to Rule 3, via pending_total/pending_sport threaded through
CorrelationTracker.is_allowed() into BankrollManager.can_add_exposure().
"""
from __future__ import annotations

import pytest

import config
import core.bankroll_manager as bmm
from core.bankroll_manager import BankrollManager
from core.correlation_tracker import CorrelationTracker
import core.correlation_tracker as ct
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from core.value_detector import Outcome


BANKROLL = 100.0


# ── BankrollManager.can_add_exposure: unit level ────────────────────────────────

@pytest.fixture
def bm(monkeypatch):
    monkeypatch.setattr(bmm.db, "get_open_positions", lambda paper: [])
    return BankrollManager(bankroll=BANKROLL, is_paper=False)


def test_pending_total_counts_toward_the_total_cap(bm):
    # MAX_TOTAL_EXPOSURE_PCT default 0.30 -> cap is $30 on a $100 bankroll.
    # $25 already "pending" from earlier in this scan + $10 more must be refused,
    # even though the DB (mocked to zero open positions) shows nothing at all.
    allowed, reason = bm.can_add_exposure(10.0, "baseball_mlb", pending_total=25.0)
    assert not allowed
    assert "Total exposure" in reason


def test_pending_total_below_cap_still_passes(bm):
    allowed, reason = bm.can_add_exposure(4.0, "baseball_mlb", pending_total=10.0)
    assert allowed, reason


def test_pending_sport_counts_toward_the_sport_cap(bm):
    # MAX_SPORT_EXPOSURE_PCT default 0.15 -> $15 cap on a $100 bankroll.
    allowed, reason = bm.can_add_exposure(
        3.0, "baseball_mlb", pending_total=3.0, pending_sport=13.0,
    )
    assert not allowed
    assert "baseball_mlb exposure" in reason


def test_no_pending_args_behaves_exactly_as_before(bm):
    """Default pending=0.0 must reproduce the pre-fix behavior exactly."""
    allowed, reason = bm.can_add_exposure(4.0, "baseball_mlb")
    assert allowed, reason


# ── CorrelationTracker.is_allowed: the actual incident, reproduced ──────────────

def _opp(ticker, sport="baseball_mlb", home="A", away="B"):
    ev = SimpleNamespace(home_team=home, away_team=away, sport_key=sport,
                         commence_time=datetime.now(timezone.utc) + timedelta(hours=3))
    me = SimpleNamespace(
        odds_event=ev,
        kalshi_market=SimpleNamespace(ticker=ticker, bet_type="player_prop"),
        kalshi_outcome="yes")
    return SimpleNamespace(matched_event=me, outcome=Outcome.PLAYER,
                           team_name=ticker, consensus_prob=0.5)


def test_a_simultaneous_batch_is_capped_in_aggregate_not_individually(monkeypatch):
    """The actual incident: many opportunities from ONE scan, each individually
    well under the cap, whose SUM must not be allowed to blow past it -- because
    none of them are in the DB yet when the others are checked.
    """
    monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: [])
    monkeypatch.setattr(ct.db, "strategies_ever_filled_on", lambda tk, paper=False: set())

    real_bm = BankrollManager(bankroll=BANKROLL, is_paper=False)
    monkeypatch.setattr(bmm.db, "get_open_positions", lambda paper: [])
    tracker = CorrelationTracker(real_bm)

    cap = config.MAX_TOTAL_EXPOSURE_PCT * BANKROLL  # $30 default
    per_bet = cap / 5.0 + 1.0  # 6 of these together exceed the cap; each alone doesn't

    pending_total = 0.0
    pending_by_sport: dict = {}
    approved = 0
    for i in range(10):
        allowed, reason = tracker.is_allowed(
            _opp(f"KX-TEST-{i}"), per_bet,
            pending_exposure_total=pending_total,
            pending_exposure_by_sport=pending_by_sport,
        )
        if allowed:
            approved += 1
            pending_total += per_bet
            pending_by_sport["baseball_mlb"] = pending_by_sport.get("baseball_mlb", 0.0) + per_bet

    total_approved_dollars = approved * per_bet
    assert total_approved_dollars <= cap + 1e-6, (
        f"approved ${total_approved_dollars:.2f} across {approved} bets against a "
        f"${cap:.2f} cap -- the batch blind spot let cumulative exposure through"
    )
    # Sanity: the fix must not be a blanket refusal -- at least one bet should fit.
    assert approved >= 1


def test_without_pending_tracking_the_same_batch_blows_the_cap(monkeypatch):
    """Negative control: confirms the scenario above is a real regression test,
    not a tautology -- omitting pending_exposure_total (the pre-fix call shape)
    lets the same batch sail straight through the cap."""
    monkeypatch.setattr(ct.db, "get_open_positions", lambda paper: [])
    monkeypatch.setattr(ct.db, "strategies_ever_filled_on", lambda tk, paper=False: set())
    monkeypatch.setattr(bmm.db, "get_open_positions", lambda paper: [])

    real_bm = BankrollManager(bankroll=BANKROLL, is_paper=False)
    tracker = CorrelationTracker(real_bm)

    cap = config.MAX_TOTAL_EXPOSURE_PCT * BANKROLL
    per_bet = cap / 5.0 + 1.0

    approved = 0
    for i in range(10):
        allowed, _ = tracker.is_allowed(_opp(f"KX-OLD-{i}"), per_bet)  # no pending kwargs
        if allowed:
            approved += 1

    total_approved_dollars = approved * per_bet
    assert total_approved_dollars > cap, (
        "expected the no-pending-tracking call shape to overshoot the cap "
        "(confirming this test scenario actually exercises the bug)"
    )
