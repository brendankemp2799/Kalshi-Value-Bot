"""
Fetches MLS scoreboard and goal events from ESPN's public (undocumented) site API.
No API key required. Use the site.web.api.espn.com host, not site.api.espn.com --
the latter returns HTTP 403 (Akamai edge block) from at least this network; the
former serves identical data and was confirmed working live (2026-08-11).

Docs: none official -- widely used by hobbyist projects.
  Scoreboard: https://site.web.api.espn.com/apis/site/v2/sports/soccer/usa.1/scoreboard?dates=YYYYMMDD
  Summary:    https://site.web.api.espn.com/apis/site/v2/sports/soccer/usa.1/summary?event={id}
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/soccer/usa.1"


def fetch_scoreboard(date_str: str) -> list[dict]:
    """All MLS games on a given date (YYYY-MM-DD). Empty list if none. Note ESPN
    wants dates as YYYYMMDD (no dashes), unlike MLB's schedule endpoint."""
    resp = requests.get(f"{_BASE}/scoreboard", params={"dates": date_str.replace("-", "")}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("events", [])


def fetch_summary(event_id: str) -> dict:
    resp = requests.get(f"{_BASE}/summary", params={"event": event_id}, timeout=15)
    resp.raise_for_status()
    return resp.json()


@dataclass
class GoalEvent:
    event_id: str
    team: str
    description: str
    clock_display: str    # e.g. "45'+1'"
    event_ts: datetime    # UTC
    timestamp_confidence: str  # "wallclock" -- only value produced; events without
                                # one are skipped, there's no lower-precision fallback
                                # for soccer the way MLB has about.startTime
    home_score: int | None = None  # cumulative score AFTER this goal, both teams --
    away_score: int | None = None  # None if the scoreline couldn't be parsed from text


def _parse_scoreline(text: str, home_team: str, away_team: str) -> tuple[int | None, int | None]:
    """ESPN's goal description always leads with 'TeamA X, TeamB Y.' (cumulative
    score after the goal, both teams) before the scorer detail, e.g. 'Goal! New
    England Revolution 0, Houston Dynamo FC 1. Guilherme (Houston Dynamo FC)...'
    -- search for each known team name followed by a number, rather than a
    fixed-position regex, since word order varies by which team is 'home' in
    the text vs in our own home/away fields."""
    def score_for(team: str) -> int | None:
        m = re.search(re.escape(team) + r"\s+(\d+)", text)
        return int(m.group(1)) if m else None

    return score_for(home_team), score_for(away_team)


def extract_goals(summary: dict, event_id: str, home_team: str, away_team: str) -> list[GoalEvent]:
    """Every scoring play in a game's summary, using ESPN's real wallclock timestamp
    (confirmed live: wallclockAvailable=True for recent MLS games). A goal without
    one is skipped -- can't measure latency without a timestamp, and there's no
    good fallback (unlike MLB, clock/period alone can't be converted to wall-clock
    time reliably because added time varies)."""
    if not summary.get("wallclockAvailable"):
        return []
    out: list[GoalEvent] = []
    for ev in summary.get("keyEvents", []):
        if not ev.get("scoringPlay"):
            continue
        wc = ev.get("wallclock")
        if not wc:
            continue
        ts = datetime.fromisoformat(wc.replace("Z", "+00:00")).astimezone(timezone.utc)
        text = ev.get("text", "")
        home_score, away_score = _parse_scoreline(text, home_team, away_team)
        out.append(GoalEvent(
            event_id=event_id,
            team=(ev.get("team") or {}).get("displayName", ""),
            description=text,
            clock_display=(ev.get("clock") or {}).get("displayValue", ""),
            event_ts=ts,
            timestamp_confidence="wallclock",
            home_score=home_score,
            away_score=away_score,
        ))
    return out
