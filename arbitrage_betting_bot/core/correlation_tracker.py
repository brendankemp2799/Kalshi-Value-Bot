"""
Prevents correlated bets that could amplify losses.

Rules (in order):
  1. Same game:  already have an open position on this exact event.
  2. Same team, same day: already have an open position involving one of these
     teams, with a game on the same UTC calendar date.
  3. Exposure:   BankrollManager cap on total / per-sport exposure.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

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
        is_mm: bool = False,
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        If allowed is False, reason explains why the bet was blocked.

        arb_game_keys: set of (home_team, away_team) tuples for games where both
        sides have positive edge in the current scan (true arbitrage). The same-game
        and same-team correlation blocks are relaxed for these pairs so both legs
        can be placed simultaneously.

        is_mm: True when evaluating a market-making quote leg rather than a
        directional bet. Same-game/same-team correlation (Rules 1-2) is relaxed for
        the same reason as arb — intentionally holding both sides of one market is
        the point, not correlated risk. Rule 0 (same ticker) is relaxed only when the
        existing open position on that ticker is itself market-making inventory (our
        own other leg or a prior fill we're layering/hedging against) — it still
        blocks if the existing position is a directional value_edge bet, so market
        making never quotes on top of a market the directional strategy already has
        a stake in.
        """
        event = opp.matched_event.odds_event
        home = event.home_team
        away = event.away_team
        sport = event.sport_key
        is_arb = arb_game_keys is not None and (home, away) in arb_game_keys
        is_special = is_arb or is_mm

        open_positions = db.get_open_positions(self.bm.is_paper)
        ticker = opp.matched_event.kalshi_market.ticker

        # Rule 0: same Kalshi ticker — never bet the same market twice, except MM
        # laying a quote alongside/against its own existing MM inventory on that ticker.
        for pos in open_positions:
            if pos["market_ticker"] == ticker:
                if is_mm and pos["strategy"] == "market_making":
                    continue
                return False, f"Already have an open position on {ticker}"

        # Rule 0b: failed-bet cooldown — skip recently failed tickers
        failed_at = self._failed_cooldowns.get(ticker, 0)
        if failed_at:
            retry_in = failed_at + config.FAILED_BET_COOLDOWN_SECONDS - time.time()
            if retry_in > 0:
                return False, f"Failed attempt cooldown — retry in {int(retry_in / 60)}min"
            else:
                del self._failed_cooldowns[ticker]

        # Rule 1: same game — skip for arb pairs / MM (both legs placed in same scan)
        if not is_special:
            for pos in open_positions:
                if pos["home_team"] == home and pos["away_team"] == away:
                    return False, f"Already have an open position on {home} vs {away}"

        # Rule 2: same team, same day — skip for arb pairs / MM. Scoped to the same
        # UTC calendar date rather than "any open position on this team, indefinitely" —
        # a team's games days apart don't carry meaningful correlated risk, and the
        # unscoped version was found blocking a real, qualifying bet for 2+ days
        # against a completely unrelated game. If a position's commence_time can't
        # be determined, fall back to the old conservative behavior (block).
        if not is_special:
            event_date = event.commence_time.astimezone(timezone.utc).date()
            for pos in open_positions:
                if pos["home_team"] not in (home, away) and pos["away_team"] not in (home, away):
                    continue
                same_day = True
                pos_commence = pos["commence_time"]
                if pos_commence:
                    try:
                        pos_dt = datetime.fromisoformat(pos_commence)
                        if pos_dt.tzinfo is None:
                            pos_dt = pos_dt.replace(tzinfo=timezone.utc)
                        same_day = pos_dt.astimezone(timezone.utc).date() == event_date
                    except ValueError:
                        same_day = True
                if same_day:
                    return (
                        False,
                        f"Correlated bet blocked — already exposed to "
                        f"{pos['home_team']} or {pos['away_team']} on the same day",
                    )

        # Rule 3: bankroll exposure (always enforced, even for arb/MM)
        allowed, reason = self.bm.can_add_exposure(recommended_dollars, sport, is_mm=is_mm)
        if not allowed:
            return False, reason

        return True, "OK"
