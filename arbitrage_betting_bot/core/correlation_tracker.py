"""
Prevents correlated bets that could amplify losses.

Rules (in order):
  1. Same game:  already have an open position on this exact event.
  2. Same team:  already have an open position involving one of these teams.
  3. Exposure:   BankrollManager cap on total / per-sport exposure.
"""
from __future__ import annotations

import logging
import time

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
        self._failed_cooldowns: dict[str, float] = {}  # ticker → unix timestamp of failure

    def record_failure(self, ticker: str) -> None:
        """Record that a bet on this ticker failed. Suppresses retries for FAILED_BET_COOLDOWN_SECONDS."""
        self._failed_cooldowns[ticker] = time.time()
        logger.debug("Cooldown started for %s (%dh)", ticker, config.FAILED_BET_COOLDOWN_SECONDS // 3600)

    def is_allowed(
        self,
        opp: ValueOpportunity,
        recommended_dollars: float,
        arb_game_keys: set[tuple[str, str]] | None = None,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        If allowed is False, reason explains why the bet was blocked.

        arb_game_keys: set of (home_team, away_team) tuples for games where both
        sides have positive edge in the current scan (true arbitrage). The same-game
        and same-team correlation blocks are relaxed for these pairs so both legs
        can be placed simultaneously.
        """
        event = opp.matched_event.odds_event
        home = event.home_team
        away = event.away_team
        sport = event.sport_key
        is_arb = arb_game_keys is not None and (home, away) in arb_game_keys

        open_positions = db.get_open_positions(self.bm.is_paper)
        ticker = opp.matched_event.kalshi_market.ticker

        # Rule 0: same Kalshi ticker — never bet the same market twice
        for pos in open_positions:
            if pos["market_ticker"] == ticker:
                return False, f"Already have an open position on {ticker}"

        # Rule 0b: failed-bet cooldown — skip recently failed tickers
        failed_at = self._failed_cooldowns.get(ticker, 0)
        if failed_at:
            retry_in = failed_at + config.FAILED_BET_COOLDOWN_SECONDS - time.time()
            if retry_in > 0:
                return False, f"Failed attempt cooldown — retry in {int(retry_in / 60)}min"
            else:
                del self._failed_cooldowns[ticker]

        # Rule 1: same game — skip for arb pairs (both legs placed in same scan)
        if not is_arb:
            for pos in open_positions:
                if pos["home_team"] == home and pos["away_team"] == away:
                    return False, f"Already have an open position on {home} vs {away}"

        # Rule 2: same team — skip for arb pairs
        if not is_arb:
            for pos in open_positions:
                if pos["home_team"] in (home, away) or pos["away_team"] in (home, away):
                    return (
                        False,
                        f"Correlated bet blocked — already exposed to "
                        f"{pos['home_team']} or {pos['away_team']}",
                    )

        # Rule 3: bankroll exposure (always enforced, even for arb)
        allowed, reason = self.bm.can_add_exposure(recommended_dollars, sport)
        if not allowed:
            return False, reason

        return True, "OK"
