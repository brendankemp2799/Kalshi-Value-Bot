"""
Detects value opportunities by comparing Kalshi prices to the de-vigged
consensus from traditional sportsbooks (via The Odds API).

A "value" opportunity exists when Kalshi offers a higher implied probability
than the de-vigged sportsbook consensus — the market is UNDERPRICED relative
to sharp money.

Edge formula:
    edge = sportsbook_consensus_prob - kalshi_price

Positive edge means Kalshi is cheaper (better odds) than what sportsbooks
imply — you're getting more than fair value.

Hard quality filters (applied before any opportunity is surfaced):
    1. Bookmaker count   ≥ MIN_BOOKMAKER_COUNT  (consensus reliability)
    2. Kalshi spread     ≤ MAX_KALSHI_SPREAD     (price is fillable)
    3. Kalshi volume     ≥ MIN_KALSHI_VOLUME     (liquid market)

Surviving opportunities are returned unsorted — composite scoring and final
sort happen in main.py after Kelly sizing is calculated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.market_matcher import MatchedEvent
from core.odds_converter import consensus_stats

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    HOME = "home"
    AWAY = "away"
    DRAW = "draw"   # 3-way markets (MLS) only


@dataclass
class ValueOpportunity:
    matched_event: MatchedEvent
    outcome: Outcome           # which team/side to bet YES on
    team_name: str             # human-readable team name
    consensus_prob: float      # de-vigged sportsbook estimate
    market_price: float        # current Kalshi price (0-1)
    edge: float                # consensus_prob - market_price (positive = value)
    market_url: str            # direct link to the Kalshi market
    bookmaker_count: int       # number of books covering this outcome
    consensus_std: float       # std dev of de-vigged probs (0 = perfect agreement)

    @property
    def edge_pct(self) -> str:
        return f"{self.edge * 100:.1f}%"

    @property
    def market_odds_american(self) -> int:
        """Convert market price back to American odds for readability."""
        from core.odds_converter import prob_to_american
        return prob_to_american(self.market_price)


def _kalshi_url(ticker: str) -> str:
    return f"https://kalshi.com/markets/{ticker}"


def detect_value(
    matched_events: list[MatchedEvent],
    min_edge: float = config.MIN_EDGE_THRESHOLD,
) -> list[ValueOpportunity]:
    """
    Scan all matched events for value opportunities on Kalshi.

    Applies hard quality filters and returns surviving opportunities
    unsorted (sorting by composite score happens in main.py after Kelly).
    """
    opportunities: list[ValueOpportunity] = []

    for me in matched_events:
        event = me.odds_event
        km = me.kalshi_market

        # ── Filter 2: Kalshi spread ───────────────────────────────────────────
        if km.spread > config.MAX_KALSHI_SPREAD:
            logger.debug(
                "Skip %s vs %s — Kalshi spread %.2f > max %.2f",
                event.home_team, event.away_team, km.spread, config.MAX_KALSHI_SPREAD,
            )
            continue

        # ── Filter 3: Kalshi volume ───────────────────────────────────────────
        if km.volume < config.MIN_KALSHI_VOLUME:
            logger.debug(
                "Skip %s vs %s — Kalshi volume $%.0f < min $%.0f",
                event.home_team, event.away_team, km.volume, config.MIN_KALSHI_VOLUME,
            )
            continue

        # ── 3-way TIE market (MLS) ───────────────────────────────────────────
        if me.kalshi_outcome == "tie":
            consensus, book_count, std_dev = consensus_stats(event.bookmakers, "Draw")
            if consensus is None:
                logger.debug("No draw consensus for %s vs %s — skipping", event.home_team, event.away_team)
                continue
            if book_count < config.MIN_BOOKMAKER_COUNT:
                logger.debug("Skip Draw %s vs %s — only %d books", event.home_team, event.away_team, book_count)
                continue
            # Use ask price — that's what we pay to fill a YES order
            kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
            edge = consensus - kalshi_price
            if edge >= min_edge:
                opportunities.append(
                    ValueOpportunity(
                        matched_event=me,
                        outcome=Outcome.DRAW,
                        team_name="Draw",
                        consensus_prob=consensus,
                        market_price=kalshi_price,
                        edge=edge,
                        market_url=_kalshi_url(km.ticker),
                        bookmaker_count=book_count,
                        consensus_std=std_dev,
                    )
                )
                logger.info(
                    "VALUE: Draw (%s vs %s) on Kalshi — edge %.1f%%  "
                    "(consensus %.1f%% vs price %.1f%%, books=%d)",
                    event.home_team, event.away_team,
                    edge * 100, consensus * 100, kalshi_price * 100, book_count,
                )
            continue  # done with this TIE MatchedEvent

        # ── Binary market (home / away) ───────────────────────────────────────
        for outcome, team in [(Outcome.HOME, event.home_team), (Outcome.AWAY, event.away_team)]:
            consensus, book_count, std_dev = consensus_stats(event.bookmakers, team)

            if consensus is None:
                logger.debug("No consensus prob for %s — skipping", team)
                continue

            # ── Filter 1: bookmaker count ─────────────────────────────────────
            if book_count < config.MIN_BOOKMAKER_COUNT:
                logger.debug(
                    "Skip %s — only %d books (min %d)",
                    team, book_count, config.MIN_BOOKMAKER_COUNT,
                )
                continue

            # Determine the fill price for this outcome.
            # We always BUY the contract, so we pay the ASK price:
            #   Buying YES: pay yes_ask
            #   Buying NO:  pay no_ask = 1 - yes_bid  (binary market identity)
            if outcome == Outcome.HOME:
                if me.kalshi_outcome == "yes":
                    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
                else:
                    kalshi_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else km.no_price
            else:
                if me.kalshi_outcome == "yes":
                    kalshi_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else km.no_price
                else:
                    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price

            edge = consensus - kalshi_price
            if edge >= min_edge:
                opportunities.append(
                    ValueOpportunity(
                        matched_event=me,
                        outcome=outcome,
                        team_name=team,
                        consensus_prob=consensus,
                        market_price=kalshi_price,
                        edge=edge,
                        market_url=_kalshi_url(km.ticker),
                        bookmaker_count=book_count,
                        consensus_std=std_dev,
                    )
                )
                logger.info(
                    "VALUE: %s on Kalshi — edge %.1f%%  "
                    "(consensus %.1f%% vs price %.1f%%, books=%d, std=%.3f)",
                    team, edge * 100, consensus * 100, kalshi_price * 100,
                    book_count, std_dev,
                )

    logger.info(
        "Found %d value opportunities after filters (min edge %.0f%%)",
        len(opportunities), min_edge * 100,
    )
    return opportunities  # unsorted — main.py sorts by composite score
