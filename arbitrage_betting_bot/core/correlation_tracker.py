"""
Prevents correlated bets that could amplify losses.

Rules (in order):
  1. Same game:  already have an open position on this exact event.
  2. Same team:  already have an open position involving one of these teams.
  3. Daily cap:  already alerted MAX_DAILY_ALERTS opportunities today.
  4. Exposure:   BankrollManager cap on total / per-sport exposure.
"""
from __future__ import annotations

import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.value_detector import ValueOpportunity
from core.bankroll_manager import BankrollManager
from storage import db

logger = logging.getLogger(__name__)


class CorrelationTracker:
    def __init__(self, bankroll_manager: BankrollManager):
        self.bm = bankroll_manager

    def is_allowed(
        self, opp: ValueOpportunity, recommended_dollars: float
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        If allowed is False, reason explains why the bet was blocked.
        """
        event = opp.matched_event.odds_event
        home = event.home_team
        away = event.away_team
        sport = event.sport_key

        open_positions = db.get_open_positions(self.bm.is_paper)

        # Rule 1: same game
        for pos in open_positions:
            if pos["home_team"] == home and pos["away_team"] == away:
                return False, f"Already have an open position on {home} vs {away}"

        # Rule 2: same team
        for pos in open_positions:
            if pos["home_team"] in (home, away) or pos["away_team"] in (home, away):
                return (
                    False,
                    f"Correlated bet blocked — already exposed to "
                    f"{pos['home_team']} or {pos['away_team']}",
                )

        # Rule 3: daily alert cap
        alerts_today = db.count_alerts_today()
        if alerts_today >= config.MAX_DAILY_ALERTS:
            return (
                False,
                f"Daily alert cap reached ({alerts_today}/{config.MAX_DAILY_ALERTS})",
            )

        # Rule 4: bankroll exposure
        allowed, reason = self.bm.can_add_exposure(recommended_dollars, sport)
        if not allowed:
            return False, reason

        return True, "OK"
