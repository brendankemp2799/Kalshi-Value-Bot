"""
Fetches live odds from The Odds API for all configured sports.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

# Active months for each sport (inclusive).
# Months outside this range return zero events from The Odds API — querying
# them wastes credits. Each tuple is (start_month, end_month); ranges that
# wrap across January use two tuples.
_SPORT_SEASONS: dict[str, list[tuple[int, int]]] = {
    "basketball_nba":            [(10, 6)],  # Oct – Jun  (wraps Jan)
    "baseball_mlb":              [(3, 10)],  # Mar – Oct
    "icehockey_nhl":             [(10, 6)],  # Oct – Jun  (wraps Jan)
    "soccer_usa_mls":            [(2, 11)],  # Feb – Nov
    "soccer_epl":                [(8, 5)],   # Aug – May  (wraps Jan)
    "soccer_uefa_champs_league": [(9, 5)],   # Sep – May  (wraps Jan)
}


def _in_season(sport: str, today: date | None = None) -> bool:
    """Return True if the sport is currently in season."""
    today = today or date.today()
    ranges = _SPORT_SEASONS.get(sport)
    if not ranges:
        return True  # unknown sport — query it anyway
    m = today.month
    for start, end in ranges:
        if start <= end:          # e.g. Mar–Oct: no wrap
            if start <= m <= end:
                return True
        else:                     # e.g. Sep–Feb: wraps across January
            if m >= start or m <= end:
                return True
    return False


@dataclass
class OddsEvent:
    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    bookmakers: list[dict] = field(default_factory=list)


class OddsAPIClient:
    def __init__(self, api_key: str = config.ODDS_API_KEY):
        if not api_key:
            raise ValueError("ODDS_API_KEY is not set. Check your .env file.")
        self.api_key = api_key
        self.base_url = config.ODDS_API_BASE_URL
        self.session = requests.Session()

    def _get(self, path: str, params: dict) -> dict | list:
        params["apiKey"] = self.api_key
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        used = resp.headers.get("x-requests-used")
        logger.debug("Odds API — used: %s, remaining: %s", used, remaining)
        # Persist latest credit snapshot so dashboard can display it
        if remaining is not None or used is not None:
            try:
                from storage.db import update_api_credits
                update_api_credits(
                    used=int(used) if used is not None else None,
                    remaining=int(remaining) if remaining is not None else None,
                )
            except Exception:
                pass  # never crash a fetch due to credit tracking
        return resp.json()

    def _fetch_raw(self, sport: str, markets: str) -> list[dict]:
        """
        Single Odds API request. Returns raw event list or [] on error.
        The Odds API rejects alternate_* markets when combined with standard
        markets in the same call (422), so callers must split them.
        """
        try:
            return self._get(
                f"/sports/{sport}/odds",
                {
                    "regions": config.ODDS_API_REGIONS,
                    "markets": markets,
                    "oddsFormat": config.ODDS_API_ODDS_FORMAT,
                },
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            logger.error("Odds API HTTP %s for %s markets=%s", status, sport, markets)
            return []
        except requests.RequestException as e:
            logger.error("Odds API request failed for %s: %s", sport, e)
            return []

    def fetch_odds(self, sport: str, markets: str = config.ODDS_API_MARKETS) -> list[OddsEvent]:
        """
        Fetch odds for a sport. markets is a comma-separated list of market types.

        The Odds API requires alternate_totals / alternate_spreads to be fetched
        in a separate call from standard markets. This method splits the markets
        list automatically, makes up to two calls, and merges the bookmaker data
        by event_id before returning.
        """
        market_list = [m.strip() for m in markets.split(",") if m.strip()]
        alternate_keys = {"alternate_totals", "alternate_spreads"}
        main_markets   = [m for m in market_list if m not in alternate_keys]
        alt_markets    = [m for m in market_list if m in alternate_keys]

        # First call: main markets
        raw_main = self._fetch_raw(sport, ",".join(main_markets)) if main_markets else []

        # Second call: alternate markets (separate request required by API)
        raw_alt: list[dict] = []
        if alt_markets:
            time.sleep(0.5)  # small gap to avoid rate-limit
            raw_alt = self._fetch_raw(sport, ",".join(alt_markets))

        # Index alternate bookmaker data by event_id for merging
        alt_by_event: dict[str, dict[str, list[dict]]] = {}  # event_id → book_name → markets
        for raw in raw_alt:
            eid = raw.get("id", "")
            for book in raw.get("bookmakers", []):
                bname = book.get("key", book.get("title", ""))
                alt_by_event.setdefault(eid, {}).setdefault(bname, []).extend(
                    book.get("markets", [])
                )

        now = datetime.now(timezone.utc)
        events: list[OddsEvent] = []
        skipped_live = 0

        for raw in raw_main:
            try:
                commence = datetime.fromisoformat(
                    raw["commence_time"].replace("Z", "+00:00")
                )
                if commence <= now:
                    skipped_live += 1
                    logger.debug(
                        "Skipping in-progress/past event: %s vs %s (%s)",
                        raw.get("home_team"), raw.get("away_team"), commence,
                    )
                    continue

                # Merge alternate markets into each bookmaker's market list
                eid = raw["id"]
                bookmakers = raw.get("bookmakers", [])
                if eid in alt_by_event:
                    alt_books = alt_by_event[eid]
                    merged = []
                    for book in bookmakers:
                        bname = book.get("key", book.get("title", ""))
                        extra = alt_books.get(bname, [])
                        if extra:
                            book = dict(book)
                            book["markets"] = list(book.get("markets", [])) + extra
                        merged.append(book)
                    bookmakers = merged

                events.append(
                    OddsEvent(
                        event_id=eid,
                        sport_key=raw["sport_key"],
                        home_team=raw["home_team"],
                        away_team=raw["away_team"],
                        commence_time=commence,
                        bookmakers=bookmakers,
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed event: %s", e)

        if skipped_live:
            logger.info(
                "Fetched %d upcoming events for %s (%d in-progress/past skipped)",
                len(events), sport, skipped_live,
            )
        else:
            logger.info("Fetched %d events for %s", len(events), sport)
        return events

    def fetch_all_sports(self) -> list[OddsEvent]:
        """Fetch odds for every sport in config.SPORTS that is currently in season."""
        all_events: list[OddsEvent] = []
        fetched = 0
        for sport in config.SPORTS:
            if not _in_season(sport):
                logger.debug("Skipping %s — off season", sport)
                continue
            if fetched > 0:
                time.sleep(1)   # avoid 429 rate-limit between sport requests
            markets = config.SPORT_MARKETS.get(sport, config.ODDS_API_MARKETS)
            all_events.extend(self.fetch_odds(sport, markets=markets))
            fetched += 1
        return all_events
