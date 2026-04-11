"""
Utility functions for converting between odds formats and implied probabilities.

American odds examples:
  +150  →  40.0%  implied probability
  -150  →  60.0%  implied probability
"""
from __future__ import annotations

# Sportsbook abbreviations → full city/region name.
# Some books (especially DraftKings) abbreviate team names in their spread/
# totals outcome data even though The Odds API provides full names at the
# event level (e.g. event.home_team = "Tampa Bay Rays" but DraftKings spread
# outcome = "TB Rays").  Normalizing both sides before comparison fixes this.
_ABBREV: dict[str, str] = {
    # Multi-letter city codes
    "TB":  "tampa bay",
    "LA":  "los angeles",
    "NY":  "new york",
    "GS":  "golden state",
    "KC":  "kansas city",
    "SF":  "san francisco",
    "NE":  "new england",
    "NO":  "new orleans",
    "OKC": "oklahoma city",
    "LV":  "las vegas",
    "SD":  "san diego",
    # MLB three-letter codes
    "NYY": "new york",   "NYM": "new york",
    "BOS": "boston",     "TOR": "toronto",
    "BAL": "baltimore",  "TB":  "tampa bay",
    "CLE": "cleveland",  "DET": "detroit",
    "CWS": "chicago",    "CHW": "chicago",
    "MIN": "minnesota",  "KCR": "kansas city",
    "HOU": "houston",    "TEX": "texas",
    "OAK": "oakland",    "SEA": "seattle",
    "LAA": "los angeles","ATH": "athletics",
    "NYY": "new york",   "BOS": "boston",
    "ATL": "atlanta",    "MIA": "miami",
    "NYM": "new york",   "PHI": "philadelphia",
    "WSH": "washington", "WAS": "washington",
    "CHC": "chicago",    "MIL": "milwaukee",
    "STL": "st. louis",  "PIT": "pittsburgh",
    "CIN": "cincinnati", "COL": "colorado",
    "ARI": "arizona",    "SF":  "san francisco",
    "LAD": "los angeles","SDP": "san diego",
    # NBA/NHL three-letter codes
    "LAL": "los angeles", "LAC": "los angeles",
    "GSW": "golden state","PHX": "phoenix",
    "PHL": "philadelphia","PHI": "philadelphia",
    "IND": "indiana",     "MIL": "milwaukee",
    "CLE": "cleveland",   "DET": "detroit",
    "CHI": "chicago",     "ATL": "atlanta",
    "MIA": "miami",       "ORL": "orlando",
    "WAS": "washington",  "BKN": "brooklyn",
    "TOR": "toronto",     "BOS": "boston",
    "SAS": "san antonio", "DAL": "dallas",
    "DEN": "denver",      "POR": "portland",
    "SAC": "sacramento",  "MEM": "memphis",
    "NOP": "new orleans", "OKC": "oklahoma city",
    "UTA": "utah",        "MIN": "minnesota",
    # NFL
    "NE":  "new england", "NO":  "new orleans",
    "KC":  "kansas city", "LV":  "las vegas",
    "LAR": "los angeles", "LAC": "los angeles",
    "JAX": "jacksonville","JAC": "jacksonville",
    "TEN": "tennessee",   "IND": "indianapolis",
    "CIN": "cincinnati",  "PIT": "pittsburgh",
    "BAL": "baltimore",   "CLE": "cleveland",
    "DAL": "dallas",      "NYG": "new york",
    "PHI": "philadelphia","WAS": "washington",
    "CHI": "chicago",     "DET": "detroit",
    "GB":  "green bay",   "MIN": "minnesota",
    "ATL": "atlanta",     "CAR": "carolina",
    "TB":  "tampa bay",   "ARI": "arizona",
    "SEA": "seattle",     "SF":  "san francisco",
    "HOU": "houston",     "BUF": "buffalo",
}


import re as _re


def _norm_team(name: str) -> str:
    """
    Normalize a team name for comparison:
      - Strip punctuation ("St." → "St", "St. Louis" → "St Louis")
      - Expand common abbreviations ("TB" → "tampa bay", "MIN" → "minnesota")
      - Lowercase everything
    e.g. "TB Rays" → "tampa bay rays", "St. Louis Cardinals" → "st louis cardinals"
    """
    # Remove punctuation except hyphens inside words
    cleaned = _re.sub(r"[^\w\s-]", " ", name)
    words = cleaned.strip().split()
    out = []
    for w in words:
        out.append(_ABBREV.get(w.upper(), w).lower())
    return " ".join(out)


def _names_match(a: str, b: str) -> bool:
    """
    Return True if two team name strings refer to the same team.

    Used for spread/totals market lookups where sportsbooks may use
    shortened or abbreviated forms of team names:
      "Pittsburgh"        matches "Pittsburgh Pirates"   (city-only)
      "TB Rays"           matches "Tampa Bay Rays"       (abbreviation)
      "St Louis Blues"    matches "St. Louis Blues"      (punctuation)
      "Los Angeles"       matches "Los Angeles Clippers" (city-only)

    Does NOT match two different teams that share only a city name:
      "Los Angeles Angels" ≠ "Los Angeles Dodgers"  (different mascots)
      "New York Yankees"   ≠ "New York Mets"        (different mascots)

    This is safe when combined with a point-value filter (spread markets
    have distinct point values per side, so we can't accidentally match
    the wrong team even with permissive name matching).
    """
    na, nb = _norm_team(a), _norm_team(b)
    if na == nb:
        return True
    # Substring containment handles city-only vs full name:
    #   "pittsburgh" in "pittsburgh pirates" ✓
    #   "los angeles" in "los angeles dodgers" ✓  (city-only lookup)
    # But we must NOT match "los angeles angels" ⊂ "los angeles dodgers"
    # because neither is a substring of the other — that falls through to
    # the word-overlap check below, which we guard more carefully.
    if na in nb or nb in na:
        return True
    # Shared significant words (>3 chars) as a last resort.
    # Guard: only match if one side has NO unshared significant words —
    # i.e. one name is a strict subset of the other (city-only vs full name).
    # "pittsburgh" vs "pittsburgh pirates": a_words={"pittsburgh"},
    #   b_words={"pittsburgh","pirates"}, shared={"pittsburgh"},
    #   a has no unshared words → match ✓
    # "los angeles angels" vs "los angeles dodgers": shared={"angeles"},
    #   a_unshared={"angels"}, b_unshared={"dodgers"} → both have unshared → no match ✓
    a_words = {w for w in na.split() if len(w) > 3}
    b_words = {w for w in nb.split() if len(w) > 3}
    if a_words and b_words:
        shared = a_words & b_words
        if shared:
            a_unshared = a_words - shared
            b_unshared = b_words - shared
            # Only match if one side is fully contained in the other
            if not a_unshared or not b_unshared:
                return True
    return False


# Sharpness weights by Odds API book key (US region).
#
# Pinnacle is NOT available in the Odds API "us" region — omitted.
# Sharpest books actually available in "us":
#   LowVig / BetOnline: reduced-vig / offshore, attract sharp money
#   DraftKings / FanDuel: dominant US retail, highly accurate due to volume
#   BetMGM / Caesars / BetRivers: solid mid-tier retail
#   Bovada / MyBookie: softest offshore lines
#   Barstool rebranded to ESPN Bet (espnbet) in late 2023; kept at low weight
#   for any historical data that still carries the old key.
#
# Any book not listed falls back to DEFAULT_BOOK_WEIGHT.
BOOK_WEIGHTS: dict[str, float] = {
    # Sharp-leaning — lowest vig, attract professional money
    "lowvig":         0.9,
    "betonlineag":    0.8,
    "betus":          0.7,
    # Large US retail — very accurate due to scale and competition
    "draftkings":     0.7,
    "fanduel":        0.7,
    "espnbet":        0.65,  # Penn/ESPN Bet, competitive pricing
    # Mid-tier US retail
    "betmgm":         0.55,
    "caesars":        0.55,
    "williamhill_us": 0.55,  # same as Caesars
    "betrivers":      0.5,
    "unibet_us":      0.5,
    "superbook":      0.5,
    "wynnbet":        0.45,
    "betway":         0.45,
    # Offshore / softer lines
    "bovada":         0.4,
    "pointsbet":      0.35,
    "mybookieag":     0.3,
    "barstool":       0.3,   # defunct / rebranded to espnbet
    "fliff":          0.25,
}
DEFAULT_BOOK_WEIGHT: float = 0.5  # fallback for any unlisted book


def american_to_prob(odds: int) -> float:
    """Convert American odds to raw implied probability (includes vig)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        abs_odds = abs(odds)
        return abs_odds / (abs_odds + 100.0)


def prob_to_american(p: float) -> int:
    """Convert probability (0-1) to American odds integer."""
    if p <= 0 or p >= 1:
        raise ValueError(f"Probability must be between 0 and 1, got {p}")
    if p < 0.5:
        return round(100.0 / p - 100.0)
    else:
        return round(-(p * 100.0) / (1.0 - p))


def remove_vig(probs: list[float]) -> list[float]:
    """
    Normalize a list of raw implied probabilities so they sum to 1.
    This removes the bookmaker's vig (overround).

    Example: [0.5263, 0.5263]  →  [0.5, 0.5]
    """
    total = sum(probs)
    if total == 0:
        raise ValueError("Probabilities sum to zero")
    return [p / total for p in probs]


def consensus_stats(
    bookmakers_data: list[dict],
    outcome_name: str,
    market_key: str = "h2h",
    point: float | None = None,
) -> tuple[float | None, int, float]:
    """
    Compute de-vigged consensus probability plus quality metrics.

    Args:
        bookmakers_data — The Odds API bookmakers list for an event
        outcome_name    — The outcome to measure (team name, "Over", "Under",
                          "Draw", "Yes", "No")
        market_key      — Odds API market type: "h2h", "totals", "spreads", "btts"
        point           — For totals/spreads: filter to markets with this line
                          value (within ±0.26 tolerance). None = accept any line.

    Returns:
        (weighted_mean, bookmaker_count, weighted_std_dev)
        weighted_mean   — sharpness-weighted de-vigged probability
                          (None if no book carries it)
        bookmaker_count — number of books with odds for this outcome
        weighted_std_dev— weighted std dev of per-book probs (0 = perfect agreement)

    Note: for "totals" and "spreads", the corresponding "alternate_totals" /
    "alternate_spreads" markets are also checked so that non-main-line Kalshi
    markets (e.g. Over 9.5 when the main line is 8.5) can find consensus.
    """
    # Build the set of Odds API market keys to search
    _ALTERNATE: dict[str, str] = {
        "totals":  "alternate_totals",
        "spreads": "alternate_spreads",
    }
    keys_to_check = {market_key, _ALTERNATE.get(market_key, market_key)}

    weighted_probs: list[tuple[float, float]] = []  # (weight, de_vigged_prob)

    for book in bookmakers_data:
        book_key = book.get("key", "")
        weight = BOOK_WEIGHTS.get(book_key, DEFAULT_BOOK_WEIGHT)

        for market in book.get("markets", []):
            if market.get("key") not in keys_to_check:
                continue
            outcomes = market.get("outcomes", [])

            # For spreads/totals: exact point match + fuzzy name match.
            # _names_match handles city-only ("Pittsburgh" → "Pittsburgh Pirates"),
            # abbreviations ("TB Rays" → "Tampa Bay Rays"), and punctuation
            # differences ("St. Louis" → "St Louis").
            # Using fuzzy name matching is safe here because the point value
            # provides a second filter — two outcomes in the same spread market
            # always have opposite-sign points, so we can't match the wrong team.
            #
            # For H2H (point is None): use exact normalized match to avoid
            # false positives between same-city teams (e.g. Cubs vs White Sox).
            if point is not None:
                target = next(
                    (o for o in outcomes
                     if _names_match(o.get("name", ""), outcome_name)
                     and o.get("point") is not None
                     and abs(float(o["point"]) - point) <= 0.01),
                    None,
                )
            else:
                norm_target = _norm_team(outcome_name)
                target = next(
                    (o for o in outcomes
                     if _norm_team(o.get("name", "")) == norm_target),
                    None,
                )

            if target is None:
                continue

            all_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = remove_vig(all_probs)
            idx = outcomes.index(target)
            weighted_probs.append((weight, no_vig[idx]))

    if not weighted_probs:
        return None, 0, 0.0

    total_weight = sum(w for w, _ in weighted_probs)
    mean = sum(w * p for w, p in weighted_probs) / total_weight
    variance = sum(w * (p - mean) ** 2 for w, p in weighted_probs) / total_weight
    std_dev = variance ** 0.5
    return mean, len(weighted_probs), round(std_dev, 6)


def consensus_probability(bookmakers_data: list[dict], outcome_name: str) -> float | None:
    """
    Given The Odds API bookmakers list for an event, compute the de-vigged
    consensus probability for a specific outcome (team name).

    bookmakers_data format (after normalization in odds_fetcher):
        [
            {
                "name": "fanduel",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Lakers", "price": -150},
                            {"name": "Celtics", "price": +130},
                        ]
                    }
                ]
            },
            ...
        ]

    Returns the average de-vigged probability across all bookmakers that
    have this outcome, or None if no bookmaker carries this outcome.
    """
    de_vigged_probs: list[float] = []

    for book in bookmakers_data:
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            # Find our outcome
            target = next((o for o in outcomes if o["name"] == outcome_name), None)
            if target is None:
                continue
            # All raw probs for this market (to de-vig)
            all_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = remove_vig(all_probs)
            # Index of our target
            idx = outcomes.index(target)
            de_vigged_probs.append(no_vig[idx])

    if not de_vigged_probs:
        return None
    return sum(de_vigged_probs) / len(de_vigged_probs)
