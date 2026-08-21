"""BTTS and first-inning-run: two binary props, one code path.

Both are plain two-outcome markets on BOTH sides, which is why one detector serves
them -- no line ladder, no team resolution, no three-way de-vig:

    BTTS  Kalshi YES "Both Teams To Score" <-> Odds API `btts` outcome "Yes"
    RFI   Kalshi YES "a run scores in the  <-> Odds API `totals_1st_1_innings`
          1st inning"                           outcome "Over" at line 0.5

Selected by MEASURED tradability against our own filters, not by what Kalshi lists:
KXEPLBTTS 82%, KXLALIGABTTS 67%, KXSERIEABTTS 64%, KXMLBRFI 50%, KXMLSBTTS 47%,
KXLIGUE1BTTS 40% -- while every first-half variant measured 0-5% (KXEPL1HSCORE 0/112)
and is deliberately excluded.

The RFI mapping is the one worth pinning: "Over 0.5 runs" and "a run scored" are the
same event, but only because the line is 0.5. Any other line silently changes the
question.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
from core.value_detector import Outcome, _BINARY_PROPS, _detect_binary_prop


def _mk(bet_type, yes_ask=0.50, spread=0.01):
    return SimpleNamespace(
        ticker=f"KXTEST{bet_type.upper()}-X", event_ticker="KXTEST-X",
        bet_type=bet_type, threshold=None, yes_ask=yes_ask, yes_price=yes_ask,
        spread=spread, volume=5000, yes_team="", title="A vs B")


def _ev(books):
    return SimpleNamespace(home_team="Arsenal", away_team="Coventry City",
                           sport_key="soccer_epl", bookmakers=books,
                           commence_time=datetime(2026, 8, 22, tzinfo=timezone.utc))


def _me(km, ev):
    return SimpleNamespace(odds_event=ev, kalshi_market=km, kalshi_outcome="yes")


def btts_book(yes_price=129, no_price=-155):
    return [{"key": "pinnacle", "markets": [{"key": "btts", "outcomes": [
        {"name": "Yes", "price": yes_price}, {"name": "No", "price": no_price}]}]}]


def rfi_book(over=176, under=-212, point=0.5):
    return [{"key": "pinnacle", "markets": [{"key": "totals_1st_1_innings", "outcomes": [
        {"name": "Over", "price": over, "point": point},
        {"name": "Under", "price": under, "point": point}]}]}]


# ── the mapping itself ──────────────────────────────────────────────────────────

def test_the_two_props_map_to_the_right_market_and_side():
    assert _BINARY_PROPS["btts"][:3] == ("btts", "Yes", None)
    assert _BINARY_PROPS["rfi"][:3] == ("totals_1st_1_innings", "Over", 0.5)


def test_rfi_is_pinned_to_the_half_run_line():
    """'Over 0.5 runs' == 'a run scored' ONLY at 0.5. Over 1.5 is a different bet."""
    assert _BINARY_PROPS["rfi"][2] == 0.5


# ── BTTS ────────────────────────────────────────────────────────────────────────

def test_btts_prices_kalshi_yes_off_the_books_yes():
    km, ev = _mk("btts", yes_ask=0.40), _ev(btts_book())
    opps, log = [], []
    _detect_binary_prop(_me(km, ev), ev, km, 0.01, opps, log)
    assert len(opps) == 1
    o = opps[0]
    assert o.outcome == Outcome.BTTS
    assert o.team_name == "Both Teams To Score"
    # +129 / -155 de-vigs to roughly 0.44 for Yes
    assert 0.40 < o.consensus_prob < 0.48
    assert o.edge > 0


def test_btts_with_no_edge_is_logged_not_bet():
    km, ev = _mk("btts", yes_ask=0.60), _ev(btts_book())
    opps, log = [], []
    _detect_binary_prop(_me(km, ev), ev, km, 0.01, opps, log)
    assert opps == []
    assert log and log[0]["status"] in ("no_edge", "spread_too_wide")


def test_btts_without_sportsbook_data_is_skipped_cleanly():
    km, ev = _mk("btts"), _ev([])
    opps, log = [], []
    _detect_binary_prop(_me(km, ev), ev, km, 0.01, opps, log)
    assert opps == []
    assert log[0]["status"] == "no_consensus"


# ── first-inning run ────────────────────────────────────────────────────────────

def test_rfi_prices_kalshi_yes_off_the_over():
    km, ev = _mk("rfi", yes_ask=0.30), _ev(rfi_book())
    km.bet_type = "rfi"
    opps, log = [], []
    _detect_binary_prop(_me(km, ev), ev, km, 0.01, opps, log)
    assert len(opps) == 1
    o = opps[0]
    assert o.outcome == Outcome.RFI
    assert o.team_name == "First Inning Run"
    # +176 / -212 de-vigs to roughly 0.36 for Over
    assert 0.32 < o.consensus_prob < 0.40


def test_rfi_ignores_a_book_quoting_a_different_line():
    """A book offering only Over 1.5 must not be read as the first-inning-run market."""
    km, ev = _mk("rfi", yes_ask=0.30), _ev(rfi_book(point=1.5))
    km.bet_type = "rfi"
    opps, log = [], []
    _detect_binary_prop(_me(km, ev), ev, km, 0.01, opps, log)
    assert opps == []
    assert log[0]["status"] == "no_consensus"


# ── config coherence ────────────────────────────────────────────────────────────

def test_both_prop_types_are_enabled_and_have_quality_tiers():
    for bt in ("btts", "rfi"):
        assert bt in config.ENABLED_BET_TYPES
        assert bt in config.QUALITY_FILTERS


def test_every_prop_sport_has_a_market_key():
    from data.kalshi_client import _SERIES_TO_BET_TYPE
    prop_sports = {s for s, m in config.PROP_MARKETS.items()}
    assert prop_sports, "no prop sports configured"
    for sport, market in config.PROP_MARKETS.items():
        assert market in {v[0] for v in _BINARY_PROPS.values()}, \
            f"{sport} fetches {market} which no detector consumes"


def test_first_half_series_are_not_wired():
    """Measured 0-5% tradable. If one appears here, someone added it without data."""
    from data.kalshi_client import _SERIES_TO_BET_TYPE
    bad = [s for s in _SERIES_TO_BET_TYPE if "1H" in s or "2H" in s]
    assert bad == [], f"untradable half-markets wired in: {bad}"
