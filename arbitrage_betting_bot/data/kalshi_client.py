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

  Totals, spreads, and BTTS markets use separate series (e.g. KXNBATOTAL).
  Each threshold is a distinct market — we keep them all (no deduplication).
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

# Maps Odds API sport keys → list of Kalshi series tickers (H2H first, then non-H2H)
_SPORT_TO_SERIES: dict[str, list[str]] = {
    "americanfootball_nfl":      ["KXNFLGAME"],
    "americanfootball_ncaaf":    ["KXNCAAFGAME"],
    "basketball_nba":            ["KXNBAGAME", "KXNBATOTAL", "KXNBASPREAD"],
    "basketball_ncaab":          ["KXNCAABGAME"],
    "baseball_mlb":              ["KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD"],
    "icehockey_nhl":             ["KXNHLGAME", "KXNHLTOTAL", "KXNHLSPREAD"],
    "soccer_usa_mls":            ["KXMLSGAME", "KXMLSBTTS"],
    "soccer_epl":                ["KXEPLGAME", "KXEPLBTTS"],
    "soccer_uefa_champs_league": ["KXUCLGAME", "KXUCLBTTS"],
}

# Maps Kalshi series prefix → bet type ("h2h" is default / omitted)
_SERIES_TO_BET_TYPE: dict[str, str] = {
    "KXNBATOTAL":  "totals",
    "KXMLBTOTAL":  "totals",
    "KXNHLTOTAL":  "totals",
    "KXEPLTOTAL":  "totals",
    "KXUCLTOTAL":  "totals",
    "KXMLSTOTAL":  "totals",
    "KXNBASPREAD": "spread",
    "KXMLBSPREAD": "spread",
    "KXNHLSPREAD": "spread",
    "KXMLSBTTS":   "btts",
    "KXEPLBTTS":   "btts",
    "KXUCLBTTS":   "btts",
}


def _parse_threshold(title: str, bet_type: str) -> float | None:
    """
    Extract the numeric threshold from a Kalshi market title.
      Totals:  "Detroit Pistons vs Orlando Magic Over 222.5?"  → 222.5
      Spread:  "Detroit Pistons -3.5 at Orlando Magic?"        → -3.5
      BTTS:    None
    """
    if bet_type == "totals":
        m = re.search(r"(?:Over|Under)\s+([\d.]+)", title, re.IGNORECASE)
        if m:
            return float(m.group(1))
    elif bet_type == "spread":
        # Spread value is the first number with a sign attached to a team name
        m = re.search(r"([+-][\d.]+)", title)
        if m:
            return float(m.group(1))
    return None


@dataclass
class KalshiMarket:
    ticker: str
    title: str          # e.g. "New York at Atlanta Winner?"
    yes_team: str       # team/label that wins if YES resolves
    no_team: str        # the other team in the matchup (H2H) or "Under"/"No" etc.
    yes_price: float    # 0.0 – 1.0  (mid of bid/ask)
    no_price: float     # 0.0 – 1.0
    yes_bid: float      # raw bid price (for spread calculation)
    yes_ask: float      # raw ask price (for spread calculation)
    volume: float
    close_time: str
    category: str
    event_ticker: str = field(default="")
    bet_type: str = field(default="h2h")       # "h2h", "totals", "spread", "btts"
    threshold: float | None = field(default=None)  # line value (totals/spread only)

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
            parts = self.event_ticker.split("-")
            if len(parts) < 2:
                return self.close_time
            date_seg = parts[1][:7]  # e.g. "26APR14"
            game_date = datetime.strptime(date_seg, "%y%b%d")
            ct = self.close_time.replace("Z", "+00:00")
            ct_dt = datetime.fromisoformat(ct)
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
        Fetch Kalshi markets for all configured sports and bet types.

        Queries each sport's series tickers (H2H + totals + spreads + BTTS),
        parses prices from the dollar-denominated fields, and deduplicates:
          - H2H markets: one per game event (by event_ticker)
          - Non-H2H markets: all kept (each threshold/direction is a separate opportunity)
        """
        seen_series: set[str] = set()
        raw_all: list[tuple[str, dict]] = []  # (series_ticker, raw_market)

        for sport in config.SPORTS:
            series_list = _SPORT_TO_SERIES.get(sport, [])
            for series in series_list:
                if series in seen_series:
                    continue
                seen_series.add(series)
                raw = self._fetch_series_markets(series)
                for r in raw:
                    raw_all.append((series, r))
                logger.debug("Series %s: %d raw markets", series, len(raw))

        # Parse and deduplicate
        by_event: dict[str, KalshiMarket] = {}   # H2H dedup key → market
        non_h2h: list[KalshiMarket] = []         # totals/spreads/btts kept individually

        for series_ticker, raw in raw_all:
            event_ticker = raw.get("event_ticker", "")

            # Determine bet_type from series
            series_prefix = event_ticker.split("-")[0].upper() if event_ticker else series_ticker.upper()
            bet_type = _SERIES_TO_BET_TYPE.get(series_prefix, "h2h")

            yes_team = raw.get("yes_sub_title", "") or ""

            # Extract the other team / label from the title
            no_team = ""
            title_raw = raw.get("title", "") or ""

            if bet_type == "h2h":
                # For H2H: parse home/away from "Team1 at Team2 Winner?"
                title_clean = re.sub(r"(?i)\s*winner\s*\??$", "", title_raw).strip()
                for sep in [" at ", " vs ", " @ "]:
                    if sep in title_clean:
                        t1, t2 = title_clean.split(sep, 1)
                        t1, t2 = t1.strip(), t2.strip()
                        yt_lower = yes_team.lower()
                        if yt_lower and (yt_lower in t2.lower() or t2.lower() in yt_lower):
                            no_team = t1
                        else:
                            no_team = t2
                        break
            else:
                # For non-H2H: no_team is just the NO label (e.g. "Under", "No")
                no_sub = raw.get("no_sub_title", "") or ""
                no_team = no_sub

            # Prices
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

            # Parse threshold for totals/spreads
            threshold = _parse_threshold(title_raw, bet_type)

            km = KalshiMarket(
                ticker=raw.get("ticker", ""),
                title=title_raw,
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
                bet_type=bet_type,
                threshold=threshold,
            )

            if bet_type == "h2h":
                # H2H deduplication: TIE markets separate, team markets one per event
                if yes_team.lower() == "tie":
                    tie_key = f"tie:{event_ticker}"
                    if tie_key not in by_event:
                        by_event[tie_key] = km
                else:
                    if event_ticker not in by_event or (yes_team and not by_event[event_ticker].yes_team):
                        by_event[event_ticker] = km
            else:
                # Non-H2H: keep each market (each threshold/direction is distinct)
                non_h2h.append(km)

        markets = list(by_event.values()) + non_h2h
        h2h_count = len(by_event)
        logger.info(
            "Fetched %d Kalshi markets (%d H2H/TIE, %d totals/spread/btts)",
            len(markets), h2h_count, len(non_h2h),
        )
        return markets
