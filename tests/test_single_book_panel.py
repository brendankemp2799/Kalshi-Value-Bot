"""Pinnacle-only panel: the dependencies that must move together.

Switching ODDS_API_BOOKMAKERS to a single book is not just a data-source change --
several gates and one sizing rule read "how many books agreed", and they all degrade
silently rather than failing loudly:

  min_bookmaker_count = 2   would reject EVERY candidate (each has exactly 1 book)
  MM_MIN_BOOKMAKERS   = 3   would block ALL market making
  consensus_std       = 0   always, so kelly's uncertainty discount can never fire --
                            measured across 164 real bets it averaged 0.898, so
                            leaving KELLY_FRACTION at 0.25 would have raised every
                            stake by 11.3% as an accidental side effect

These tests exist so that a future revert to a multi-book panel, or a change to any
one of these constants alone, fails here instead of in production.
"""
from __future__ import annotations

import pytest

import config
from core.kelly_calculator import calculate_kelly


def _books():
    return [b.strip() for b in config.ODDS_API_BOOKMAKERS.split(",") if b.strip()]


def test_the_panel_is_pinnacle():
    assert _books() == ["pinnacle"]


def test_quality_filters_accept_a_single_book():
    """A floor of 2 rejects every candidate when only one book is fetched."""
    n = len(_books())
    for tier, qf in config.QUALITY_FILTERS.items():
        assert qf["min_bookmaker_count"] <= n, (
            f"{tier} requires {qf['min_bookmaker_count']} books but only {n} are fetched"
        )


def test_market_making_accepts_a_single_book():
    assert config.MM_MIN_BOOKMAKERS <= len(_books())


def test_kelly_fraction_compensates_for_the_dead_uncertainty_discount():
    """With one book consensus_std is always 0, so uncertainty_factor is always 1.0.
    KELLY_FRACTION must absorb the discount that no longer applies (mean 0.898),
    or stakes rise ~11% purely from changing data source."""
    assert config.KELLY_FRACTION == pytest.approx(0.225, abs=0.005), (
        "KELLY_FRACTION must stay ~0.25 * 0.898 while the panel is single-book"
    )


def test_sizing_is_unchanged_versus_the_old_multi_book_setup():
    """The point of the 0.25 -> 0.225 change: same dollars, not fewer."""
    common = dict(consensus_prob=0.55, market_price=0.50, bankroll=1000.0)
    # old world: 0.25 base, typical disagreement discount of 0.898
    old = calculate_kelly(**common, kelly_fraction=0.25 * 0.898, consensus_std=0.0)
    # new world: 0.225 base, discount can never fire
    new = calculate_kelly(**common, kelly_fraction=config.KELLY_FRACTION, consensus_std=0.0)
    assert new.recommended_dollars == pytest.approx(old.recommended_dollars, rel=0.02)


def test_a_single_book_gets_no_uncertainty_discount():
    """Documents WHY the compensation is needed, rather than assuming it."""
    a = calculate_kelly(consensus_prob=0.55, market_price=0.50, bankroll=1000.0,
                        consensus_std=0.0)
    b = calculate_kelly(consensus_prob=0.55, market_price=0.50, bankroll=1000.0,
                        consensus_std=0.05)
    assert a.recommended_dollars > b.recommended_dollars, \
        "the discount should still work when a std IS present"
    assert a.fractional_kelly == pytest.approx(config.KELLY_FRACTION * a.full_kelly_fraction,
                                               rel=1e-6), "std=0 must mean no discount"


def test_alternate_lines_stay_enabled():
    """Pinnacle's FEATURED line is one number; the ladder is what covers Kalshi's
    strikes. Single-book only works because alternates are on."""
    assert config.ENABLE_ALTERNATE_LINES is True
