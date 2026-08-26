"""Unit tests for de-vig math and team name normalization."""
# conftest.py adds arbitrage_betting_bot/ to sys.path

import pytest

from core.odds_converter import (
    _names_match,
    _norm_team,
    american_to_prob,
    consensus_stats,
    remove_vig,
    scaled_alternate_prob,
    scaled_alternate_diagnostics,
)

# Three-book bookmakers fixture: DK -145, FD -148, BetMGM -142 for Milwaukee.
# Expected de-vigged consensus for Milwaukee ≈ 0.567 (hand-computed below):
#   DK  (-145, +121): raw 0.5918, 0.4525 → vig 1.0443 → dvg 0.5666   wt 0.70
#   FD  (-148, +124): raw 0.5968, 0.4464 → vig 1.0432 → dvg 0.5721   wt 0.70
#   MGM (-142, +118): raw 0.5868, 0.4587 → vig 1.0455 → dvg 0.5612   wt 0.55
#   weighted mean = (0.7*0.5666 + 0.7*0.5721 + 0.55*0.5612) / (0.7+0.7+0.55) ≈ 0.5671
BOOKMAKERS_MLB = [
    {
        "key": "draftkings",
        "title": "DraftKings",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Milwaukee Brewers", "price": -145},
                    {"name": "Miami Marlins",     "price": +121},
                ],
            }
        ],
    },
    {
        "key": "fanduel",
        "title": "FanDuel",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Milwaukee Brewers", "price": -148},
                    {"name": "Miami Marlins",     "price": +124},
                ],
            }
        ],
    },
    {
        "key": "betmgm",
        "title": "BetMGM",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Milwaukee Brewers", "price": -142},
                    {"name": "Miami Marlins",     "price": +118},
                ],
            }
        ],
    },
]


# ── De-vig math ───────────────────────────────────────────────────────────────

def test_consensus_book_count():
    _, count, _ = consensus_stats(BOOKMAKERS_MLB, "Milwaukee Brewers")
    assert count == 3


def test_consensus_within_expected_range():
    consensus, _, _ = consensus_stats(BOOKMAKERS_MLB, "Milwaukee Brewers")
    assert consensus is not None
    assert 0.555 < consensus < 0.580, f"Expected ~0.567, got {consensus:.4f}"


def test_consensus_std_dev_small_when_books_agree():
    _, _, std = consensus_stats(BOOKMAKERS_MLB, "Milwaukee Brewers")
    assert std < 0.01, f"Books agree closely; std_dev should be small, got {std:.4f}"


def test_remove_vig_sums_to_one():
    raw = [american_to_prob(-145), american_to_prob(+121)]
    no_vig = remove_vig(raw)
    assert abs(sum(no_vig) - 1.0) < 1e-9


def test_remove_vig_sums_to_one_three_way():
    # 3-way soccer market
    raw = [american_to_prob(-130), american_to_prob(+330), american_to_prob(+260)]
    no_vig = remove_vig(raw)
    assert abs(sum(no_vig) - 1.0) < 1e-9


def test_american_to_prob_favorite():
    p = american_to_prob(-200)
    assert abs(p - 2 / 3) < 1e-9


def test_american_to_prob_underdog():
    p = american_to_prob(+200)
    assert abs(p - 1 / 3) < 1e-9


def test_consensus_returns_none_for_unknown_team():
    consensus, count, _ = consensus_stats(BOOKMAKERS_MLB, "Pittsburgh Pirates")
    assert consensus is None
    assert count == 0


# ── Team name normalization / matching ────────────────────────────────────────

def test_names_match_city_only():
    assert _names_match("Pittsburgh", "Pittsburgh Pirates")


def test_names_match_abbreviation():
    assert _names_match("TB Rays", "Tampa Bay Rays")


def test_names_match_punctuation():
    assert _names_match("St Louis Blues", "St. Louis Blues")


def test_names_match_full_city():
    assert _names_match("Los Angeles", "Los Angeles Dodgers")


def test_names_no_match_same_city_different_mascot():
    # LA Angels vs LA Dodgers must NOT match — different mascots, same city
    assert not _names_match("Los Angeles Angels", "Los Angeles Dodgers")


def test_names_no_match_ny_teams():
    assert not _names_match("New York Yankees", "New York Mets")


def test_names_no_match_chicago_teams():
    assert not _names_match("Chicago Cubs", "Chicago White Sox")


def test_norm_team_lowercase():
    assert _norm_team("Tampa Bay Rays") == "tampa bay rays"


def test_norm_team_strips_punctuation():
    assert "st" in _norm_team("St. Louis Cardinals")


# ── one-sided quotes must not fabricate a probability ─────────────────────────────
#
# Found live 2026-08-24 while wiring up DraftKings' alternate player-prop ladder:
# every book that carries pitcher_strikeouts_alternate/batter_home_runs_alternate/
# batter_total_bases_alternate (checked DraftKings, FanDuel, BetOnline, William Hill,
# Bovada, Fanatics) sends ONLY the Over leg, never a matching Under, at any point.
# consensus_stats pairs a target outcome with its same-line "siblings" to de-vig --
# with one outcome, remove_vig([p]) is p/p, which is 1.0 for ANY price. Live: Logan
# Gilbert Over 7.5 Ks at DraftKings +268 (a real ~27% shot) came back as "100% certain."
# Worse, blending that fabricated 1.0 into a point Pinnacle DID price correctly (5.5,
# real ~0.57) dragged the combined consensus up to 0.75.

def test_a_lone_outcome_is_skipped_not_devigged_into_certainty():
    """THE BUG. One book, one side, no opposing price to de-vig against -- this must
    return no consensus, not manufacture a fabricated 100%."""
    one_sided = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "Logan Gilbert", "price": 268, "point": 7.5},
        ]}]}]
    v, n, _ = consensus_stats(one_sided, "Over", market_key="pitcher_strikeouts",
                              point=7.5, participant="Logan Gilbert")
    assert v is None and n == 0, "a lone Over leg must not price as a certainty"


def test_a_lone_outcome_does_not_corrupt_a_book_that_has_both_sides():
    """The same bug, but worse: one book's genuine two-sided price got dragged toward
    1.0 by averaging in another book's fabricated certainty at the same line."""
    paired = [{"key": "pinnacle", "markets": [
        {"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "Logan Gilbert", "price": -154, "point": 5.5},
            {"name": "Under", "description": "Logan Gilbert", "price": 128, "point": 5.5},
        ]}]}]
    lone = [{"key": "draftkings", "markets": [
        {"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "Logan Gilbert", "price": -154, "point": 5.5},
        ]}]}]
    only_paired, n_paired, _ = consensus_stats(
        paired, "Over", market_key="pitcher_strikeouts", point=5.5, participant="Logan Gilbert")
    both, n_both, _ = consensus_stats(
        paired + lone, "Over", market_key="pitcher_strikeouts", point=5.5,
        participant="Logan Gilbert")
    assert n_both == n_paired == 1, "the one-sided book must not count as a contributor"
    assert both == pytest.approx(only_paired), \
        "a lone opposing-side-free quote changed a properly-devigged consensus"


# ── scaled_alternate_prob: anchor DraftKings' one-sided ladder against Pinnacle ──
#
# Verified live 2026-08-24: DraftKings' alternate ladder is Over-only at every rung,
# so it can never satisfy consensus_stats()'s two-sided de-vig. This estimates a
# probability anyway by measuring the ratio between Pinnacle's real de-vig and
# DraftKings' raw price at the ONE point they share, then applying that ratio to
# DraftKings' other rungs -- a calibrated estimate, not a second book's opinion.

def _ladder(anchor_price=-154, target_price=268, anchor_point=5.5, target_point=7.5,
           alt_book="draftkings", alt_key_suffix="pitcher_strikeouts_alternate"):
    return [
        {"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "P", "price": anchor_price, "point": anchor_point},
            {"name": "Under", "description": "P", "price": 128, "point": anchor_point},
        ]}]},
        {"key": alt_book, "markets": [{"key": alt_key_suffix, "outcomes": [
            {"name": "Over", "description": "P", "price": anchor_price, "point": anchor_point},
            {"name": "Over", "description": "P", "price": target_price, "point": target_point},
        ]}]},
    ]


def test_scaled_alternate_prob_applies_the_measured_anchor_ratio():
    books = _ladder()
    anchor_fair, _, _ = consensus_stats(books, "Over", market_key="pitcher_strikeouts",
                                        point=5.5, participant="P")
    anchor_raw = american_to_prob(-154)
    target_raw = american_to_prob(268)

    scaled, n, std = scaled_alternate_prob(books, "pitcher_strikeouts", 7.5, "P")
    assert n == 1 and std == pytest.approx(0.04)
    assert scaled == pytest.approx(target_raw * (anchor_fair / anchor_raw))


def test_scaled_alternate_prob_none_without_a_pinnacle_anchor():
    books = [{"key": "draftkings", "markets": [{"key": "pitcher_strikeouts_alternate",
             "outcomes": [{"name": "Over", "description": "P", "price": 268, "point": 7.5}]}]}]
    v, n, _ = scaled_alternate_prob(books, "pitcher_strikeouts", 7.5, "P")
    assert v is None and n == 0


def test_scaled_alternate_prob_none_when_target_is_the_anchor_point():
    """consensus_stats() already prices this directly -- scaling would be redundant."""
    v, n, _ = scaled_alternate_prob(_ladder(), "pitcher_strikeouts", 5.5, "P")
    assert v is None and n == 0


def test_scaled_alternate_prob_none_when_alt_book_misses_the_anchor_point():
    books = [
        {"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "P", "price": -154, "point": 5.5},
            {"name": "Under", "description": "P", "price": 128, "point": 5.5}]}]},
        {"key": "draftkings", "markets": [{"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "P", "price": 268, "point": 7.5}]}]},
    ]
    v, n, _ = scaled_alternate_prob(books, "pitcher_strikeouts", 7.5, "P")
    assert v is None and n == 0, "no overlap point exists -- no ratio is computable"


def test_scaled_alternate_prob_none_when_alt_book_misses_the_target_point():
    books = [
        {"key": "pinnacle", "markets": [{"key": "pitcher_strikeouts", "outcomes": [
            {"name": "Over", "description": "P", "price": -154, "point": 5.5},
            {"name": "Under", "description": "P", "price": 128, "point": 5.5}]}]},
        {"key": "draftkings", "markets": [{"key": "pitcher_strikeouts_alternate", "outcomes": [
            {"name": "Over", "description": "P", "price": -154, "point": 5.5}]}]},
    ]
    v, n, _ = scaled_alternate_prob(books, "pitcher_strikeouts", 7.5, "P")
    assert v is None and n == 0


def test_scaled_alternate_prob_clamps_to_a_valid_probability():
    """A pathological ratio must not escape [0, 1]."""
    books = _ladder(anchor_price=-10000, target_price=-5000, target_point=6.5)
    scaled, n, _ = scaled_alternate_prob(books, "pitcher_strikeouts", 6.5, "P")
    assert scaled is not None and 0.0 <= scaled <= 1.0


def test_scaled_alternate_prob_uses_the_configured_alt_bookmaker(monkeypatch):
    import config
    monkeypatch.setattr(config, "PROP_ALTERNATE_BOOKMAKERS", "fanduel")
    dk_books = _ladder()                        # only draftkings carries the ladder
    fd_books = _ladder(alt_book="fanduel")       # only fanduel does
    assert scaled_alternate_prob(dk_books, "pitcher_strikeouts", 7.5, "P")[0] is None, \
        "must not fall back to draftkings once a different book is configured"
    assert scaled_alternate_prob(fd_books, "pitcher_strikeouts", 7.5, "P")[0] is not None


def test_scaled_estimates_clear_the_shared_high_uncertainty_gate():
    """THE BUG CAUGHT BEFORE SHIPPING. kelly_calculator's real max-discount std is
    0.05, but quality_check() shares high_uncertainty_std=0.04 (strict '>') across
    every bet type, with high_uncertainty_min_books=4 -- and every scaled estimate
    reports book_count=1. 0.05 would have hard-rejected all of them; 0.04 is the
    largest value that still clears the gate. See test_player_props.py for the
    end-to-end version of this same regression."""
    import config
    qf = config.quality_filters("player_prop")
    _, book_count, std_dev = scaled_alternate_prob(_ladder(), "pitcher_strikeouts", 7.5, "P")
    tripped = std_dev > qf["high_uncertainty_std"] and book_count < qf["high_uncertainty_min_books"]
    assert not tripped


# ── scaled_alternate_diagnostics: the full calibration chain (2026-08-24) ────────
#
# Added for shadow-mode logging (core/value_detector.py::_record_dk_shadow) --
# scaled_alternate_prob() is now a thin wrapper around this that only keeps the
# final number. These tests pin the wrapper and the diagnostics function agreeing,
# and that every intermediate value a calibration record needs is actually present.

def test_diagnostics_and_the_wrapper_agree_on_the_final_number():
    books = _ladder()
    diag = scaled_alternate_diagnostics(books, "pitcher_strikeouts", 7.5, "P")
    scaled, n, std = scaled_alternate_prob(books, "pitcher_strikeouts", 7.5, "P")
    assert diag is not None
    assert diag["scaled_prob"] == pytest.approx(scaled)
    assert n == 1 and std == pytest.approx(0.04)


def test_diagnostics_carries_every_intermediate_value():
    books = _ladder(anchor_point=5.5, target_point=7.5)
    diag = scaled_alternate_diagnostics(books, "pitcher_strikeouts", 7.5, "P")
    assert diag["anchor_point"] == pytest.approx(5.5)
    assert diag["distance"] == pytest.approx(2.0)
    assert diag["anchor_raw_prob"] == pytest.approx(american_to_prob(-154))
    assert diag["target_raw_prob"] == pytest.approx(american_to_prob(268))
    assert diag["ratio"] == pytest.approx(diag["anchor_fair_prob"] / diag["anchor_raw_prob"])
    assert diag["scaled_prob"] == pytest.approx(diag["target_raw_prob"] * diag["ratio"])


def test_diagnostics_none_cases_match_the_wrapper():
    """Same refusal cases as scaled_alternate_prob() above -- diagnostics must
    refuse under exactly the same conditions, not just return a differently-shaped
    empty value."""
    no_anchor = [{"key": "draftkings", "markets": [{"key": "pitcher_strikeouts_alternate",
                 "outcomes": [{"name": "Over", "description": "P", "price": 268, "point": 7.5}]}]}]
    assert scaled_alternate_diagnostics(no_anchor, "pitcher_strikeouts", 7.5, "P") is None
    assert scaled_alternate_diagnostics(_ladder(), "pitcher_strikeouts", 5.5, "P") is None
