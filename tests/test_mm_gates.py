"""Tests for the market-maker eligibility gates, crossing guard, clip sizing and
requote-skip logic added 2026-08-14.

Context for why each of these exists. A survey of all 979 live Kalshi sports
markets found that of the 230 with a spread >= 5c -- the entire universe MM was
willing to quote -- 179 (78%) had NEVER TRADED. Wide spread and liquidity are
close to mutually exclusive on Kalshi sports: the spread is wide because nobody
is there. Meanwhile MM had recorded exactly one fill in its lifetime, and the
three mechanical reasons were (a) a single clip consumed 89-97% of the whole MM
budget so only one candidate per tick could ever be funded, (b) every resting
leg was cancelled and re-placed every 30s, surrendering queue priority 120x/hour,
and (c) every rejection path was logger.debug, so none of it was visible.
"""
from __future__ import annotations

import pytest

import config
import execution.market_maker as mm
from core.kelly_calculator import mm_clip_size


def _cand(**over) -> dict:
    """A candidate that passes every gate, so each test can break exactly one."""
    base = {
        "consensus_prob": 0.50,
        "kalshi_spread": 0.10,
        "kalshi_volume_24h": 5000.0,
        "bookmaker_count": 5,
        "consensus_std": 0.01,
        "yes_bid": 0.45,
        "yes_ask": 0.55,
    }
    base.update(over)
    return base


def test_baseline_candidate_quotes():
    a = mm.evaluate_mm_candidate(_cand())
    assert a.kind == mm.MMActionKind.QUOTE
    assert a.reason == "ok"


# ── #5 liquidity ──────────────────────────────────────────────────────────────

def test_never_traded_market_is_rejected():
    """The 78% case: a wide spread with zero volume is nobody being there, not a
    fat spread on offer. A quote there cannot fill however it is priced."""
    a = mm.evaluate_mm_candidate(_cand(kalshi_volume_24h=0.0))
    assert a.kind == mm.MMActionKind.NONE
    assert a.reason == "insufficient_volume"


def test_volume_gate_is_skipped_when_volume_is_absent():
    """mm_backtest.py replays candlesticks and cannot supply a lifetime volume.
    A missing key must skip the gate, not reject -- otherwise the backtest
    reports zero opportunities instead of failing loudly."""
    c = _cand()
    del c["kalshi_volume_24h"]
    assert mm.evaluate_mm_candidate(c).kind == mm.MMActionKind.QUOTE


# ── #6 fair-value confidence ──────────────────────────────────────────────────

def test_too_few_books_rejected():
    """_maybe_mm_candidate only forwards markets the directional strategy
    accepted, but that bar is min_bookmaker_count=2. Two books is not enough
    confidence to center a two-sided quote on."""
    a = mm.evaluate_mm_candidate(_cand(bookmaker_count=2))
    assert a.reason == "too_few_books"


def test_high_disagreement_rejected():
    """The directional high_uncertainty_std=0.04 test only applies once 4+ books
    are present, so a 3-book market with a 0.10 std slipped through. Here it is
    unconditional: both legs of a two-sided quote are wrong at once when the
    center is wrong."""
    a = mm.evaluate_mm_candidate(_cand(bookmaker_count=3, consensus_std=0.10))
    assert a.reason == "high_disagreement"


def test_confidence_gates_skipped_when_absent():
    c = _cand()
    del c["bookmaker_count"]
    del c["consensus_std"]
    assert mm.evaluate_mm_candidate(c).kind == mm.MMActionKind.QUOTE


# ── #2 centering + crossing guard ─────────────────────────────────────────────

def test_consensus_far_above_spread_is_directional_not_mm():
    """Consensus 0.70 against a 0.45/0.55 book says the market is WRONG. That is
    a directional signal; quoting around it produces a 0.665 YES bid, above the
    ask."""
    a = mm.evaluate_mm_candidate(_cand(consensus_prob=0.70))
    assert a.kind == mm.MMActionKind.NONE
    assert a.reason == "consensus_outside_spread"


def test_consensus_far_below_spread_is_directional_not_mm():
    a = mm.evaluate_mm_candidate(_cand(consensus_prob=0.30))
    assert a.reason == "consensus_outside_spread"


def test_consensus_just_outside_touch_is_tolerated():
    """MM_CENTERING_TOLERANCE exists so a market that is centered in every
    practical sense isn't disqualified by a 1c difference."""
    a = mm.evaluate_mm_candidate(_cand(consensus_prob=0.56))
    assert a.kind == mm.MMActionKind.QUOTE


def test_quotes_never_cross_the_book():
    """The actual defect this closes: place_resting_quote() rests a plain GTC
    order and treats immediate matching as benign. It is not -- crossing pays the
    TAKER fee (4x maker) at a price the directional model never validated, and
    only the crossing leg fills, leaving naked directional exposure instead of a
    matched pair."""
    for consensus in (0.46, 0.50, 0.54):
        for spread in (0.06, 0.10, 0.20, 0.40):
            bid, ask = 0.50 - spread / 2, 0.50 + spread / 2
            a = mm.evaluate_mm_candidate(
                _cand(consensus_prob=consensus, kalshi_spread=spread,
                      yes_bid=bid, yes_ask=ask))
            if a.kind != mm.MMActionKind.QUOTE:
                continue
            assert a.yes_bid_price < ask, (
                f"YES bid {a.yes_bid_price} crosses ask {ask}")
            # Buying NO at p is economically selling YES at 1-p, which crosses
            # the bid when 1-p <= yes_bid.
            assert (1.0 - a.no_bid_price) > bid, (
                f"NO bid {a.no_bid_price} crosses bid {bid}")


def test_inventory_skew_cannot_push_a_quote_through_the_book():
    """Skew shifts the reservation price by up to 5c, which on a tight book is
    enough to cross on its own -- the guard has to be applied after it."""
    a = mm.evaluate_mm_candidate(_cand(kalshi_spread=0.06, yes_bid=0.47,
                                       yes_ask=0.53),
                                 net_inventory_contracts=-20.0)
    if a.kind == mm.MMActionKind.QUOTE:
        assert a.yes_bid_price < 0.53
        assert (1.0 - a.no_bid_price) > 0.47


# ── fee floor ─────────────────────────────────────────────────────────────────

def test_pair_that_cannot_clear_maker_fees_is_rejected():
    """A matched pair pays exactly $1, so gross capture is 1 - pair_cost and both
    legs pay a maker fee (~0.87c a pair near 50c). Below ~1.3c of spread that is
    negative no matter how the quote is placed."""
    a = mm.evaluate_mm_candidate(
        _cand(kalshi_spread=0.01, yes_bid=0.495, yes_ask=0.505))
    assert a.kind == mm.MMActionKind.NONE
    assert a.reason in ("spread_too_narrow", "below_fee_floor")


def test_quoted_action_reports_positive_net_per_pair():
    a = mm.evaluate_mm_candidate(_cand(kalshi_spread=0.10))
    assert a.net_per_pair is not None
    assert a.net_per_pair >= config.MM_MIN_NET_PER_PAIR
    # Cross-check against the arithmetic independently of the implementation.
    gross = 1.0 - (a.yes_bid_price + a.no_bid_price)
    assert a.net_per_pair < gross, "fees must reduce the gross capture"


# ── #4 clip sizing ────────────────────────────────────────────────────────────

def test_one_clip_no_longer_consumes_the_whole_mm_budget():
    """The one-candidate ceiling, pinned. Measured live at a $157.72 bankroll:
    total MM budget $7.89, a single clip $7.04-$7.68, so the first candidate
    exhausted the budget and ~59 others per scan were rejected by the aggregate
    cap. A clip must now leave room for MM_MAX_CONCURRENT_QUOTES of them."""
    bankroll = 157.72
    budget = config.MM_MAX_EXPOSURE_PCT * bankroll
    for spread in (0.05, 0.06, 0.10, 0.16, 0.30):
        clip = mm_clip_size(spread, bankroll=bankroll)
        assert clip * config.MM_MAX_CONCURRENT_QUOTES <= budget + 1e-6, (
            f"spread {spread}: clip ${clip} x {config.MM_MAX_CONCURRENT_QUOTES} "
            f"exceeds the ${budget:.2f} MM budget")


def test_clip_still_scales_with_spread():
    small = mm_clip_size(0.05, bankroll=100_000)
    large = mm_clip_size(0.20, bankroll=100_000)
    assert large > small


def test_clip_respects_absolute_dollar_cap_at_large_bankroll():
    assert mm_clip_size(0.50, bankroll=1_000_000) <= config.MM_MAX_CLIP_DOLLARS


# ── reasons are always populated ──────────────────────────────────────────────

@pytest.mark.parametrize("over,expected", [
    ({"consensus_prob": None}, "no_consensus"),
    ({"consensus_prob": 0.95, "yes_bid": 0.93, "yes_ask": 0.99}, "outside_fair_value_band"),
    ({"kalshi_spread": 0.02}, "spread_too_narrow"),
    ({"kalshi_volume_24h": 3.0}, "insufficient_volume"),
    ({"bookmaker_count": 1}, "too_few_books"),
    ({"consensus_std": 0.5}, "high_disagreement"),
    ({"consensus_prob": 0.80, "yes_bid": 0.45, "yes_ask": 0.55}, "consensus_outside_spread"),
])
def test_every_rejection_carries_a_specific_reason(over, expected):
    """The visibility fix depends on these strings: they are what reaches
    mm_decision_log and the INFO summary. A bare NONE tells the operator
    nothing, which is the state MM was in."""
    a = mm.evaluate_mm_candidate(_cand(**over))
    assert a.kind == mm.MMActionKind.NONE
    assert a.reason == expected


# ── kickoff stop (2026-08-15) ─────────────────────────────────────────────────

def test_quoting_stops_before_kickoff():
    """A resting quote does not expire when the game starts, and once the event
    leaves mm_candidates run_mm_tick never sees that ticker again — so the last
    chance to cancel is while there is still time on the clock."""
    a = mm.evaluate_mm_candidate(_cand(seconds_to_kickoff=60))
    assert a.kind == mm.MMActionKind.NONE
    assert a.reason == "too_close_to_kickoff"


def test_already_started_game_is_rejected():
    a = mm.evaluate_mm_candidate(_cand(seconds_to_kickoff=-3600))
    assert a.reason == "too_close_to_kickoff"


def test_far_from_kickoff_still_quotes():
    a = mm.evaluate_mm_candidate(_cand(seconds_to_kickoff=86400))
    assert a.kind == mm.MMActionKind.QUOTE


def test_kickoff_margin_covers_the_observed_tick_stall():
    """MM ticks are ~30s normally but measured gaps of 2.4 and 19 minutes occur
    while run_scan() blocks the loop. A margin under that can be slept straight
    through, leaving a live quote in an in-play market."""
    assert config.MM_STOP_QUOTING_BEFORE_KICKOFF_SECONDS >= 19 * 60


def test_kickoff_gate_fires_even_for_an_otherwise_perfect_market():
    """It is a hard risk stop, so it must beat every other gate — including on a
    market with deep volume, tight books and confident consensus."""
    a = mm.evaluate_mm_candidate(_cand(seconds_to_kickoff=10, kalshi_volume_24h=999999,
                                       bookmaker_count=9, consensus_std=0.001))
    assert a.reason == "too_close_to_kickoff"


def test_unknown_kickoff_leaves_the_gate_open():
    """Returning None must not silently halt all market making — the orphan
    sweeper is the backstop for anything that slips through."""
    c = _cand()
    assert "seconds_to_kickoff" not in c
    assert mm.evaluate_mm_candidate(c).kind == mm.MMActionKind.QUOTE
