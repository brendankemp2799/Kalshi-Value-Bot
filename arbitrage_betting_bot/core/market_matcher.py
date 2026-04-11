"""
Fuzzy-matches sportsbook events (from The Odds API) with Kalshi markets.

H2H matching (game-winner):
  Each Kalshi H2H market has a yes_team field — the team that wins if YES
  resolves (e.g. "New York").  We fuzzy-match that team name against each
  sportsbook event's home_team and away_team to:
    1. Find which sportsbook event the Kalshi market covers.
    2. Set kalshi_outcome = "yes" if YES team == home_team,
                            "no"  if YES team == away_team.

Non-H2H matching (totals, spreads):
  These markets list both team names in their title (e.g. "Detroit Pistons vs
  Orlando Magic Over 222.5?").  We extract both team names from the title and
  fuzzy-match them as a pair against sportsbook events.  kalshi_outcome is
  always "yes" for the primary (YES) side; the NO side (Under) is evaluated
  separately in value_detector._detect_totals().
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from data.odds_fetcher import OddsEvent
from data.kalshi_client import KalshiMarket, _SPORT_TO_SERIES
from core.odds_converter import _names_match

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
    kalshi_outcome: str   # "yes", "no", or "tie"


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


def _kalshi_game_date(event_ticker: str) -> datetime | None:
    """
    Parse the game date from a Kalshi event_ticker.
    e.g. "KXMLBGAME-26APR08PITNYY" → datetime(2026, 4, 8, tzinfo=UTC)
    Returns None if parsing fails.
    """
    try:
        parts = event_ticker.split("-")
        if len(parts) < 2:
            return None
        date_seg = parts[1][:7]  # e.g. "26APR08"
        dt = datetime.strptime(date_seg, "%y%b%d")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except ImportError:
    import pytz
    _ET = pytz.timezone("America/New_York")


def _dates_compatible(kalshi_ticker: str, odds_commence: datetime) -> bool:
    """
    Return True only if the Kalshi market's game date exactly matches the
    sportsbook event's date, both expressed in US Eastern time.

    Kalshi tickers encode Eastern dates (e.g. 26APR08 = Apr 8 ET).
    The Odds API commence_time is UTC — a 7 PM PT game on Apr 8 is Apr 9 UTC
    but still Apr 8 ET. Comparing UTC dates would block valid West Coast matches.
    """
    kalshi_date = _kalshi_game_date(kalshi_ticker)
    if kalshi_date is None:
        return True  # can't parse — allow match rather than block it
    odds_date_et = odds_commence.astimezone(_ET).date()
    return kalshi_date.date() == odds_date_et


def _parse_title_teams(title: str) -> tuple[str, str] | None:
    """
    Extract two team names from a Kalshi non-H2H market title.

    Handles formats:
      "Detroit Pistons vs Orlando Magic Over 222.5?"     → ("Detroit Pistons", "Orlando Magic")
      "Los Angeles L at Golden State: Total Points"      → ("Los Angeles L", "Golden State")
      "Bayern Munich vs Barcelona Both Teams Score?"     → ("Bayern Munich", "Barcelona")
      "Detroit Pistons -3.5 at Orlando Magic?"           → ("Detroit Pistons", "Orlando Magic")

    Note: "Team wins by over X.5" (Kalshi spread format) cannot yield two teams
    and returns None — those are matched via event_ticker in the spread path.
    """
    clean = title
    # Strip ": Total[s] [Points/Runs/Goals/...]" suffix (NBA/NHL/soccer totals format)
    clean = re.sub(r":\s*Totals?\b.*$", "", clean, flags=re.IGNORECASE)
    # Strip over/under threshold
    clean = re.sub(r"\b(?:Over|Under)\s+[\d.]+", "", clean, flags=re.IGNORECASE)
    # Strip "Both Teams Score" / "BTTS"
    clean = re.sub(r"\bBoth\s+Teams?\s+Score\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\bBTTS\b", "", clean, flags=re.IGNORECASE)
    # Strip "Winner" / "Total"
    clean = re.sub(r"\b(Winner|Total)\b", "", clean, flags=re.IGNORECASE)
    # Strip spread values: -3.5, +3.5
    clean = re.sub(r"\s[+-]\d+\.?\d*", "", clean)
    # Strip "wins by [more than] X.Y [units]" — Kalshi spread single-team format
    clean = re.sub(r"\s+wins\s+by\s+(?:more\s+than\s+|over\s+)?[\d.]+(?:\s+\w+)?", "", clean, flags=re.IGNORECASE)
    # Strip trailing punctuation and extra whitespace
    clean = re.sub(r"[?!]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()

    for sep in [" vs ", " at ", " @ "]:
        if sep in clean:
            t1, t2 = clean.split(sep, 1)
            t1, t2 = t1.strip(), t2.strip()
            if t1 and t2:
                return t1, t2
    return None


def _sportsbook_lines(event: OddsEvent, market_key: str) -> set[float]:
    """
    Return all point values the sportsbooks carry for a given market type.
    e.g. for market_key="totals" this returns {9.5} if every book has the same
    main line, or {8.5, 9.0, 9.5} if they differ.
    Returns an empty set if no books have this market.
    """
    lines: set[float] = set()
    for book in event.bookmakers:
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            for o in market.get("outcomes", []):
                pt = o.get("point")
                if pt is not None:
                    lines.add(float(pt))
    return lines


def _team_spread_line(event: OddsEvent, team_name: str) -> float | None:
    """
    Return the spread point value a specific team is listed at across sportsbooks.

    Sportsbooks list the favorite at a negative point (e.g. -1.5) and the
    underdog at a positive point (e.g. +1.5). This lets us check whether a
    Kalshi spread market's covering team is actually listed at the threshold
    Kalshi implies (always negative: "wins by over X.5").

    Returns the first matching point value found, or None if the team is not
    found in any spread market.
    """
    for book in event.bookmakers:
        for market in book.get("markets", []):
            if market.get("key") != "spreads":
                continue
            for o in market.get("outcomes", []):
                if _names_match(o.get("name", ""), team_name):
                    pt = o.get("point")
                    if pt is not None:
                        return float(pt)
    return None


def match_events(
    odds_events: list[OddsEvent],
    kalshi_markets: list[KalshiMarket],
) -> list[MatchedEvent]:
    """
    Match each sportsbook event to Kalshi market(s).

    H2H binary markets:
      Each game produces one MatchedEvent.  kalshi_outcome = "yes" if the
      YES team is the home team, "no" if it is the away team.

    3-way soccer H2H markets:
      Each game can produce up to two MatchedEvents:
        1. A team-winner market (kalshi_outcome = "yes" or "no")
        2. A TIE market        (kalshi_outcome = "tie")

    Totals / spreads markets:
      Each market is matched by extracting both team names from the title and
      fuzzy-matching the pair to a sportsbook event.
      kalshi_outcome = "yes" (primary YES side — direction is baked into the market).
      The NO (Under) side of totals markets is evaluated in value_detector.
    """
    threshold = config.FUZZY_MATCH_THRESHOLD

    # Separate markets by bet type
    h2h_tie_markets = [km for km in kalshi_markets if km.bet_type == "h2h" and km.yes_team.lower() == "tie"]
    h2h_team_markets = [km for km in kalshi_markets if km.bet_type == "h2h" and km.yes_team.lower() != "tie"]
    non_h2h_markets = [km for km in kalshi_markets if km.bet_type != "h2h"]

    tie_by_event: dict[str, KalshiMarket] = {km.event_ticker: km for km in h2h_tie_markets}

    results: list[MatchedEvent] = []
    matched_tickers: set[str] = set()

    # ── H2H matching (existing logic) ────────────────────────────────────────
    for event in odds_events:
        allowed_series = set(_SPORT_TO_SERIES.get(event.sport_key, []))

        best_km: KalshiMarket | None = None
        best_outcome: str = "yes"
        best_score: int = 0

        for km in h2h_team_markets:
            event_series = km.event_ticker.split("-")[0] if km.event_ticker else ""
            if allowed_series and event_series not in allowed_series:
                continue
            if km.ticker in matched_tickers:
                continue
            if not km.yes_team:
                continue
            if not _dates_compatible(km.event_ticker, event.commence_time):
                logger.debug(
                    "Skip %s — date mismatch with %s vs %s (%s)",
                    km.ticker, event.home_team, event.away_team,
                    event.commence_time.date(),
                )
                continue

            home_score = _team_score(event.home_team, km.yes_team)
            away_score = _team_score(event.away_team, km.yes_team)

            if home_score >= threshold and home_score > best_score:
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
                "Matched H2H: %s vs %s → Kalshi %s (yes_team=%s, outcome=%s, score=%d)",
                event.home_team, event.away_team,
                best_km.ticker, best_km.yes_team, best_outcome, best_score,
            )

            # For 3-way markets (soccer), also attach the TIE market if one exists
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
                    "Matched TIE: %s vs %s → %s",
                    event.home_team, event.away_team, tie_km.ticker,
                )

    # ── Non-H2H matching (totals / spreads) ──────────────────────────────────
    now_utc = datetime.now(timezone.utc)

    for km in non_h2h_markets:
        if km.ticker in matched_tickers:
            continue

        # Skip markets whose game has already started. Kalshi keeps markets
        # open until settlement (days after the game), but the Odds API drops
        # completed/in-progress events. Without a sportsbook counterpart the
        # matcher can latch onto a wrong same-city game, producing phantom edge.
        game_dt = _kalshi_game_date(km.event_ticker)
        if game_dt is not None and game_dt < now_utc:
            # game_dt is date-only (midnight UTC); add 12 hours so a same-day
            # game isn't skipped until it's clearly in the past.
            if game_dt + timedelta(hours=12) < now_utc:
                logger.debug("Skip %s — game date %s is in the past", km.ticker, game_dt.date())
                continue

        teams = _parse_title_teams(km.title)
        if not teams:
            # NBA/NHL spreads use "Team wins by over X.5 points" — single team.
            # Fall back to matching via yes_team (the covering team) alone, using
            # the same fuzzy logic as H2H but accepting either home or away.
            if km.bet_type == "spread" and km.yes_team:
                teams = None  # handled below in spread-fallback path
            else:
                logger.debug("Skip non-H2H %s — can't parse teams from title: %s", km.ticker, km.title)
                continue

        t1, t2 = teams if teams else (km.yes_team, "")

        # Build the set of allowed series for sport-gating
        km_series = km.event_ticker.split("-")[0].upper() if km.event_ticker else ""

        best_event: OddsEvent | None = None
        best_score: int = 0

        for event in odds_events:
            allowed_series = set(_SPORT_TO_SERIES.get(event.sport_key, []))
            if km_series and km_series not in allowed_series:
                continue
            if not _dates_compatible(km.event_ticker, event.commence_time):
                continue

            if teams:
                # Two-team title: both must match
                s_t1h = _team_score(event.home_team, t1)
                s_t2a = _team_score(event.away_team, t2)
                s_t1a = _team_score(event.away_team, t1)
                s_t2h = _team_score(event.home_team, t2)
                score_fwd = min(s_t1h, s_t2a)
                score_rev = min(s_t1a, s_t2h)
                best_combo = max(score_fwd, score_rev)
            else:
                # Single-team spread (e.g. "LA Lakers wins by over 7.5"):
                # match yes_team against either home or away
                best_combo = max(
                    _team_score(event.home_team, t1),
                    _team_score(event.away_team, t1),
                )

            if best_combo >= threshold and best_combo > best_score:
                best_event = event
                best_score = best_combo

        if best_event:
            # For totals/spreads, only keep Kalshi markets whose threshold
            # matches a line the sportsbooks actually carry.  Without
            # alternate_totals from the Odds API, off-main-line thresholds
            # will always produce consensus=None — no point evaluating them.
            if km.bet_type == "totals" and km.threshold is not None:
                sb_lines = _sportsbook_lines(best_event, "totals")
                if sb_lines:
                    closest = min(sb_lines, key=lambda x: abs(x - km.threshold))
                    if abs(closest - km.threshold) > 0.26:
                        logger.debug(
                            "Skip %s — threshold %.1f not in sportsbook totals lines %s",
                            km.ticker, km.threshold, sorted(sb_lines),
                        )
                        continue

            elif km.bet_type == "spread" and km.threshold is not None and km.yes_team:
                # For spreads, check that the covering team is listed at this
                # threshold on sportsbooks.  Kalshi always uses "Team wins by
                # X.5" (threshold = -X.5) for BOTH teams, but sportsbooks only
                # list the favorite at -X.5 and the underdog at +X.5.
                # If Pittsburgh is the underdog, they're at +1.5 on sportsbooks
                # — searching for Pittsburgh at -1.5 will always fail.
                sb_point = _team_spread_line(best_event, km.yes_team)
                if sb_point is not None and abs(sb_point - km.threshold) > 0.26:
                    logger.debug(
                        "Skip %s — %s is at %.1f on sportsbooks, not %.1f",
                        km.ticker, km.yes_team, sb_point, km.threshold,
                    )
                    continue

            matched_tickers.add(km.ticker)
            results.append(
                MatchedEvent(
                    odds_event=best_event,
                    kalshi_market=km,
                    kalshi_outcome="yes",  # always YES — direction baked into the market
                )
            )
            logger.debug(
                "Matched %s: %s vs %s → Kalshi %s (score=%d, threshold=%s)",
                km.bet_type, best_event.home_team, best_event.away_team,
                km.ticker, best_score, km.threshold,
            )

    h2h_count = sum(1 for r in results if r.kalshi_market.bet_type == "h2h")
    non_h2h_count = len(results) - h2h_count
    matched_event_ids = {r.odds_event.event_id for r in results}
    unmatched = [e for e in odds_events if e.event_id not in matched_event_ids]
    logger.info(
        "Matched %d/%d sportsbook events to Kalshi markets (%d H2H/TIE, %d totals/spread)",
        len(matched_event_ids),
        len(odds_events),
        h2h_count,
        non_h2h_count,
    )
    if unmatched:
        logger.info("Unmatched sportsbook events (%d):", len(unmatched))
        for e in sorted(unmatched, key=lambda x: (x.sport_key, x.commence_time)):
            logger.info(
                "  [%s] %s vs %s  (%s)",
                e.sport_key.split("_")[-1].upper(),
                e.home_team,
                e.away_team,
                e.commence_time.strftime("%a %b %d %H:%M UTC"),
            )
    return results
