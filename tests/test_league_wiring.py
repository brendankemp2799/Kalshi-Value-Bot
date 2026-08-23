"""Adding a league touches five separate tables. Miss one and it fails quietly.

None of these produce an error -- they produce a league that looks wired and isn't:

  SPORTS                  missing -> never fetched at all
  SPORT_MARKETS           missing -> KeyError or wrong markets bought
  _SPORT_TO_SERIES        missing -> Kalshi side never queried, zero matches
  SOCCER_GAME_SERIES      missing -> the three outcomes of a soccer match collapse to
                                     ONE market per event, so the draw and one team
                                     stop being priced with no warning
  _SERIES_TO_BET_TYPE     missing -> a totals series is treated as h2h and priced
                                     against the wrong sportsbook market
"""
from __future__ import annotations

import re

import config
from data.kalshi_client import (
    _SPORT_TO_SERIES, _SERIES_TO_BET_TYPE, SOCCER_GAME_SERIES,
)


def test_every_sport_we_fetch_has_markets_configured():
    for sport in config.SPORTS:
        assert sport in config.SPORT_MARKETS, f"{sport} has no SPORT_MARKETS entry"


def test_every_sport_we_fetch_has_kalshi_series():
    for sport in config.SPORTS:
        assert sport in _SPORT_TO_SERIES, f"{sport} has no Kalshi series mapped"
        assert _SPORT_TO_SERIES[sport], f"{sport} maps to an empty series list"


def test_no_orphan_kalshi_series():
    """A series mapped for a sport we never fetch costs Kalshi calls for nothing."""
    for sport in _SPORT_TO_SERIES:
        assert sport in config.SPORTS, f"{sport} has Kalshi series but is not in SPORTS"


def test_every_soccer_game_series_is_registered_as_three_way():
    """THE QUIET ONE. Without this the draw and one team vanish from pricing."""
    for sport, series in _SPORT_TO_SERIES.items():
        if not sport.startswith("soccer_"):
            continue
        for s in series:
            if s.endswith("GAME"):
                assert s in SOCCER_GAME_SERIES, (
                    f"{s} ({sport}) is a soccer H2H series but is not in "
                    f"SOCCER_GAME_SERIES -- its draw and one team will never be priced"
                )


def test_no_non_soccer_series_claims_to_be_three_way():
    soccer_series = {s for sport, ser in _SPORT_TO_SERIES.items()
                     if sport.startswith("soccer_") for s in ser}
    for s in SOCCER_GAME_SERIES:
        assert s in soccer_series, f"{s} is in SOCCER_GAME_SERIES but no soccer sport maps it"


def test_every_totals_series_is_typed_as_totals():
    for sport, series in _SPORT_TO_SERIES.items():
        for s in series:
            if s.endswith("TOTAL"):
                assert _SERIES_TO_BET_TYPE.get(s) == "totals", (
                    f"{s} ({sport}) would be priced as h2h against a totals market")


def test_a_sport_asking_for_totals_actually_has_a_totals_series_and_vice_versa():
    """Buying the totals market for a league Kalshi has no totals series for is pure
    credit waste; having the series without buying the market is a dead fetch."""
    for sport in config.SPORTS:
        wants = "totals" in config.SPORT_MARKETS.get(sport, "")
        has = any(s.endswith("TOTAL") for s in _SPORT_TO_SERIES.get(sport, []))
        assert wants == has, (
            f"{sport}: SPORT_MARKETS totals={wants} but Kalshi totals series={has}")


def test_prop_series_are_only_wired_where_the_sportsbook_market_exists():
    """Pinnacle carries no totals_1st_1_innings for NPB or KBO (verified 2026-08-23),
    so an RFI series there would log 'no consensus' on every rung forever."""
    for sport in ("baseball_npb", "baseball_kbo"):
        for s in _SPORT_TO_SERIES.get(sport, []):
            assert not s.endswith("RFI"), (
                f"{s} is wired but Pinnacle quotes no first-inning totals for {sport}")


def test_no_half_time_series_are_wired():
    """Every first-half variant measured 0-5% tradable -- Kalshi lists them, nobody
    trades them."""
    for sport, series in _SPORT_TO_SERIES.items():
        for s in series:
            assert not re.search(r"1H|2H", s), f"{s} ({sport}) is a half-time series"


def test_disabled_bet_types_are_not_wired():
    for sport, series in _SPORT_TO_SERIES.items():
        for s in series:
            bt = _SERIES_TO_BET_TYPE.get(s, "h2h")
            assert bt in config.ENABLED_BET_TYPES, (
                f"{s} ({sport}) is bet_type {bt!r}, which is not enabled")
