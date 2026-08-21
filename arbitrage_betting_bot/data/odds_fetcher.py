"""
Fetches live odds from The Odds API for all configured sports.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

# When each event's alternate-line ladder was last bought. Alternates bill per game
# per refresh, so this is what stops a 45-minute scan loop from re-buying the same
# ladder every cycle. In memory by design -- a restart re-fetches (a few credits),
# which is the safe direction, since a stale ladder would misprice silently.
_ALT_FETCHED_AT: dict[str, datetime] = {}

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
    "americanfootball_nfl":      [(8, 2)],   # Aug (preseason) – Feb  (wraps Jan)
    "soccer_spain_la_liga":      [(8, 5)],   # Aug – May  (wraps Jan)
    "soccer_italy_serie_a":      [(8, 5)],   # Aug – May  (wraps Jan)
    "soccer_france_ligue_one":   [(8, 5)],   # Aug – May  (wraps Jan)
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

    @staticmethod
    def _scope_params() -> dict:
        """
        Whichever of `bookmakers` / `regions` we are billing against.

        Cost is (markets x units). `regions` charges 1 unit per region; `bookmakers`
        charges 1 unit per 10 books. Naming <=10 books is therefore half the price of
        regions=us,eu AND keeps Pinnacle, which regions=us alone would lose. See the
        ODDS_API_BOOKMAKERS block in config.py for the measurements.
        """
        books = (config.ODDS_API_BOOKMAKERS or "").strip()
        if books:
            return {"bookmakers": books}
        return {"regions": config.ODDS_API_REGIONS}

    @staticmethod
    def _window_params() -> dict:
        """Ask the API for only the games we would actually bet.

        Doesn't change the price of a bulk call (cost ignores event count) but keeps
        payloads small, and matters directly for the per-event alternate fetches,
        which DO bill per game.
        """
        hours = getattr(config, "MAX_TIME_TO_EVENT_HOURS", 0)
        if not hours:
            return {}
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return {
            "commenceTimeFrom": now.strftime(fmt),
            "commenceTimeTo": (now + timedelta(hours=hours)).strftime(fmt),
        }

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
                    **self._scope_params(),
                    **self._window_params(),
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

    def fetch_historical_odds(self, sport: str, date_iso: str, markets: str) -> list[dict]:
        """
        Raw odds snapshot for a sport as of the given UTC timestamp (the API
        returns the last available snapshot at-or-before `date_iso`). Used for
        closing-line lookups — costs real credits (~10/call), unlike live odds
        polling, so callers must not invoke this on a tight loop.

        Returns the raw event list (bookmakers in the same shape as live odds —
        feed directly into core.odds_converter.consensus_stats()), or [] on error.
        """
        try:
            resp = self._get(
                f"/historical/sports/{sport}/odds",
                {
                    "regions": config.ODDS_API_REGIONS,
                    "markets": markets,
                    "oddsFormat": config.ODDS_API_ODDS_FORMAT,
                    "date": date_iso,
                },
            )
            return resp.get("data", [])
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            logger.warning("Odds API historical HTTP %s for %s @ %s", status, sport, date_iso)
            return []
        except requests.RequestException as e:
            logger.warning("Odds API historical request failed for %s: %s", sport, e)
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
        _MAX_TTE_HOURS = getattr(config, "MAX_TIME_TO_EVENT_HOURS", 0)
        events: list[OddsEvent] = []
        skipped_live = 0
        skipped_far = 0

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

                # Enforce the horizon locally too. commenceTimeTo already asks the API
                # to filter, but this is the guarantee: it holds for cached responses,
                # for callers that bypass _window_params, and if the parameter is ever
                # silently ignored. Orders placed >48h out filled 7.6% of the time.
                if _MAX_TTE_HOURS and commence > now + timedelta(hours=_MAX_TTE_HOURS):
                    skipped_far += 1
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

        if skipped_live or skipped_far:
            logger.info(
                "Fetched %d events for %s (%d in-progress/past, %d beyond %dh skipped)",
                len(events), sport, skipped_live, skipped_far, _MAX_TTE_HOURS,
            )
        else:
            logger.info("Fetched %d events for %s", len(events), sport)
        return events

    def fetch_event_alternates(self, sport: str, event_id: str,
                               markets: str = "alternate_totals") -> list[dict]:
        """
        The full line ladder for ONE game, from the per-event endpoint.

        This is the only way to get alternate lines: the bulk /odds endpoint returns
        422 for alternate_* markets (measured 2026-08-20), and its standard `totals`
        market returns just one featured line per book. Books disagree about what that
        featured line is -- for one MLB game, 18 books showed 9.0 and 4 showed 9.5 --
        so whether any of them happened to match Kalshi's strike was luck. It usually
        wasn't: 20 of 29 totals candidates we would have bet had NO book quoting
        Kalshi's number, even across 31 books.

        Costs 1 credit per call with <=10 bookmakers (markets x units = 1 x 1).
        Returns the raw bookmakers list, or [] on error -- a failed enrichment must
        degrade to "no alternates for this game", never break the scan.
        """
        try:
            resp = self._get(
                f"/sports/{sport}/events/{event_id}/odds",
                {
                    **self._scope_params(),
                    "markets": markets,
                    "oddsFormat": config.ODDS_API_ODDS_FORMAT,
                },
            )
            return (resp or {}).get("bookmakers", []) or []
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            # 422 = this sport/market combination carries no alternates; normal, not a fault.
            log = logger.debug if status == 422 else logger.warning
            log("Alternates HTTP %s for %s/%s", status, sport, event_id)
            return []
        except requests.RequestException as e:
            logger.warning("Alternates request failed for %s/%s: %s", sport, event_id, e)
            return []

    @staticmethod
    def _merge_bookmakers(base: list[dict], extra: list[dict]) -> list[dict]:
        """
        Fold per-event alternate markets into an event's existing bookmaker list.

        Alternates keep their own market key (`alternate_totals`), which
        core.odds_converter.consensus_stats() already searches alongside `totals` --
        so nothing downstream needs to change. A book present only in the alternates
        response is appended rather than dropped.
        """
        by_key = {b.get("key", b.get("title", "")): dict(b) for b in base}
        for b in extra:
            k = b.get("key", b.get("title", ""))
            if k in by_key:
                merged = dict(by_key[k])
                merged["markets"] = list(merged.get("markets", [])) + list(b.get("markets", []))
                by_key[k] = merged
            else:
                by_key[k] = dict(b)
        return list(by_key.values())

    @staticmethod
    def _alternates_due(event_id: str, commence: datetime,
                        now: datetime | None = None) -> bool:
        """
        Is this game's ladder stale enough to re-buy?

        Alternates bill PER GAME PER REFRESH, so cadence is the cost dial. It is tiered
        by time-to-event because that is where value concentrates: orders placed 3-12h
        out filled 45.8% of the time and returned +34.7%, versus 7.6% fill and a
        losing return beyond 48h. So pay for fresh ladders near the game and let
        distant ones go stale.

        Cache is in memory: a restart re-fetches, which costs a handful of credits and
        is the safe direction (stale ladders would silently misprice).
        """
        now = now or datetime.now(timezone.utc)
        hours_out = (commence - now).total_seconds() / 3600.0
        if hours_out < 0:
            return False
        every = None
        for max_h, refresh_h in config.ALTERNATE_LINE_REFRESH_TIERS:
            if hours_out <= max_h:
                every = refresh_h
                break
        if every is None:
            return False           # beyond the last tier -> outside our window
        last = _ALT_FETCHED_AT.get(event_id)
        if last is None:
            return True
        return (now - last).total_seconds() >= every * 3600

    def enrich_with_props(self, events: list[OddsEvent],
                          event_ids: set[str] | None = None) -> int:
        """
        Attach prop markets (config.PROP_MARKETS) to `events`, in place. Returns
        credits spent.

        Same per-event billing as alternates -- the bulk endpoint 422s on these -- so
        `event_ids` should be restricted to games where Kalshi actually lists the
        matching market. Kalshi is free to inspect, so we know that before spending
        anything.

        Reuses the alternates refresh cadence: a prop line and a totals ladder go stale
        at the same rate, and tracking two schedules for one event would double the
        bookkeeping for no benefit.
        """
        if not getattr(config, "ENABLE_PROP_MARKETS", False):
            return 0
        now = datetime.now(timezone.utc)
        spent = 0
        for ev in events:
            if event_ids is not None and ev.event_id not in event_ids:
                continue
            market = config.PROP_MARKETS.get(ev.sport_key)
            if not market:
                continue
            key = f"prop:{ev.event_id}"
            if not self._alternates_due(key, ev.commence_time, now):
                continue
            if spent:
                time.sleep(0.15)
            extra = self.fetch_event_alternates(ev.sport_key, ev.event_id, markets=market)
            _ALT_FETCHED_AT[key] = now
            # An empty response costs nothing (verified: Pinnacle-without-the-market
            # returns cost=0), so count only calls that returned data.
            if extra:
                spent += 1
                ev.bookmakers = self._merge_bookmakers(ev.bookmakers, extra)
        if spent:
            logger.info("Fetched prop markets for %d event(s) (~%d credits)", spent, spent)
        return spent

    def enrich_with_alternates(self, events: list[OddsEvent],
                               event_ids: set[str] | None = None) -> int:
        """
        Attach full line ladders to `events`, in place. Returns credits spent
        (1 per event fetched).

        `event_ids` restricts the spend to games worth paying for -- normally the ones
        Kalshi actually lists a totals market for. Without it every event in the window
        is fetched, which is correct but wasteful.
        """
        if not getattr(config, "ENABLE_ALTERNATE_LINES", False):
            return 0

        now = datetime.now(timezone.utc)
        spent = 0
        for ev in events:
            if event_ids is not None and ev.event_id not in event_ids:
                continue
            if not self._alternates_due(ev.event_id, ev.commence_time, now):
                continue
            if spent:
                time.sleep(0.15)   # be polite; this is N calls, not one
            extra = self.fetch_event_alternates(ev.sport_key, ev.event_id)
            _ALT_FETCHED_AT[ev.event_id] = now
            spent += 1
            if extra:
                ev.bookmakers = self._merge_bookmakers(ev.bookmakers, extra)
        if spent:
            logger.info("Fetched alternate lines for %d event(s) (~%d credits)",
                        spent, spent)
        return spent

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
