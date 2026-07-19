"""Unit tests for de-vig math and team name normalization."""
# conftest.py adds arbitrage_betting_bot/ to sys.path

from core.odds_converter import (
    _names_match,
    _norm_team,
    american_to_prob,
    consensus_stats,
    remove_vig,
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
