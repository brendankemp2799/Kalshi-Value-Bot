"""
In-game state layer for MLB clock-decay / Poisson totals research.

Pre-registered hypothesis: research/hypotheses/2026-08-13-mlb-clock-decay-totals.md

WHY MLB IS CLEANER THAN THE SOCCER EQUIVALENT
---------------------------------------------
The soccer layer (research/mls_ingame_state.py) had to reconstruct a minute->wallclock
mapping from ESPN keyEvents, and for many leagues had to *infer* the kickoff anchor
because ESPN emits no marker events. MLB's StatsAPI gives per-play `about.endTime`
directly, so end-of-inning wallclock is read, never derived. No inference, no anchor
guard, no dropped leagues.

WHAT IT PROVIDES
----------------
`game_states(date)` yields, per completed game:

    innings[N] -> (total_runs_after_N, wallclock_utc_end_of_inning_N)
    final_total

An inning snapshot is taken at the end of the BOTTOM half (a completed inning), so
`innings[5]` is the state a bot would see with four innings of regulation left.

Free data only: MLB StatsAPI (no auth, no rate limit documented) + Kalshi.
Never touches the Odds API.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.mlb_events_fetcher import fetch_live_feed, fetch_schedule


@dataclass
class GameState:
    game_pk: int
    date: str
    away_team: str
    home_team: str
    start: datetime
    # inning number -> (total runs after that inning, wallclock at its end)
    innings: dict[int, tuple[int, datetime]] = field(default_factory=dict)
    final_total: int = 0
    went_extras: bool = False

    def state_at(self, inning: int) -> tuple[int, datetime] | None:
        return self.innings.get(inning)


def game_states(date_str: str) -> list[GameState]:
    """Every completed MLB game on a date, with end-of-inning snapshots."""
    out: list[GameState] = []
    try:
        games = fetch_schedule(date_str)
    except Exception as e:
        print(f"schedule {date_str}: {e}", file=sys.stderr)
        return out

    for g in games:
        if g.get("status", {}).get("detailedState") != "Final":
            continue
        try:
            pk = g["gamePk"]
            away = g["teams"]["away"]["team"]["name"]
            home = g["teams"]["home"]["team"]["name"]
            start = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            continue

        try:
            feed = fetch_live_feed(pk)
            plays = feed["liveData"]["plays"]["allPlays"]
        except Exception as e:
            print(f"  feed {pk}: {e}", file=sys.stderr)
            continue
        if not plays:
            continue

        innings: dict[int, tuple[int, datetime]] = {}
        final_total = 0
        max_inning = 0
        for p in plays:
            a = p.get("about", {})
            r = p.get("result", {})
            aw, hm = r.get("awayScore"), r.get("homeScore")
            if aw is None or hm is None:
                continue
            final_total = aw + hm
            inn = a.get("inning")
            if inn:
                max_inning = max(max_inning, inn)
            # Snapshot only at the end of a COMPLETED bottom half = a full inning played.
            if a.get("isComplete") and a.get("halfInning") == "bottom" and a.get("endTime"):
                try:
                    ts = datetime.fromisoformat(a["endTime"].replace("Z", "+00:00"))
                except ValueError:
                    continue
                innings[inn] = (aw + hm, ts)

        if not innings:
            continue
        out.append(GameState(
            game_pk=pk, date=date_str, away_team=away, home_team=home, start=start,
            innings=innings, final_total=final_total, went_extras=max_inning > 9,
        ))
    return out


if __name__ == "__main__":
    for ds in sys.argv[1:]:
        gs = game_states(ds)
        print(f"{ds}: {len(gs)} completed games")
        for g in gs[:5]:
            s5 = g.state_at(5)
            s7 = g.state_at(7)
            print(f"   {g.away_team[:18]:18} @ {g.home_team[:18]:18} "
                  f"final={g.final_total:2} extras={g.went_extras!s:5} "
                  f"end5={s5[0] if s5 else '-'} end7={s7[0] if s7 else '-'}")
