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

Supported bet types:
    H2H    — moneyline: home/away winner (or draw for 3-way soccer)
    TOTALS — over/under total score (consensus_stats with market_key="totals")
    SPREAD — point spread cover     (consensus_stats with market_key="spreads")
    BTTS   — both teams score       (consensus_stats with market_key="btts")
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
    HOME  = "home"
    AWAY  = "away"
    DRAW  = "draw"   # 3-way soccer H2H
    OVER  = "over"   # totals: over the line
    UNDER = "under"  # totals: under the line
    COVER = "cover"  # spread: yes_team covers
    BTTS  = "btts"   # both teams to score


@dataclass
class ValueOpportunity:
    matched_event: MatchedEvent
    outcome: Outcome           # which side to bet YES on
    team_name: str             # human-readable label (team, "Over 222.5", etc.)
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


_SERIES_SLUG: dict[str, str] = {
    "KXNFLGAME":    "nfl-game",
    "KXNCAAFGAME":  "ncaaf-game",
    "KXNBAGAME":    "nba-game",
    "KXNCAABGAME":  "ncaab-game",
    "KXMLBGAME":    "mlb-game",
    "KXNHLGAME":    "nhl-game",
    "KXMLSGAME":    "mls-game",
    "KXEPLGAME":    "epl-game",
    "KXUCLGAME":    "uefa-champions-league-game",
    # Totals
    "KXNBATOTAL":   "nba-total",
    "KXMLBTOTAL":   "mlb-total",
    "KXNHLTOTAL":   "nhl-total",
    "KXEPLTOTAL":   "epl-total",
    "KXUCLTOTAL":   "ucl-total",
    "KXMLSTOTAL":   "mls-total",
    # Spreads
    "KXNBASPREAD":  "nba-spread",
    "KXMLBSPREAD":  "mlb-spread",
    "KXNHLSPREAD":  "nhl-spread",
    # BTTS
    "KXMLSBTTS":    "mls-btts",
    "KXEPLBTTS":    "epl-btts",
    "KXUCLBTTS":    "ucl-btts",
}


def _kalshi_url(ticker: str, event_ticker: str = "") -> str:
    event = (event_ticker if event_ticker else ticker).lower()
    series = event.split("-")[0].upper()
    slug = _SERIES_SLUG.get(series, series.lower())
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event}"


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
                "Skip %s vs %s [%s] — spread %.2f > max %.2f (ticker=%s)",
                event.home_team, event.away_team, km.bet_type,
                km.spread, config.MAX_KALSHI_SPREAD, km.ticker,
            )
            continue

        # ── Filter 3: Kalshi volume ───────────────────────────────────────────
        if km.volume < config.MIN_KALSHI_VOLUME:
            logger.debug(
                "Skip %s vs %s [%s] — volume %.0f < min %.0f (ticker=%s)",
                event.home_team, event.away_team, km.bet_type,
                km.volume, config.MIN_KALSHI_VOLUME, km.ticker,
            )
            continue

        logger.debug(
            "Passed filters: %s vs %s [%s] ticker=%s spread=%.2f vol=%.0f",
            event.home_team, event.away_team, km.bet_type,
            km.ticker, km.spread, km.volume,
        )

        # ── Route by bet type ─────────────────────────────────────────────────
        if km.bet_type == "totals":
            _detect_totals(me, event, km, min_edge, opportunities)
        elif km.bet_type == "spread":
            _detect_spread(me, event, km, min_edge, opportunities)
        elif km.bet_type == "btts":
            _detect_btts(me, event, km, min_edge, opportunities)
        elif me.kalshi_outcome == "tie":
            _detect_h2h_tie(me, event, km, min_edge, opportunities)
        else:
            _detect_h2h(me, event, km, min_edge, opportunities)

    logger.info(
        "Found %d value opportunities after filters (min edge %.0f%%)",
        len(opportunities), min_edge * 100,
    )
    return opportunities  # unsorted — main.py sorts by composite score


# ── H2H helpers ───────────────────────────────────────────────────────────────

def _detect_h2h(me, event, km, min_edge, opportunities):
    for outcome, team in [(Outcome.HOME, event.home_team), (Outcome.AWAY, event.away_team)]:
        consensus, book_count, std_dev = consensus_stats(event.bookmakers, team)

        if consensus is None:
            logger.debug("No consensus prob for %s — skipping", team)
            continue

        if book_count < config.MIN_BOOKMAKER_COUNT:
            logger.debug("Skip %s — only %d books (min %d)", team, book_count, config.MIN_BOOKMAKER_COUNT)
            continue

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
                    market_url=_kalshi_url(km.ticker, km.event_ticker),
                    bookmaker_count=book_count,
                    consensus_std=std_dev,
                )
            )
            logger.info(
                "VALUE H2H: %s — edge %.1f%%  (consensus %.1f%% vs price %.1f%%, books=%d, std=%.3f)",
                team, edge * 100, consensus * 100, kalshi_price * 100, book_count, std_dev,
            )


def _detect_h2h_tie(me, event, km, min_edge, opportunities):
    consensus, book_count, std_dev = consensus_stats(event.bookmakers, "Draw")
    if consensus is None:
        logger.debug("No draw consensus for %s vs %s — skipping", event.home_team, event.away_team)
        return
    if book_count < config.MIN_BOOKMAKER_COUNT:
        logger.debug("Skip Draw %s vs %s — only %d books", event.home_team, event.away_team, book_count)
        return
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
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count,
                consensus_std=std_dev,
            )
        )
        logger.info(
            "VALUE DRAW: %s vs %s — edge %.1f%%  (consensus %.1f%% vs price %.1f%%, books=%d)",
            event.home_team, event.away_team,
            edge * 100, consensus * 100, kalshi_price * 100, book_count,
        )


# ── Totals helper ─────────────────────────────────────────────────────────────

def _detect_totals(me, event, km, min_edge, opportunities):
    if km.threshold is None:
        logger.debug("Skip totals %s — no threshold parsed from title: %s", km.ticker, km.title)
        return

    # Direction: "Over" if YES = over, "Under" if YES = under
    direction_label = "Over" if "over" in km.yes_team.lower() else "Under"
    outcome_type = Outcome.OVER if direction_label == "Over" else Outcome.UNDER

    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers,
        direction_label,
        market_key="totals",
        point=km.threshold,
    )

    if consensus is None:
        logger.debug(
            "No totals consensus for %s vs %s (%s %s) — no sportsbook data",
            event.home_team, event.away_team, direction_label, km.threshold,
        )
        return

    if book_count < config.MIN_BOOKMAKER_COUNT:
        logger.debug(
            "Skip totals %s vs %s — only %d books", event.home_team, event.away_team, book_count
        )
        return

    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = consensus - kalshi_price

    if edge >= min_edge:
        label = f"{direction_label} {km.threshold}"
        opportunities.append(
            ValueOpportunity(
                matched_event=me,
                outcome=outcome_type,
                team_name=label,
                consensus_prob=consensus,
                market_price=kalshi_price,
                edge=edge,
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count,
                consensus_std=std_dev,
            )
        )
        logger.info(
            "VALUE TOTALS: %s (%s vs %s) — edge %.1f%%  "
            "(consensus %.1f%% vs price %.1f%%, books=%d)",
            label, event.home_team, event.away_team,
            edge * 100, consensus * 100, kalshi_price * 100, book_count,
        )


# ── Spread helper ─────────────────────────────────────────────────────────────

def _detect_spread(me, event, km, min_edge, opportunities):
    if km.threshold is None:
        logger.debug("Skip spread %s — no threshold parsed from title: %s", km.ticker, km.title)
        return

    # yes_team for spreads is the covering team name
    covering_team = km.yes_team
    if not covering_team:
        logger.debug("Skip spread %s — no yes_team (covering team)", km.ticker)
        return

    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers,
        covering_team,
        market_key="spreads",
        point=km.threshold,
    )

    if consensus is None:
        logger.debug(
            "No spread consensus for %s (%s %s) — no sportsbook data",
            covering_team, km.threshold, km.ticker,
        )
        return

    if book_count < config.MIN_BOOKMAKER_COUNT:
        logger.debug("Skip spread %s — only %d books", km.ticker, book_count)
        return

    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = consensus - kalshi_price

    if edge >= min_edge:
        label = f"{covering_team} {km.threshold:+.1f}"
        opportunities.append(
            ValueOpportunity(
                matched_event=me,
                outcome=Outcome.COVER,
                team_name=label,
                consensus_prob=consensus,
                market_price=kalshi_price,
                edge=edge,
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count,
                consensus_std=std_dev,
            )
        )
        logger.info(
            "VALUE SPREAD: %s (%s vs %s) — edge %.1f%%  "
            "(consensus %.1f%% vs price %.1f%%, books=%d)",
            label, event.home_team, event.away_team,
            edge * 100, consensus * 100, kalshi_price * 100, book_count,
        )


# ── BTTS helper ───────────────────────────────────────────────────────────────

def _detect_btts(me, event, km, min_edge, opportunities):
    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers,
        "Yes",
        market_key="btts",
    )

    if consensus is None:
        logger.debug(
            "No BTTS consensus for %s vs %s — no sportsbook data",
            event.home_team, event.away_team,
        )
        return

    if book_count < config.MIN_BOOKMAKER_COUNT:
        logger.debug(
            "Skip BTTS %s vs %s — only %d books", event.home_team, event.away_team, book_count
        )
        return

    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = consensus - kalshi_price

    if edge >= min_edge:
        opportunities.append(
            ValueOpportunity(
                matched_event=me,
                outcome=Outcome.BTTS,
                team_name="BTTS",
                consensus_prob=consensus,
                market_price=kalshi_price,
                edge=edge,
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count,
                consensus_std=std_dev,
            )
        )
        logger.info(
            "VALUE BTTS: %s vs %s — edge %.1f%%  "
            "(consensus %.1f%% vs price %.1f%%, books=%d)",
            event.home_team, event.away_team,
            edge * 100, consensus * 100, kalshi_price * 100, book_count,
        )
