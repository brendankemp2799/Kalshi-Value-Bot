"""
Derives a flagged bet's real-world result from The Odds API's /scores
endpoint, since no Kalshi market-resolution field exists anymore for this
path (execution/auto_settle.py's entire mechanism has no retail-book
equivalent -- none of these books expose a settlement API to a bot).

Matches by odds_api_event_id -- the same event id both the odds endpoint and
the scores endpoint use (confirmed via The Odds API docs).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from data.odds_fetcher import OddsAPIClient
from core.payout_calculator import compute_pnl
from core.odds_converter import _names_match
import storage.db as db

logger = logging.getLogger(__name__)


def derive_result(
    bet_type: str, side: str, threshold: float | None,
    home_team: str, away_team: str,
    home_score: float, away_score: float,
) -> str | None:
    """'won' | 'lost' | 'push', or None if this can't be derived with
    confidence -- routes to manual settlement instead of guessing."""
    if bet_type == "h2h":
        if side.lower() == "draw":
            return "won" if home_score == away_score else "lost"
        if home_score == away_score:
            # A tie with no draw market priced for this side (shouldn't happen
            # for soccer -- see core/retail_value_detector.py's 3-way handling
            # -- but don't guess if it somehow does).
            return None
        winner = home_team if home_score > away_score else away_team
        return "won" if _names_match(side, winner) else "lost"

    if bet_type == "totals":
        if threshold is None:
            return None
        total = home_score + away_score
        if total == threshold:
            return "push"
        over_won = total > threshold
        return "won" if (over_won == (side == "over")) else "lost"

    if bet_type == "spread":
        if threshold is None:
            return None
        if _names_match(side, home_team):
            team_score, opp_score = home_score, away_score
        elif _names_match(side, away_team):
            team_score, opp_score = away_score, home_score
        else:
            return None
        margin = (team_score - opp_score) + threshold
        if margin == 0:
            return "push"
        return "won" if margin > 0 else "lost"

    return None


def settle_due_flagged_bets() -> int:
    """For each sport with a PLACED bet whose commence_time has passed, fetch
    that sport's scores once and settle every match found. Returns how many
    bets were settled. Batches by sport, same discipline as execution/
    auto_settle.py::auto_settle_positions() batching by Kalshi ticker."""
    placed = db.get_flagged_bets(status="PLACED", limit=1000)
    if not placed:
        return 0

    now = datetime.now(timezone.utc)
    by_sport: dict[str, list] = {}
    for bet in placed:
        commence = datetime.fromisoformat(bet["commence_time"])
        if commence.tzinfo is None:
            commence = commence.replace(tzinfo=timezone.utc)
        if commence > now:
            continue  # game hasn't started yet -- nothing to check
        by_sport.setdefault(bet["sport"], []).append(bet)

    if not by_sport:
        return 0

    client = OddsAPIClient()
    settled = 0
    for sport, bets in by_sport.items():
        scores = client.fetch_scores(sport)
        scores_by_id = {s.get("id"): s for s in scores}
        for bet in bets:
            score_row = scores_by_id.get(bet["odds_api_event_id"])
            if not score_row or not score_row.get("completed"):
                continue
            raw_scores = score_row.get("scores") or []
            home_score = away_score = None
            for s in raw_scores:
                if s.get("name") == bet["home_team"]:
                    home_score = float(s["score"])
                elif s.get("name") == bet["away_team"]:
                    away_score = float(s["score"])
            if home_score is None or away_score is None:
                logger.warning(
                    "Scores present but team names didn't match for bet #%d (%s vs %s)",
                    bet["id"], bet["home_team"], bet["away_team"],
                )
                continue

            result = derive_result(
                bet["bet_type"], bet["side"], bet["threshold"],
                bet["home_team"], bet["away_team"], home_score, away_score,
            )
            if result is None:
                logger.warning(
                    "Bet #%d (%s) needs manual settlement -- ambiguous result "
                    "(%s vs %s, %s, score %s-%s)",
                    bet["id"], bet["outcome_label"], bet["home_team"],
                    bet["away_team"], bet["bet_type"], home_score, away_score,
                )
                continue

            pnl = compute_pnl(bet["actual_stake"], bet["actual_odds_american"], result)
            db.settle_flagged_bet(
                bet["id"], result, pnl,
                final_home_score=home_score, final_away_score=away_score,
                settlement_source="odds_api_scores",
            )
            settled += 1
            logger.info("Settled bet #%d: %s (%s), pnl=$%.2f",
                        bet["id"], bet["outcome_label"], result, pnl)

    return settled
