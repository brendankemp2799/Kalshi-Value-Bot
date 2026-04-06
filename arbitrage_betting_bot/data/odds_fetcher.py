"""
Fetches live odds from The Odds API for all configured sports.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date

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
    "americanfootball_nfl":      [(9, 2)],   # Sep – Feb  (wraps Jan)
    "americanfootball_ncaaf":    [(8, 1)],   # Aug – Jan  (wraps Jan)
    "basketball_nba":            [(10, 6)],  # Oct – Jun  (wraps Jan)
    "basketball_ncaab":          [(11, 4)],  # Nov – Apr  (wraps Jan)
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
        remaining = resp.headers.get("x-requests-remaining", "?")
        used = resp.headers.get("x-requests-used", "?")
        logger.debug("Odds API — used: %s, remaining: %s", used, remaining)
        return resp.json()

    def fetch_odds(self, sport: str) -> list[OddsEvent]:
        """Fetch moneyline odds for a sport. Returns a list of OddsEvent objects."""
        try:
            data = self._get(
                f"/sports/{sport}/odds",
                {
                    "regions": config.ODDS_API_REGIONS,
                    "markets": config.ODDS_API_MARKETS,
                    "oddsFormat": config.ODDS_API_ODDS_FORMAT,
                },
            )
        except requests.HTTPError as e:
            logger.error("Odds API HTTP error for %s: %s", sport, e)
            return []
        except requests.RequestException as e:
            logger.error("Odds API request failed for %s: %s", sport, e)
            return []

        events: list[OddsEvent] = []
        for raw in data:
            try:
                events.append(
                    OddsEvent(
                        event_id=raw["id"],
                        sport_key=raw["sport_key"],
                        home_team=raw["home_team"],
                        away_team=raw["away_team"],
                        commence_time=datetime.fromisoformat(
                            raw["commence_time"].replace("Z", "+00:00")
                        ),
                        bookmakers=raw.get("bookmakers", []),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping malformed event: %s", e)

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
            all_events.extend(self.fetch_odds(sport))
            fetched += 1
        return all_events
