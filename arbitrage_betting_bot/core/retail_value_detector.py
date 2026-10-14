"""
Detects value bets at legal retail sportsbooks (DraftKings, FanDuel, BetMGM,
Caesars, Fanatics) by comparing each book's own posted price against a sharp-
book (Pinnacle) de-vigged fair probability -- the same underlying comparison
core/value_detector.py made against Kalshi, but against a retail book's own
price instead of an exchange's, with no bid/ask spread or exchange fee to
account for (fee_rate=0.0 everywhere this feeds core/kelly_calculator.py).

Unlike the Kalshi path, there is no second venue to MATCH against: sharp and
retail prices both live in the same OddsEvent.bookmakers list (one Odds API
call), already structured (market.key, market.point, outcome.name/price) --
core/market_matcher.py's title-parsing has no role here.

Edge is deliberately measured against the retail book's RAW (vig-included)
implied probability, not a de-vigged one: the vig is the actual price you are
offered, so it is what your fair-probability estimate has to clear.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from data.odds_fetcher import OddsEvent
from core.odds_converter import consensus_stats, american_to_prob, _norm_team, _names_match

logger = logging.getLogger(__name__)


@dataclass
class RetailOpportunity:
    odds_api_event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    bet_type: str             # "h2h" | "totals" | "spread"
    outcome_label: str        # display string, e.g. "Lakers ML", "Over 220.5", "Celtics -4.5"
    side: str                 # team name | "Draw" | "over" | "under" -- settlement-matching key
    threshold: float | None   # totals/spread line; None for h2h
    book_key: str
    book_odds_american: int
    book_price_prob: float    # american_to_prob(book_odds_american) -- vig-inclusive
    consensus_prob: float     # de-vigged sharp-book fair probability
    edge: float                # consensus_prob - book_price_prob
    bookmaker_count: int       # sharp books backing consensus_prob
    consensus_std: float


def _sharp_only(bookmakers: list[dict]) -> list[dict]:
    """Keep only config.SHARP_BOOKMAKERS. consensus_stats() blends every book
    it's handed by weight -- a retail book must never be in this list, or the
    'fair' price it gets compared against would be partly itself."""
    return [b for b in bookmakers if b.get("key") in config.SHARP_BOOKMAKERS]


def _retail_price(
    bookmakers: list[dict], book_key: str, market_key: str,
    outcome_name: str, point: float | None,
) -> int | None:
    """This book's own posted American odds for one outcome, or None if it
    doesn't carry this market/line. Deliberately not de-vigged -- see module
    docstring on why edge is measured against the raw price."""
    for book in bookmakers:
        if book.get("key") != book_key:
            continue
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            for o in market.get("outcomes", []):
                if point is not None:
                    if not _names_match(o.get("name", ""), outcome_name):
                        continue
                    if o.get("point") is None or abs(float(o["point"]) - point) > 0.01:
                        continue
                else:
                    if _norm_team(o.get("name", "")) != _norm_team(outcome_name):
                        continue
                try:
                    return int(o["price"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def _best_retail(
    event: OddsEvent, sharp_books: list[dict], market_key: str,
    outcome_name: str, side: str, threshold: float | None, outcome_label: str,
    min_edge: float,
) -> RetailOpportunity | None:
    """Price `outcome_name` against every configured retail book, keep the
    single best-edge one. Returns None if nothing clears min_edge."""
    consensus_prob, book_count, std = consensus_stats(
        sharp_books, outcome_name, market_key=market_key, point=threshold,
    )
    if consensus_prob is None:
        return None

    best: RetailOpportunity | None = None
    for book_key in config.RETAIL_BOOKMAKERS:
        price = _retail_price(event.bookmakers, book_key, market_key, outcome_name, threshold)
        if price is None:
            continue
        book_prob = american_to_prob(price)
        edge = consensus_prob - book_prob
        if edge < min_edge:
            continue
        if best is None or edge > best.edge:
            best = RetailOpportunity(
                odds_api_event_id=event.event_id,
                sport_key=event.sport_key,
                home_team=event.home_team,
                away_team=event.away_team,
                commence_time=event.commence_time,
                bet_type="spread" if market_key == "spreads" else market_key,
                outcome_label=outcome_label,
                side=side,
                threshold=threshold,
                book_key=book_key,
                book_odds_american=price,
                book_price_prob=book_prob,
                consensus_prob=consensus_prob,
                edge=edge,
                bookmaker_count=book_count,
                consensus_std=std,
            )
    return best


def _h2h_outcome_names(sharp_books: list[dict]) -> set[str]:
    """Outcome names actually offered by the sharp book's own h2h market --
    home/away, plus "Draw" for soccer's 3-way market. Reading them from the
    sharp book itself (rather than assuming home/away) means 3-way markets
    need no special-casing."""
    names: set[str] = set()
    for book in sharp_books:
        for market in book.get("markets", []):
            if market.get("key") == "h2h":
                for o in market.get("outcomes", []):
                    if o.get("name"):
                        names.add(o["name"])
    return names


def _totals_points(event: OddsEvent) -> set[float]:
    """Distinct totals lines actually offered by any configured retail book for
    this event -- there's no Kalshi strike forcing a specific number anymore,
    so price whatever's posted rather than a single 'featured' line."""
    return {
        float(o["point"])
        for book in event.bookmakers if book.get("key") in config.RETAIL_BOOKMAKERS
        for market in book.get("markets", []) if market.get("key") == "totals"
        for o in market.get("outcomes", []) if o.get("point") is not None
    }


def _spread_points(event: OddsEvent) -> dict[str, set[float]]:
    """team name -> distinct spread points any configured retail book offers
    for that team, for this event."""
    out: dict[str, set[float]] = {}
    for book in event.bookmakers:
        if book.get("key") not in config.RETAIL_BOOKMAKERS:
            continue
        for market in book.get("markets", []):
            if market.get("key") != "spreads":
                continue
            for o in market.get("outcomes", []):
                if o.get("point") is None:
                    continue
                if _names_match(o.get("name", ""), event.home_team):
                    team = event.home_team
                elif _names_match(o.get("name", ""), event.away_team):
                    team = event.away_team
                else:
                    continue
                out.setdefault(team, set()).add(float(o["point"]))
    return out


def detect_retail_value(
    events: list[OddsEvent],
    min_edge: float = config.MIN_EDGE,
) -> list[RetailOpportunity]:
    """Scan every event for a best-book value bet on h2h, totals, and spreads.
    At most one RetailOpportunity per (event, outcome) -- the single best-edge
    retail book for that outcome, not every book that happens to clear min_edge."""
    opportunities: list[RetailOpportunity] = []

    for event in events:
        sharp_books = _sharp_only(event.bookmakers)
        if not sharp_books:
            continue

        # ── h2h (including soccer's 3-way Draw) ────────────────────────────
        for outcome_name in _h2h_outcome_names(sharp_books):
            opp = _best_retail(
                event, sharp_books, "h2h", outcome_name, outcome_name, None,
                outcome_label=f"{outcome_name} ML", min_edge=min_edge,
            )
            if opp:
                opportunities.append(opp)

        # ── totals ───────────────────────────────────────────────────────────
        for point in _totals_points(event):
            for name, side in (("Over", "over"), ("Under", "under")):
                opp = _best_retail(
                    event, sharp_books, "totals", name, side, point,
                    outcome_label=f"{name} {point:g}", min_edge=min_edge,
                )
                if opp:
                    opportunities.append(opp)

        # ── spreads ──────────────────────────────────────────────────────────
        for team, points in _spread_points(event).items():
            for point in points:
                opp = _best_retail(
                    event, sharp_books, "spreads", team, team, point,
                    outcome_label=f"{team} {point:+g}", min_edge=min_edge,
                )
                if opp:
                    opportunities.append(opp)

    return opportunities
