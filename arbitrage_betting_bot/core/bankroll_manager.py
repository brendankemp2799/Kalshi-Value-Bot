"""
Tracks bankroll, total exposure, and per-sport exposure.
Enforces hard caps before any alert is issued.
"""
from __future__ import annotations

import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from storage import db

logger = logging.getLogger(__name__)


class BankrollManager:
    def __init__(self, bankroll: float = config.BANKROLL, is_paper: bool = False):
        self.bankroll = bankroll
        self.is_paper = is_paper

    @property
    def total_at_risk(self) -> float:
        positions = db.get_open_positions(self.is_paper)
        return sum(float(p["stake"]) for p in positions)

    def sport_exposure(self, sport: str) -> float:
        positions = db.get_open_positions(self.is_paper)
        return sum(float(p["stake"]) for p in positions if p["sport"] == sport)

    def can_add_exposure(self, additional: float, sport: str) -> tuple[bool, str]:
        """
        Check whether adding `additional` dollars more exposure is allowed.
        Returns (allowed, reason).
        """
        new_total = self.total_at_risk + additional
        if new_total / self.bankroll > config.MAX_TOTAL_EXPOSURE_PCT:
            return (
                False,
                f"Total exposure would reach {new_total / self.bankroll * 100:.0f}% "
                f"(max {config.MAX_TOTAL_EXPOSURE_PCT * 100:.0f}%)",
            )

        new_sport_exp = self.sport_exposure(sport) + additional
        if new_sport_exp / self.bankroll > config.MAX_SPORT_EXPOSURE_PCT:
            return (
                False,
                f"{sport} exposure would reach {new_sport_exp / self.bankroll * 100:.0f}% "
                f"(max {config.MAX_SPORT_EXPOSURE_PCT * 100:.0f}%)",
            )

        return True, "OK"

    def snapshot(self) -> None:
        db.snapshot_bankroll(self.bankroll, self.total_at_risk)
