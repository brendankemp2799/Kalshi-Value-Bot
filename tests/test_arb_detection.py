"""The true-arbitrage detection that bypasses correlation Rules 1-2.

Discovered 2026-08-29: the price-sum check (home_ask + away_ask < 1.0) is
necessary but not sufficient -- a margin smaller than what both legs' real
fees would cost is a guaranteed small LOSS dressed up as risk-free profit,
not an arb. One real detection in this account's history (2026-08-21/22,
New York Yankees/Toronto Blue Jays) had only a 2-cent margin, well inside
the ~3.5 cents a single taker-filled leg at that price could cost. Neither
leg happened to fill that time, so nothing was lost, but the gap was real.

These tests pin: the fee-aware rejection on that exact historical case, that
a genuinely wide margin still passes, and estimated_fee_per_contract()'s
shape (peaks at price=0.50, symmetric).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.kelly_calculator import estimated_fee_per_contract
from core.value_detector import Outcome
from main import _detect_arb_game_keys


def _opp(home, away, outcome, price, sport="baseball_mlb"):
    ev = SimpleNamespace(home_team=home, away_team=away, sport_key=sport,
                         commence_time=datetime.now(timezone.utc) + timedelta(hours=3))
    me = SimpleNamespace(odds_event=ev, kalshi_market=SimpleNamespace(ticker="KX-X"))
    return SimpleNamespace(matched_event=me, outcome=outcome, market_price=price)


def _scored(*opps):
    return [(0.0, o, None) for o in opps]


# ── estimated_fee_per_contract ───────────────────────────────────────────────────

def test_fee_peaks_at_fifty_cents():
    fee_50 = estimated_fee_per_contract(0.50)
    fee_10 = estimated_fee_per_contract(0.10)
    fee_90 = estimated_fee_per_contract(0.90)
    assert fee_50 > fee_10
    assert fee_50 > fee_90


def test_fee_symmetric_around_fifty_cents():
    assert estimated_fee_per_contract(0.30) == pytest.approx(estimated_fee_per_contract(0.70))


def test_fee_matches_the_real_worst_case_figure():
    # RATE=0.07 default: fee(0.49) = 0.49 * 0.07 * 0.51 ~= 0.0175
    assert estimated_fee_per_contract(0.49) == pytest.approx(0.49 * 0.07 * 0.51)


# ── _detect_arb_game_keys ────────────────────────────────────────────────────────

def test_the_real_thin_margin_case_is_now_rejected():
    """2026-08-21/22: HOME ask 0.49 + AWAY ask 0.49 = 0.98, a 2-cent margin --
    below the ~3.5-cent worst-case round-trip fee. Must NOT be treated as a
    risk-free arb."""
    scored = _scored(
        _opp("New York Yankees", "Toronto Blue Jays", Outcome.HOME, 0.49),
        _opp("New York Yankees", "Toronto Blue Jays", Outcome.AWAY, 0.49),
    )
    keys = _detect_arb_game_keys(scored)
    assert ("New York Yankees", "Toronto Blue Jays") not in keys


def test_a_wide_margin_case_still_passes():
    """Same account's other real detection: 0.49 + 0.35 = 0.84, a 16-cent margin
    -- comfortably clears any plausible fee estimate."""
    scored = _scored(
        _opp("New York Yankees", "Toronto Blue Jays", Outcome.HOME, 0.49),
        _opp("New York Yankees", "Toronto Blue Jays", Outcome.AWAY, 0.35),
    )
    keys = _detect_arb_game_keys(scored)
    assert ("New York Yankees", "Toronto Blue Jays") in keys


def test_the_fee_boundary_is_a_hard_cutoff_not_a_fuzzy_one():
    """Bracket the boundary at symmetric prices (home_ask == away_ask): a margin
    one cent below the worst-case fee is rejected, one cent above is accepted.
    Confirms the comparison is a real threshold, not accidentally inverted or
    always-true/always-false."""
    # Solve 1 - 2p == 2*fee(p) numerically for equal home/away prices.
    lo, hi = 0.01, 0.99
    for _ in range(60):
        mid = (lo + hi) / 2
        margin = 1.0 - 2 * mid
        fee = 2 * estimated_fee_per_contract(mid)
        if margin > fee:
            lo = mid
        else:
            hi = mid
    boundary_price = lo  # just inside "accepted" territory

    just_rejected = boundary_price + 0.005  # nudges margin below the fee
    just_accepted = boundary_price - 0.005  # nudges margin above the fee

    rejected_keys = _detect_arb_game_keys(_scored(
        _opp("Home Team", "Away Team", Outcome.HOME, just_rejected),
        _opp("Home Team", "Away Team", Outcome.AWAY, just_rejected),
    ))
    accepted_keys = _detect_arb_game_keys(_scored(
        _opp("Home Team", "Away Team", Outcome.HOME, just_accepted),
        _opp("Home Team", "Away Team", Outcome.AWAY, just_accepted),
    ))
    assert ("Home Team", "Away Team") not in rejected_keys
    assert ("Home Team", "Away Team") in accepted_keys


def test_price_sum_over_one_is_never_an_arb_regardless_of_fees():
    scored = _scored(
        _opp("Home Team", "Away Team", Outcome.HOME, 0.55),
        _opp("Home Team", "Away Team", Outcome.AWAY, 0.55),
    )
    keys = _detect_arb_game_keys(scored)
    assert ("Home Team", "Away Team") not in keys


def test_soccer_games_are_never_treated_as_two_way_arb():
    """3-way soccer can't guarantee payout on both YES sides (a draw pays neither)."""
    scored = _scored(
        _opp("Liverpool", "Newcastle United", Outcome.HOME, 0.40, sport="soccer_epl"),
        _opp("Liverpool", "Newcastle United", Outcome.AWAY, 0.40, sport="soccer_epl"),
    )
    keys = _detect_arb_game_keys(scored)
    assert keys == set()


def test_only_one_side_present_is_not_an_arb():
    scored = _scored(_opp("Home Team", "Away Team", Outcome.HOME, 0.30))
    keys = _detect_arb_game_keys(scored)
    assert keys == set()


def test_unrelated_games_do_not_cross_contaminate():
    scored = _scored(
        _opp("Yankees", "Blue Jays", Outcome.HOME, 0.49),
        _opp("Yankees", "Blue Jays", Outcome.AWAY, 0.49),  # thin margin, rejected
        _opp("Dodgers", "Tigers", Outcome.HOME, 0.40),
        _opp("Dodgers", "Tigers", Outcome.AWAY, 0.40),  # wide margin, accepted
    )
    keys = _detect_arb_game_keys(scored)
    assert ("Yankees", "Blue Jays") not in keys
    assert ("Dodgers", "Tigers") in keys
