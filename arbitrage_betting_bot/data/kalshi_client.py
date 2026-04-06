"""
Fetches market prices from Kalshi's REST API v2.
Requires KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH set in .env.

API docs: https://trading-api.kalshi.com/trade-api/v2/openapi.json

Market structure (current):
  Game-winner markets live under series tickers (e.g. KXNBAGAME).
  Each game produces two markets — one per team — with tickers like:
    KXNBAGAME-26APR06NYKATL-NYK  (YES = New York wins)
    KXNBAGAME-26APR06NYKATL-ATL  (YES = Atlanta wins)
  We fetch all and deduplicate to one market per game (by event_ticker),
  keeping whichever market's YES team matches the home team, or the first
  one found.  Prices are in yes_bid_dollars / no_bid_dollars (USD strings).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

# Maps Odds API sport keys → Kalshi game-winner series tickers
_SPORT_TO_SERIES: dict[str, str] = {
    "americanfootball_nfl":        "KXNFLGAME",
    "americanfootball_ncaaf":      "KXNCAAFGAME",
    "basketball_nba":              "KXNBAGAME",
    "basketball_ncaab":            "KXNCAABGAME",
    "baseball_mlb":                "KXMLBGAME",
    "icehockey_nhl":               "KXNHLGAME",
    "soccer_usa_mls":              "KXMLSGAME",
    "soccer_epl":                  "KXEPLGAME",
    "soccer_uefa_champs_league":   "KXUCLGAME",
}


@dataclass
class KalshiMarket:
    ticker: str
    title: str          # e.g. "New York at Atlanta Winner?"
    yes_team: str       # team that wins if YES resolves, e.g. "New York"
    no_team: str        # the other team in the matchup, e.g. "Atlanta"
    yes_price: float    # 0.0 – 1.0  (mid of bid/ask)
    no_price: float     # 0.0 – 1.0
    yes_bid: float      # raw bid price (for spread calculation)
    yes_ask: float      # raw ask price (for spread calculation)
    volume: float
    close_time: str
    category: str
    event_ticker: str = field(default="")  # e.g. "KXNBAGAME-26APR06NYKATL"

    @property
    def spread(self) -> float:
        """Bid-ask spread (0.0–1.0). Smaller = more liquid."""
        return round(self.yes_ask - self.yes_bid, 4)

    @property
    def game_time(self) -> str:
        """
        ISO 8601 UTC string for actual game kickoff/tip-off.

        Kalshi's close_time is the settlement deadline (~2 weeks after the game),
        not the start time. The game DATE is encoded in the event_ticker:
          KXUCLGAME-26APR14ATMBAR  →  April 14, 2026
        The game TIME (hour:minute) is correct in close_time — only the date is off.
        We combine the ticker date with the close_time's UTC hour:minute.
        Falls back to close_time if parsing fails.
        """
        try:
            # Extract date segment from event_ticker: "KXUCLGAME-26APR14ATMBAR" → "26APR14"
            parts = self.event_ticker.split("-")
            if len(parts) < 2:
                return self.close_time
            date_seg = parts[1][:7]  # e.g. "26APR14"
            game_date = datetime.strptime(date_seg, "%y%b%d")

            # Extract UTC time from close_time: "2026-04-28T19:00:00Z" → hour=19, min=0
            ct = self.close_time.replace("Z", "+00:00")
            ct_dt = datetime.fromisoformat(ct)

            # Combine: correct date + correct time, in UTC
            game_dt = game_date.replace(
                hour=ct_dt.hour, minute=ct_dt.minute, tzinfo=timezone.utc
            )
            return game_dt.isoformat()
        except Exception:
            return self.close_time


class KalshiClient:
    def __init__(self):
        self.base_url = config.KALSHI_API_BASE_URL

    def _get(self, path: str, params: dict | None = None) -> dict:
        from data.kalshi_auth import auth_headers
        url = f"{self.base_url}{path}"
        headers = auth_headers("GET", url)
        resp = requests.get(url, params=params or {}, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_price(raw: dict, field_dollars: str, field_cents: str) -> float | None:
        """Parse a price from dollar string or cents integer, returning 0.0–1.0."""
        val = raw.get(field_dollars)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
        val = raw.get(field_cents)
        if val is not None:
            try:
                return float(val) / 100.0
            except (ValueError, TypeError):
                pass
        return None

    def _fetch_series_markets(self, series_ticker: str) -> list[dict]:
        """Fetch all open markets for one Kalshi series (paginated)."""
        results: list[dict] = []
        cursor = None
        while True:
            params: dict = {
                "limit": 200,
                "status": "open",
                "series_ticker": series_ticker,
            }
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get("/markets", params)
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                logger.error("Kalshi HTTP %s fetching series %s", status, series_ticker)
                break
            except requests.RequestException as e:
                logger.error("Kalshi request failed for series %s: %s", series_ticker, e)
                break
            results.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return results

    def fetch_sports_markets(self) -> list[KalshiMarket]:
        """
        Fetch Kalshi game-winner markets for all configured sports.

        Queries each sport's series ticker (e.g. KXNBAGAME for NBA),
        parses prices from the new dollar-denominated fields, and
        deduplicates to one market per game event.
        """
        # Collect raw markets across all active sport series
        seen_series: set[str] = set()
        raw_all: list[dict] = []
        for sport in config.SPORTS:
            series = _SPORT_TO_SERIES.get(sport)
            if not series or series in seen_series:
                continue
            seen_series.add(series)
            raw = self._fetch_series_markets(series)
            raw_all.extend(raw)
            logger.debug("Series %s: %d raw markets", series, len(raw))

        # Parse and deduplicate (one market per event_ticker)
        # For each event, prefer the market whose yes_team is the home team
        # (we can't know home vs away yet, so we just keep one per event)
        by_event: dict[str, KalshiMarket] = {}
        for raw in raw_all:
            event_ticker = raw.get("event_ticker", "")

            yes_team = raw.get("yes_sub_title", "") or ""

            # Extract the other team from the title for cross-match validation.
            # Title format: "Team1 at Team2 Winner?" → away=Team1, home=Team2
            no_team = ""
            title_raw = raw.get("title", "") or ""
            title_clean = re.sub(r"(?i)\s*winner\s*\??$", "", title_raw).strip()
            for sep in [" at ", " vs ", " @ "]:
                if sep in title_clean:
                    t1, t2 = title_clean.split(sep, 1)
                    t1, t2 = t1.strip(), t2.strip()
                    # yes_team matches one side; no_team is the other
                    yt_lower = yes_team.lower()
                    if yt_lower and (yt_lower in t2.lower() or t2.lower() in yt_lower):
                        no_team = t1
                    else:
                        no_team = t2
                    break

            # Price: prefer bid/ask mid for yes; fall back to last_price
            yes_bid = self._parse_price(raw, "yes_bid_dollars", "yes_bid")
            yes_ask = self._parse_price(raw, "yes_ask_dollars", "yes_ask")
            if yes_bid is not None and yes_ask is not None:
                yes_price = (yes_bid + yes_ask) / 2.0
            elif yes_bid is not None:
                yes_price = yes_bid
            elif yes_ask is not None:
                yes_price = yes_ask
            else:
                yes_price = self._parse_price(raw, "last_price_dollars", "last_price")

            if yes_price is None or yes_price <= 0:
                continue

            no_bid = self._parse_price(raw, "no_bid_dollars", "no_bid")
            no_ask = self._parse_price(raw, "no_ask_dollars", "no_ask")
            if no_bid is not None and no_ask is not None:
                no_price = (no_bid + no_ask) / 2.0
            elif no_bid is not None:
                no_price = no_bid
            else:
                no_price = 1.0 - yes_price

            raw_yes_bid = self._parse_price(raw, "yes_bid_dollars", "yes_bid") or 0.0
            raw_yes_ask = self._parse_price(raw, "yes_ask_dollars", "yes_ask") or yes_price

            km = KalshiMarket(
                ticker=raw.get("ticker", ""),
                title=raw.get("title", ""),
                yes_team=yes_team,
                no_team=no_team,
                yes_price=round(yes_price, 4),
                no_price=round(no_price, 4),
                yes_bid=round(raw_yes_bid, 4),
                yes_ask=round(raw_yes_ask, 4),
                volume=float(raw.get("volume_fp") or raw.get("volume", 0) or 0),
                close_time=raw.get("close_time", ""),
                category=raw.get("category", "") or "",
                event_ticker=event_ticker,
            )

            # TIE markets are kept separately (one per event).
            # Team markets are deduplicated to one per event (matcher picks the
            # right outcome via yes_team fuzzy-matching anyway).
            if yes_team.lower() == "tie":
                tie_key = f"tie:{event_ticker}"
                if tie_key not in by_event:
                    by_event[tie_key] = km
            else:
                if event_ticker not in by_event or (yes_team and not by_event[event_ticker].yes_team):
                    by_event[event_ticker] = km

        markets = list(by_event.values())
        logger.info("Fetched %d Kalshi markets", len(markets))
        return markets
