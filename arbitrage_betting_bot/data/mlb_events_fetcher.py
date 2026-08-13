"""
Fetches MLB schedule and play-by-play from MLB's public Stats API.
No auth, no documented rate limit, free. New data source — not used anywhere
else in this repo.

Docs (unofficial, widely used):
  https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=YYYY-MM-DD
  https://statsapi.mlb.com/api/v1.1/game/{gamePk}/feed/live
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dataclasses import dataclass
from datetime import datetime, timezone

import requests

_BASE = "https://statsapi.mlb.com/api"


def fetch_schedule(date_str: str) -> list[dict]:
    """All MLB games on a given date (YYYY-MM-DD). Empty list if none."""
    resp = requests.get(
        f"{_BASE}/v1/schedule",
        params={"sportId": 1, "date": date_str},
        timeout=15,
    )
    resp.raise_for_status()
    dates = resp.json().get("dates", [])
    if not dates:
        return []
    return dates[0].get("games", [])


def fetch_live_feed(game_pk: int) -> dict:
    """Full play-by-play feed for one game."""
    resp = requests.get(f"{_BASE}/v1.1/game/{game_pk}/feed/live", timeout=15)
    resp.raise_for_status()
    return resp.json()


@dataclass
class HomeRunEvent:
    game_pk: int
    inning: int
    half_inning: str          # "top" or "bottom"
    team: str                 # full name of the scoring team
    description: str
    away_score: int           # score after the play
    home_score: int           # score after the play
    event_ts: datetime        # UTC
    timestamp_confidence: str # "pitch" or "at_bat_start"


def _hit_pitch_ts(play: dict) -> datetime | None:
    """The startTime of the pitch that was actually put in play, if present."""
    for ev in reversed(play.get("playEvents", [])):
        if ev.get("isPitch") and ev.get("details", {}).get("isInPlay"):
            ts = ev.get("startTime")
            if ts:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return None


def extract_home_runs(feed: dict) -> list[HomeRunEvent]:
    """Every home-run play in a game's feed, with the most precise timestamp
    available (the pitch that was hit, not the whole at-bat or the trot)."""
    game_pk = feed.get("gamePk")
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    home_team = feed.get("gameData", {}).get("teams", {}).get("home", {}).get("name")
    away_team = feed.get("gameData", {}).get("teams", {}).get("away", {}).get("name")

    out: list[HomeRunEvent] = []
    for play in plays:
        result = play.get("result", {})
        if result.get("eventType") != "home_run":
            continue
        about = play.get("about", {})
        is_top = about.get("isTopInning")
        team = away_team if is_top else home_team

        ts = _hit_pitch_ts(play)
        confidence = "pitch"
        if ts is None:
            confidence = "at_bat_start"
            start = about.get("startTime")
            ts = (
                datetime.fromisoformat(start.replace("Z", "+00:00"))
                if start else None
            )
        if ts is None:
            continue  # no usable timestamp at all — skip, can't measure latency

        out.append(HomeRunEvent(
            game_pk=game_pk,
            inning=about.get("inning"),
            half_inning="top" if is_top else "bottom",
            team=team,
            description=result.get("description", ""),
            away_score=result.get("awayScore"),
            home_score=result.get("homeScore"),
            event_ts=ts.astimezone(timezone.utc),
            timestamp_confidence=confidence,
        ))
    return out
