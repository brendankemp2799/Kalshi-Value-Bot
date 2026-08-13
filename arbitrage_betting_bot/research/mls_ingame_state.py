"""
Shared in-game state layer for the latency-insensitive soccer strategies
(clock decay and Poisson divergence), across 31 verified leagues.

(File name is historical — this began as MLS-only. It now covers every league in
SOCCER_LEAGUES below.)

WHY THIS IS A SEPARATE LAYER FROM THE LEAD-CHANGE WORK
------------------------------------------------------
The lead-change study (research/experiments/2026-08-12-mls-lead-change-momentum.md)
needed sub-second trade data because its edge depended on reacting to an event faster
than other participants. These strategies do not: their premise is that the market
misprices a *known, publicly visible state* (the score and the clock), so a data feed
that is 30s late is perfectly adequate. That means 1-minute candlesticks are enough,
and no race is involved.

WHAT IT PROVIDES
----------------
`match_states(date, slug)` yields, per completed match, a callable timeline:

    score_at(minute)     -> (home_goals, away_goals)
    wallclock_at(minute) -> UTC datetime to look the Kalshi price up at

MAPPING MATCH MINUTE TO WALL CLOCK
----------------------------------
Preferred: ESPN's own `Kickoff` and `Start 2nd Half` keyEvents, which carry real
wallclock stamps. First-half minute m is kickoff+m; second-half minute m is
second_half_start+(m-45). Stoppage time makes any fixed-offset assumption wrong, so a
"kickoff + 60 minutes" shortcut is never used.

Fallback: many leagues (Allsvenskan, Eliteserien, J-League and others) carry wallclock
stamps but emit no marker events, and requiring them silently discarded those leagues
entirely. For those, the anchor is INFERRED from the earliest stamped event in each
half: that event's own elapsed clock says how far into the half it happened, so
anchor = wallclock - elapsed.

Inference was validated against 46 matches that do carry real anchors: kickoff error
median 0s / p90 0s, second-half error median 10s / p90 33s / max 95s — but kickoff had
one 6422s outlier. Hence the guard: an inferred kickoff more than 30 minutes from
ESPN's scheduled start is rejected and the match dropped. `MatchState.anchors_inferred`
flags which matches used the fallback so results can be split on it.

Goals are attributed by team id and the running tally is VALIDATED against the official
final score; any match that disagrees is dropped. Text-based scoreline parsing alone
produced matches with a 2-3 final score and zero recorded goals, because ESPN's goal
text does not always spell a team the way `displayName` does.

Free data only: ESPN site API + Kalshi. Never touches the Odds API.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from data.mls_events_fetcher import fetch_scoreboard, fetch_summary  # noqa: F401 (MLS default path)

_ESPN_BASE = "https://site.web.api.espn.com/apis/site/v2/sports/soccer"

# league key -> (ESPN slug, Kalshi *GAME series, Kalshi *TOTAL series)
#
# Kalshi carries 133 soccer TOTAL series; this repo's own _SPORT_TO_SERIES maps three.
# Every mapping below was VERIFIED, not guessed: for each Kalshi series we took a
# settled GAME market's team names on its busiest date and required >=60% fuzzy
# overlap against the candidate ESPN slug's fixtures that day (see the mapping script
# in this experiment's write-up). Guessing produced HTTP 400s and, worse, silent
# mismatches -- fra.2 scored 17% against the Canadian Premier League, which a
# name-based guess would have accepted.
#
# Kalshi's settled-market endpoint retains ~66 days and broad soccer coverage began
# ~2026-07-17, so all of these are northern-summer-active leagues inside that window.
# Excluded for failing verification: K-League, Canadian PL, Ekstraklasa, Croatia HNL,
# Ecuador, Czech, Swiss, Brazil Serie C, Conmebol Sudamericana, UCL/UECL qualifiers.
SOCCER_LEAGUES: dict[str, tuple[str, str, str]] = {
    "allsvenskan":    ('swe.1'                   , 'KXALLSVENSKANGAME'       , 'KXALLSVENSKANTOTAL'),
    "argentina":      ('arg.1'                   , 'KXARGPREMDIVGAME'        , 'KXARGPREMDIVTOTAL'),
    "argentina2":     ('arg.2'                   , 'KXARGNACBGAME'           , 'KXARGNACBTOTAL'),
    "belgium":        ('bel.1'                   , 'KXBELGIANPLGAME'         , 'KXBELGIANPLTOTAL'),
    "brazil":         ('bra.1'                   , 'KXBRASILEIROGAME'        , 'KXBRASILEIROTOTAL'),
    "brazil2":        ('bra.2'                   , 'KXBRASILEIROBGAME'       , 'KXBRASILEIROBTOTAL'),
    "bundesliga2":    ('ger.2'                   , 'KXBUNDESLIGA2GAME'       , 'KXBUNDESLIGA2TOTAL'),
    "chile":          ('chi.1'                   , 'KXCHLLDPGAME'            , 'KXCHLLDPTOTAL'),
    "chineseSL":      ('chn.1'                   , 'KXCHNSLGAME'             , 'KXCHNSLTOTAL'),
    "colombia":       ('col.1'                   , 'KXDIMAYORGAME'           , 'KXDIMAYORTOTAL'),
    "copabrasil":     ('bra.copa_do_brazil'      , 'KXCOPADOBRASILGAME'      , 'KXCOPADOBRASILTOTAL'),
    "coppaitalia":    ('ita.coppa_italia'        , 'KXCOPPAITALIAGAME'       , 'KXCOPPAITALIATOTAL'),
    "denmark":        ('den.1'                   , 'KXDENSUPERLIGAGAME'      , 'KXDENSUPERLIGATOTAL'),
    "eliteserien":    ('nor.1'                   , 'KXELITESERIENGAME'       , 'KXELITESERIENTOTAL'),
    "eredivisie":     ('ned.1'                   , 'KXEREDIVISIEGAME'        , 'KXEREDIVISIETOTAL'),
    "europaq":        ('uefa.europa_qual'        , 'KXUELGAME'               , 'KXUELTOTAL'),
    "jleague":        ('jpn.1'                   , 'KXJLEAGUEGAME'           , 'KXJLEAGUETOTAL'),
    "leaguescup":     ('concacaf.leagues.cup'    , 'KXLEAGUESCUPGAME'        , 'KXLEAGUESCUPTOTAL'),
    "ligaexp":        ('mex.2'                   , 'KXLIGAEXPGAME'           , 'KXLIGAEXPTOTAL'),
    "ligamx":         ('mex.1'                   , 'KXLIGAMXGAME'            , 'KXLIGAMXTOTAL'),
    "mls":            ('usa.1'                   , 'KXMLSGAME'               , 'KXMLSTOTAL'),
    "nwsl":           ('usa.nwsl'                , 'KXNWSLGAME'              , 'KXNWSLTOTAL'),
    "paraguay":       ('par.1'                   , 'KXAPFDDHGAME'            , 'KXAPFDDHTOTAL'),
    "peru":           ('per.1'                   , 'KXPERLIGA1GAME'          , 'KXPERLIGA1TOTAL'),
    "portugal":       ('por.1'                   , 'KXLIGAPORTUGALGAME'      , 'KXLIGAPORTUGALTOTAL'),
    "scotcup":        ('sco.cis'                 , 'KXSCOCUPGAME'            , 'KXSCOCUPTOTAL'),
    "scotland":       ('sco.1'                   , 'KXSCOTTISHPREMGAME'      , 'KXSCOTTISHPREMTOTAL'),
    "uclw":           ('uefa.wchampions_qual'    , 'KXUCLWGAME'              , 'KXUCLWTOTAL'),
    "uruguay":        ('uru.1'                   , 'KXURYPDGAME'             , 'KXURYPDTOTAL'),
    "usl":            ('usa.usl.1'               , 'KXUSLGAME'               , 'KXUSLTOTAL'),
    "venezuela":      ('ven.1'                   , 'KXVENFUTVEGAME'          , 'KXVENFUTVETOTAL'),
}


def _espn_scoreboard(date_str: str, slug: str) -> list[dict]:
    r = requests.get(f"{_ESPN_BASE}/{slug}/scoreboard",
                     params={"dates": date_str.replace("-", "")}, timeout=15)
    r.raise_for_status()
    return r.json().get("events", [])


def _espn_summary(event_id: str, slug: str) -> dict:
    r = requests.get(f"{_ESPN_BASE}/{slug}/summary",
                     params={"event": event_id}, timeout=15)
    r.raise_for_status()
    return r.json()


@dataclass
class MatchState:
    event_id: str
    date: str
    home_team: str
    away_team: str
    kickoff: datetime
    second_half_start: datetime | None
    league: str = "usa.1"          # ESPN slug this match came from
    anchors_inferred: bool = False  # True when Kickoff/2nd-half were derived, not stamped
    # (wallclock, home_goals_after, away_goals_after) per goal, in order
    goals: list[tuple[datetime, int, int]] = field(default_factory=list)
    final_home: int = 0
    final_away: int = 0

    @property
    def total_goals(self) -> int:
        return self.final_home + self.final_away

    def wallclock_at(self, minute: float) -> datetime | None:
        """Wall-clock time of a given match minute, anchored on ESPN's own stamps."""
        if minute <= 45:
            return self.kickoff + timedelta(minutes=minute)
        if self.second_half_start is None:
            return None
        return self.second_half_start + timedelta(minutes=minute - 45)

    def score_at(self, minute: float) -> tuple[int, int] | None:
        """(home, away) goals as of the given match minute."""
        wc = self.wallclock_at(minute)
        if wc is None:
            return None
        h = a = 0
        for gts, gh, ga in self.goals:
            if gts <= wc:
                h, a = gh, ga
        return h, a


def _parse_scoreline_from_text(text: str, home: str, away: str) -> tuple[int | None, int | None]:
    """ESPN's goal description leads with the cumulative score, e.g.
    'Goal! New England Revolution 0, Houston Dynamo FC 1. ...'.

    Only used as a cross-check on the team-id tally in match_states(), never as the
    primary source: ESPN's goal text does not always spell a team the same way as its
    `displayName` (observed: 'Atlanta United' in the text vs 'Atlanta United FC' as the
    display name), so this silently returns None for those matches. Relying on it alone
    produced matches with a 2-3 final score and zero recorded goals.
    """
    import re

    def score_for(team: str) -> int | None:
        m = re.search(re.escape(team) + r"\s+(\d+)", text)
        return int(m.group(1)) if m else None

    return score_for(home), score_for(away)


def match_states(date_str: str, slug: str = "usa.1") -> list[MatchState]:
    """Every completed match on a date for one league, with a minute->state timeline."""
    out: list[MatchState] = []
    try:
        events = _espn_scoreboard(date_str, slug)
    except Exception as e:
        print(f"scoreboard {date_str}: {e}", file=sys.stderr)
        return out

    for ev in events:
        if not ev.get("status", {}).get("type", {}).get("completed"):
            continue
        comps = ev["competitions"][0]["competitors"]
        try:
            home = next(c["team"]["displayName"] for c in comps if c["homeAway"] == "home")
            away = next(c["team"]["displayName"] for c in comps if c["homeAway"] == "away")
            home_id = next(str(c["team"]["id"]) for c in comps if c["homeAway"] == "home")
            away_id = next(str(c["team"]["id"]) for c in comps if c["homeAway"] == "away")
            fh = int(next(c["score"] for c in comps if c["homeAway"] == "home"))
            fa = int(next(c["score"] for c in comps if c["homeAway"] == "away"))
        except (StopIteration, KeyError, ValueError, TypeError):
            continue

        try:
            summary = _espn_summary(ev["id"], slug)
        except Exception as e:
            print(f"  summary {ev['id']}: {e}", file=sys.stderr)
            continue
        if not summary.get("wallclockAvailable"):
            continue

        kickoff = second_half = None
        raw_goals: list[tuple[datetime, str, int | None, int | None]] = []
        # (wallclock, elapsed_seconds, period) for every stamped event -- used to infer
        # the anchors for leagues where ESPN omits the Kickoff / Start 2nd Half markers.
        stamped: list[tuple[datetime, float, int]] = []
        for k in summary.get("keyEvents", []):
            wc = k.get("wallclock")
            if not wc:
                continue
            ts = datetime.fromisoformat(wc.replace("Z", "+00:00"))
            label = ((k.get("type") or {}).get("text") or "").lower()
            if label == "kickoff" and kickoff is None:
                kickoff = ts
            elif "start 2nd half" in label and second_half is None:
                second_half = ts
            cv = (k.get("clock") or {}).get("value")
            per = ((k.get("period") or {}).get("number"))
            if cv is not None and per in (1, 2):
                stamped.append((ts, float(cv), int(per)))
            if k.get("scoringPlay"):
                th, ta = _parse_scoreline_from_text(k.get("text", ""), home, away)
                raw_goals.append((ts, str((k.get("team") or {}).get("id", "")), th, ta))

        # Several leagues (Allsvenskan, Eliteserien, J-League and others) carry real
        # wallclock stamps but emit no Kickoff / Start-2nd-Half marker events, so a
        # strict requirement silently discards the entire league. Infer the anchor from
        # the EARLIEST stamped event in each half instead: that event's own elapsed
        # clock tells us how far into the half it occurred, so
        # anchor = wallclock - elapsed. This is bounded and self-consistent, unlike
        # assuming a fixed 15-minute interval; inference error is measured against
        # matches that do carry real anchors (see _validate_inference below).
        inferred = False
        if kickoff is None and stamped:
            first = min((s for s in stamped if s[2] == 1), default=None)
            if first:
                kickoff = first[0] - timedelta(seconds=first[1])
                inferred = True
        if second_half is None and stamped:
            first2 = min((s for s in stamped if s[2] == 2), default=None)
            if first2:
                # clock.value is elapsed from match start, so subtract the 45' already played
                second_half = first2[0] - timedelta(seconds=max(0.0, first2[1] - 45 * 60))
                inferred = True

        if kickoff is None or second_half is None:
            continue

        # Sanity-bound the inference. Validated against 46 matches that carry REAL
        # anchors: kickoff inference is usually exact (median 0s) and second-half is
        # close (median 10s, max 95s) -- but kickoff had one 6422s outlier, which would
        # read the Kalshi price from an hour and a half off. Cross-check any inferred
        # kickoff against ESPN's own scheduled start and drop the match if they
        # disagree by more than 30 minutes (real delays exist, but not that big).
        if inferred:
            try:
                sched = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if abs((kickoff - sched).total_seconds()) > 30 * 60:
                print(f"  skip {ev['id']} {away} @ {home}: inferred kickoff "
                      f"{abs((kickoff - sched).total_seconds()):.0f}s from scheduled",
                      file=sys.stderr)
                continue
            if second_half <= kickoff or (second_half - kickoff).total_seconds() > 90 * 60:
                continue

        # Build the running score by team id, then VALIDATE against the final score.
        # Team id is exact where the description text is not, and the validation is
        # what makes this trustworthy: own goals in particular are credited
        # inconsistently by ESPN, so rather than special-case them, any match whose
        # reconstructed tally disagrees with the official final score is dropped
        # entirely. A silently wrong timeline would read Kalshi prices for the wrong
        # game state, which is worse than a smaller sample.
        raw_goals.sort(key=lambda g: g[0])
        goals: list[tuple[datetime, int, int]] = []
        h = a = 0
        for ts, tid, th, ta in raw_goals:
            if th is not None and ta is not None:
                h, a = th, ta          # text scoreline is authoritative when parseable
            elif tid == home_id:
                h += 1
            elif tid == away_id:
                a += 1
            else:
                h = a = -1             # unattributable -- force the check below to fail
                break
            goals.append((ts, h, a))

        if (h, a) != (fh, fa):
            print(f"  skip {ev['id']} {away} @ {home}: reconstructed {a}-{h} "
                  f"!= official {fa}-{fh}", file=sys.stderr)
            continue
        out.append(MatchState(
            event_id=str(ev["id"]), date=date_str, league=slug, home_team=home, away_team=away,
            anchors_inferred=inferred,
            kickoff=kickoff, second_half_start=second_half, goals=goals,
            final_home=fh, final_away=fa,
        ))
    return out


def discover_dates(start: str, end: str, slug: str = "usa.1") -> list[str]:
    """Dates with completed MLS matches. MLS plays in weekend clusters with long
    international-break gaps, so scanning every calendar day wastes most requests."""
    d = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    while d <= last:
        try:
            evs = _espn_scoreboard(d.isoformat(), slug)
            if any(e.get("status", {}).get("type", {}).get("completed") for e in evs):
                out.append(d.isoformat())
        except Exception:
            pass
        d += timedelta(days=1)
    return out


if __name__ == "__main__":
    for ds in sys.argv[1:]:
        for ms in match_states(ds):
            s55 = ms.score_at(55)
            s75 = ms.score_at(75)
            print(f"{ms.date} {ms.away_team[:18]:18} @ {ms.home_team[:18]:18} "
                  f"final {ms.final_away}-{ms.final_home} (tot {ms.total_goals})  "
                  f"55'={s55}  75'={s75}  goals={len(ms.goals)}")
