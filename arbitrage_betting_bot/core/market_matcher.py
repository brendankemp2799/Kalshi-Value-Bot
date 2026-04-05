"""
Fuzzy-matches sportsbook events (from The Odds API) with Kalshi game-winner
markets.

Each Kalshi game market has a yes_team field — the team that wins if YES
resolves (e.g. "New York").  We fuzzy-match that team name against each
sportsbook event's home_team and away_team to:
  1. Find which sportsbook event the Kalshi market covers.
  2. Set kalshi_outcome = "yes" if YES team == home_team,
                          "no"  if YES team == away_team.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from rapidfuzz import fuzz

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from data.odds_fetcher import OddsEvent
from data.kalshi_client import KalshiMarket, _SPORT_TO_SERIES

logger = logging.getLogger(__name__)

# Known abbreviation expansions to improve matching
TEAM_ALIASES: dict[str, str] = {
    "LA":  "Los Angeles",
    "NY":  "New York",
    "GS":  "Golden State",
    "KC":  "Kansas City",
    "TB":  "Tampa Bay",
    "SF":  "San Francisco",
    "NE":  "New England",
    "NO":  "New Orleans",
    "OKC": "Oklahoma City",
}


@dataclass
class MatchedEvent:
    odds_event: OddsEvent
    kalshi_market: KalshiMarket
    kalshi_outcome: str   # "yes" or "no" — which side pays if home_team wins


def _normalize(name: str) -> str:
    for abbr, full in TEAM_ALIASES.items():
        name = name.replace(abbr, full)
    return name.lower().strip()


def _team_score(team: str, candidate: str) -> int:
    """Best rapidfuzz score between a sportsbook team name and a Kalshi team name."""
    t = _normalize(team)
    c = _normalize(candidate)
    return max(
        fuzz.partial_ratio(t, c),
        fuzz.token_sort_ratio(t, c),
        fuzz.token_set_ratio(t, c),
    )


def match_events(
    odds_events: list[OddsEvent],
    kalshi_markets: list[KalshiMarket],
) -> list[MatchedEvent]:
    """
    Match each sportsbook event to Kalshi game-winner market(s).

    For binary sports (NBA, MLB, NHL, NFL …):
      Each game produces one MatchedEvent.  kalshi_outcome = "yes" if the
      YES team is the home team, "no" if it is the away team.

    For 3-way sports (MLS):
      Each game produces up to two MatchedEvents:
        1. A team-winner market (kalshi_outcome = "yes" or "no")
        2. A TIE market      (kalshi_outcome = "tie")
      Both are matched via the shared event_ticker.
    """
    threshold = config.FUZZY_MATCH_THRESHOLD

    # Separate TIE markets so we can look them up by event_ticker
    tie_by_event: dict[str, KalshiMarket] = {
        km.event_ticker: km
        for km in kalshi_markets
        if km.yes_team.lower() == "tie"
    }
    team_markets = [km for km in kalshi_markets if km.yes_team.lower() != "tie"]

    # Build a lookup: Kalshi series ticker → set of market tickers in that series
    # e.g. "KXNBAGAME" → all NBA market tickers
    series_to_tickers: dict[str, set[str]] = {}
    for km in team_markets:
        # Extract series prefix from ticker (everything before the date segment)
        series = km.event_ticker.split("-")[0] if km.event_ticker else ""
        series_to_tickers.setdefault(series, set()).add(km.ticker)

    results: list[MatchedEvent] = []
    matched_tickers: set[str] = set()

    for event in odds_events:
        # Restrict candidate markets to the correct sport's series
        allowed_series = _SPORT_TO_SERIES.get(event.sport_key, "")

        best_km: KalshiMarket | None = None
        best_outcome: str = "yes"
        best_score: int = 0

        for km in team_markets:
            # Hard sport-level gate — never match across sports
            event_series = km.event_ticker.split("-")[0] if km.event_ticker else ""
            if allowed_series and event_series != allowed_series:
                continue
            if km.ticker in matched_tickers:
                continue
            if not km.yes_team:
                continue

            home_score = _team_score(event.home_team, km.yes_team)
            away_score = _team_score(event.away_team, km.yes_team)

            if home_score >= threshold and home_score > best_score:
                # Also verify the away team matches the other Kalshi team
                if km.no_team:
                    cross = _team_score(event.away_team, km.no_team)
                    if cross < threshold:
                        logger.debug(
                            "Skip %s — away %s doesn't match no_team %s (score %d)",
                            km.ticker, event.away_team, km.no_team, cross,
                        )
                        continue
                best_km = km
                best_outcome = "yes"
                best_score = home_score

            if away_score >= threshold and away_score > best_score:
                # Also verify the home team matches the other Kalshi team
                if km.no_team:
                    cross = _team_score(event.home_team, km.no_team)
                    if cross < threshold:
                        logger.debug(
                            "Skip %s — home %s doesn't match no_team %s (score %d)",
                            km.ticker, event.home_team, km.no_team, cross,
                        )
                        continue
                best_km = km
                best_outcome = "no"
                best_score = away_score

        if best_km:
            matched_tickers.add(best_km.ticker)
            results.append(
                MatchedEvent(
                    odds_event=event,
                    kalshi_market=best_km,
                    kalshi_outcome=best_outcome,
                )
            )
            logger.debug(
                "Matched: %s vs %s → Kalshi %s (yes_team=%s, outcome=%s, score=%d)",
                event.home_team, event.away_team,
                best_km.ticker, best_km.yes_team, best_outcome, best_score,
            )

            # For 3-way markets (MLS), also attach the TIE market if one exists
            tie_km = tie_by_event.get(best_km.event_ticker)
            if tie_km and tie_km.ticker not in matched_tickers:
                matched_tickers.add(tie_km.ticker)
                results.append(
                    MatchedEvent(
                        odds_event=event,
                        kalshi_market=tie_km,
                        kalshi_outcome="tie",
                    )
                )
                logger.debug(
                    "Matched TIE market: %s vs %s → %s",
                    event.home_team, event.away_team, tie_km.ticker,
                )

    logger.info(
        "Matched %d/%d sportsbook events to Kalshi markets",
        len(results), len(odds_events),
    )
    return results
