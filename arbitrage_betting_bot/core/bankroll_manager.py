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

    @property
    def mm_exposure(self) -> float:
        positions = db.get_open_positions(self.is_paper)
        return sum(float(p["stake"]) for p in positions if p["strategy"] == "market_making")

    def can_add_exposure(self, additional: float, sport: str, is_mm: bool = False) -> tuple[bool, str]:
        """
        Check whether adding `additional` dollars more exposure is allowed.
        Returns (allowed, reason).

        is_mm: also enforce MAX_MM_EXPOSURE_PCT — a sub-cap *within* the same shared
        total/sport caps below, not a separate bankroll. Market making shares every
        other exposure limit with the directional strategy; this just stops the new,
        unvalidated execution mode from occupying too much of that shared pool at once.
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

        if is_mm:
            new_mm_exp = self.mm_exposure + additional
            # Epsilon tolerance: mm_clip_size() rounds to the nearest cent, which can
            # round UP a clip meant to sit exactly at the cap (e.g. $8.726 -> $8.73)
            # just past it — the same class of float-boundary issue fixed elsewhere
            # in this codebase for maker_only (see git history). Without this, a
            # clip sized to fit the cap can still get rejected by a fraction of a
            # cent. Half a cent, expressed relative to bankroll, covers the maximum
            # possible cent-rounding error regardless of bankroll size.
            _EPS = 0.005 / self.bankroll
            if new_mm_exp / self.bankroll > config.MM_MAX_EXPOSURE_PCT + _EPS:
                return (
                    False,
                    f"Market-making exposure would reach {new_mm_exp / self.bankroll * 100:.0f}% "
                    f"(max {config.MM_MAX_EXPOSURE_PCT * 100:.0f}%)",
                )

        return True, "OK"

    def snapshot(self) -> None:
        db.snapshot_bankroll(self.bankroll, self.total_at_risk)
