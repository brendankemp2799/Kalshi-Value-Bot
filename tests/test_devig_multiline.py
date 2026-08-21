"""De-vigging must pair each outcome with ITS line, not with the whole ladder.

THE BUG (found 2026-08-20, before it ever reached production). consensus_stats
de-vigged against every outcome a book returned under one market key:

    all_probs = [american_to_prob(o["price"]) for o in outcomes]
    no_vig = _devig(all_probs)

A featured `totals` market has exactly two outcomes -- Over and Under at one line --
so that was correct and the distinction never surfaced. `alternate_totals` carries the
full ladder, ~28 outcomes across 14 lines, and normalising across all of them treats
bets that can be simultaneously true as mutually exclusive alternatives.

Measured on live FanDuel data:

    Over 1.5  @ -4500  -> implied 0.9783   consensus_stats returned  0.1316
    Under 1.5 @ +1200  -> implied 0.0769   consensus_stats returned  0.0009
    Over 6.5 + Under 6.5 summed to 0.65 instead of 1.0

The bug was latent only because alternate markets were never fetched (the bulk
endpoint 422s on them). Enabling alternate lines would have activated it, and the
result is not a missed bet -- it is a confidently WRONG price, on every totals market,
in the direction of making longshots look cheap.
"""
from __future__ import annotations

import pytest

from core.odds_converter import consensus_stats


def book(key, market_key, rows, weightless=False):
    """rows = [(name, point, american_price), ...]"""
    return {
        "key": key,
        "markets": [{
            "key": market_key,
            "outcomes": [{"name": n, "point": p, "price": pr} for n, p, pr in rows],
        }],
    }


# ── the incident, with the real numbers ─────────────────────────────────────────

LADDER = [
    ("Over", 1.5, -4500), ("Under", 1.5, 1200),
    ("Over", 2.5, -1600), ("Under", 2.5, 750),
    ("Over", 3.5, -480),  ("Under", 3.5, 330),
    ("Over", 4.5, -295),  ("Under", 4.5, 220),
    ("Over", 8.5, 120),   ("Under", 8.5, -140),
]


def test_a_heavy_favourite_on_a_ladder_keeps_its_probability():
    """Over 1.5 at -4500 is a ~97% shot. It came back as 13%."""
    v, n, _ = consensus_stats([book("fanduel", "alternate_totals", LADDER)],
                              "Over", market_key="totals", point=1.5)
    assert n == 1
    assert v == pytest.approx(0.95, abs=0.04), f"got {v} — de-vigged against the ladder"


def test_each_pair_on_a_ladder_sums_to_one():
    """THE invariant. Over(x) and Under(x) are a complete two-outcome market."""
    b = [book("fanduel", "alternate_totals", LADDER)]
    for line in (1.5, 2.5, 3.5, 4.5, 8.5):
        o, _, _ = consensus_stats(b, "Over", market_key="totals", point=line)
        u, _, _ = consensus_stats(b, "Under", market_key="totals", point=line)
        assert o is not None and u is not None
        assert o + u == pytest.approx(1.0, abs=1e-6), \
            f"line {line}: Over {o:.4f} + Under {u:.4f} = {o + u:.4f}"


def test_probabilities_fall_monotonically_as_the_line_rises():
    """A sanity property no amount of vig should break: Over 1.5 > Over 8.5."""
    b = [book("fanduel", "alternate_totals", LADDER)]
    vals = [consensus_stats(b, "Over", market_key="totals", point=p)[0]
            for p in (1.5, 2.5, 3.5, 4.5, 8.5)]
    assert all(a > c for a, c in zip(vals, vals[1:])), vals


# ── the shapes that must keep working ───────────────────────────────────────────

def test_a_featured_two_outcome_market_is_unchanged():
    """The overwhelming majority of existing data. This fix must be a no-op here."""
    b = [book("pinnacle", "totals", [("Over", 8.5, -110), ("Under", 8.5, -110)])]
    v, n, _ = consensus_stats(b, "Over", market_key="totals", point=8.5)
    assert n == 1
    assert v == pytest.approx(0.5, abs=1e-6)


def test_spreads_pair_by_absolute_line_because_the_signs_are_opposite():
    """Team A -1.5 and Team B +1.5 are one market. Matching on equal point would
    never find the complement."""
    b = [{"key": "pinnacle", "markets": [{"key": "alternate_spreads", "outcomes": [
        {"name": "Team A", "point": -1.5, "price": -110},
        {"name": "Team B", "point": 1.5, "price": -110},
        {"name": "Team A", "point": -3.5, "price": 200},
        {"name": "Team B", "point": 3.5, "price": -250},
    ]}]}]
    a, _, _ = consensus_stats(b, "Team A", market_key="spreads", point=-1.5)
    bb, _, _ = consensus_stats(b, "Team B", market_key="spreads", point=1.5)
    assert a == pytest.approx(0.5, abs=1e-6)
    assert a + bb == pytest.approx(1.0, abs=1e-6)


def test_h2h_still_devigs_across_the_whole_market():
    """A 3-way soccer market IS the complete exclusive set — don't break it."""
    b = [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Team A", "price": 150},
        {"name": "Team B", "price": 200},
        {"name": "Draw", "price": 240},
    ]}]}]
    probs = [consensus_stats(b, n, market_key="h2h")[0] for n in ("Team A", "Team B", "Draw")]
    assert all(p is not None for p in probs)
    assert sum(probs) == pytest.approx(1.0, abs=1e-6)


def test_featured_and_alternate_markets_combine_without_distortion():
    """After enrichment a book carries BOTH keys; the same line must agree."""
    b = [{"key": "fanduel", "markets": [
        {"key": "totals", "outcomes": [
            {"name": "Over", "point": 8.5, "price": -110},
            {"name": "Under", "point": 8.5, "price": -110}]},
        {"key": "alternate_totals", "outcomes": [
            {"name": "Over", "point": 1.5, "price": -4500},
            {"name": "Under", "point": 1.5, "price": 1200}]},
    ]}]
    v85, n85, _ = consensus_stats(b, "Over", market_key="totals", point=8.5)
    v15, _, _ = consensus_stats(b, "Over", market_key="totals", point=1.5)
    assert v85 == pytest.approx(0.5, abs=1e-6), "featured line distorted by the ladder"
    assert v15 > 0.9, "ladder line distorted by the featured market"
