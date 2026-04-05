"""
Utility functions for converting between odds formats and implied probabilities.

American odds examples:
  +150  →  40.0%  implied probability
  -150  →  60.0%  implied probability
"""
from __future__ import annotations


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
    bookmakers_data: list[dict], outcome_name: str
) -> tuple[float | None, int, float]:
    """
    Compute de-vigged consensus probability plus quality metrics.

    Returns:
        (mean_prob, bookmaker_count, std_dev)
        mean_prob       — average de-vigged probability across all covering books
                          (None if no book carries this outcome)
        bookmaker_count — number of books that have odds for this outcome
        std_dev         — standard deviation of per-book de-vigged probs
                          (0.0 = perfect agreement, higher = books disagree)
    """
    de_vigged_probs: list[float] = []

    for book in bookmakers_data:
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            target = next((o for o in outcomes if o["name"] == outcome_name), None)
            if target is None:
                continue
            all_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = remove_vig(all_probs)
            idx = outcomes.index(target)
            de_vigged_probs.append(no_vig[idx])

    if not de_vigged_probs:
        return None, 0, 0.0

    mean = sum(de_vigged_probs) / len(de_vigged_probs)
    variance = sum((p - mean) ** 2 for p in de_vigged_probs) / len(de_vigged_probs)
    std_dev = variance ** 0.5
    return mean, len(de_vigged_probs), round(std_dev, 6)


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
