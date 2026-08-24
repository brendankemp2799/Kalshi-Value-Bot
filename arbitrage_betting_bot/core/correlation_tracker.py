"""
Prevents correlated bets that could amplify losses.

Rules (in order):
  1. Same game:  bets sharing a factor (scoring / result) must not together
     exceed one max-size bet; the game overall must not exceed two. Both are
     multiples of MAX_PCT_BANKROLL — see config for why that relationship matters.
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


# Which latent factor each bet type resolves on. Bets sharing a factor are the ones
# Kelly mis-sized by treating them as independent: Over 8.5, First Inning Run and a
# hitter's total bases are three bets on "this game scores".
_BET_FACTOR: dict[str, str] = {
    "h2h":         "result",
    "spread":      "result",
    "totals":      "scoring",
    "btts":        "scoring",
    "rfi":         "scoring",
    "player_prop": "scoring",
}


def bet_factor(bet_type: str) -> str:
    """The factor a bet type loads on.

    An unmapped type gets a bucket of its OWN rather than silently joining an existing
    one -- a new market type must not inherit a correlation assumption by accident,
    which is the same failure that put 11 positions on the wrong side on 2026-08-22.
    tests/test_correlation_rules.py pins that every enabled bet type is mapped here.
    """
    return _BET_FACTOR.get(bet_type) or f"unmapped:{bet_type}"


def _cannot_both_win(new_bet_type: str, new_team: str, new_side: str, pos) -> bool:
    """True if these two bets are mutually exclusive outcomes of one 3-way market.

    Two YES bets on different runners of the same match -- Liverpool win vs Newcastle
    win vs Draw -- cannot both pay. Stacking them is not the correlated over-betting
    the factor cap exists to stop, so they do not accumulate against it. They CAN all
    lose together, so they still accumulate against the game cap.

    Side matters: NO on Newcastle means "Newcastle does not win", which OVERLAPS with
    Liverpool YES rather than excluding it. Only YES-vs-YES qualifies.
    """
    if new_bet_type != "h2h" or pos["bet_type"] != "h2h":
        return False
    if new_side != "yes" or (pos["side"] or "yes") != "yes":
        return False
    return (new_team or "").strip().lower() != (pos["team_name"] or "").strip().lower()


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
        pending_game_stakes: dict[tuple[str, str], float] | None = None,
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
        #
        # Asked of every position we have EVER filled on the ticker, not just the open
        # ones. Scoped to open positions, this rule freed the ticker the moment a
        # position closed, and the next scan re-bought the identical market at the
        # identical price -- the edge had not moved, so the same opportunity was still
        # sitting there. KXMLBBTTS-26AUG22SJMIN-BTTS went through that loop four times
        # on 2026-08-21..22 for -$3.91, each re-entry following a stop-loss exit.
        #
        # Stop-losses are off as of 2026-08-23, which removes the only driver we have
        # actually observed, but not the hole: any early close reopens it. A Kalshi
        # game ticker names one fixture at one start time and never recurs, so this
        # costs no legitimate volume.
        held_by = db.strategies_ever_filled_on(ticker, self.bm.is_paper)
        if held_by and not (is_mm and held_by == {"market_making"}):
            return False, f"Already bet {ticker} (held by {'/'.join(sorted(held_by))})"

        # Rule 0b: failed-bet cooldown — skip recently failed tickers
        failed_at = self._failed_cooldowns.get(ticker, 0)
        if failed_at:
            retry_in = failed_at + config.FAILED_BET_COOLDOWN_SECONDS - time.time()
            if retry_in > 0:
                return False, f"Failed attempt cooldown — retry in {int(retry_in / 60)}min"
            else:
                del self._failed_cooldowns[ticker]

        # Rule 1: same game — correlated bets must not, together, exceed what a
        # single bet was allowed to be.
        #
        # Both caps derive from MAX_PCT_BANKROLL (see the config block), so a max-size
        # single bet always fits. The predecessor was a flat 2%-of-bankroll figure that
        # was 2.5x SMALLER than the largest permitted single bet -- so one big bet
        # locked the game against everything else, while three small correlated bets
        # got refused.
        if not is_special:
            same_game = [dict(p) for p in open_positions
                         if p["home_team"] == home and p["away_team"] == away]
            if pending_game_stakes:
                same_game += pending_game_stakes.get((home, away), [])

            if same_game:
                from execution.trade_executor import resolve_side

                km = opp.matched_event.kalshi_market
                new_bet_type = km.bet_type
                new_factor = bet_factor(new_bet_type)
                new_side = resolve_side(opp)

                max_bet = config.MAX_PCT_BANKROLL * self.bm.bankroll
                factor_cap = config.MAX_FACTOR_EXPOSURE_MULTIPLE * max_bet
                game_cap = config.MAX_GAME_EXPOSURE_MULTIPLE * max_bet

                # Factor cap: the correlation control.
                factor_used = sum(
                    (p["stake"] or 0.0) for p in same_game
                    if bet_factor(p["bet_type"]) == new_factor
                    and not _cannot_both_win(new_bet_type, opp.team_name, new_side, p)
                )
                if factor_used > 0 and factor_used + recommended_dollars > factor_cap:
                    return (
                        False,
                        f"{new_factor} exposure on {home} vs {away} would reach "
                        f"${factor_used + recommended_dollars:.2f} "
                        f"(max ${factor_cap:.2f} — correlated bets count as one)",
                    )

                # Game cap: the concentration control. Everything counts, including
                # mutually exclusive outcomes, which can all lose together.
                game_used = sum((p["stake"] or 0.0) for p in same_game)
                if game_used > 0 and game_used + recommended_dollars > game_cap:
                    return (
                        False,
                        f"Game exposure would reach "
                        f"${game_used + recommended_dollars:.2f} on {home} vs {away} "
                        f"(max ${game_cap:.2f})",
                    )

        # Rule 2: same team, same day — skip for arb pairs / MM. Scoped to the same
        # UTC calendar date rather than "any open position on this team, indefinitely" —
        # a team's games days apart don't carry meaningful correlated risk, and the
        # unscoped version was found blocking a real, qualifying bet for 2+ days
        # against a completely unrelated game. If a position's commence_time can't
        # be determined, fall back to the old conservative behavior (block).
        if not is_special:
            event_date = event.commence_time.astimezone(timezone.utc).date()
            for pos in open_positions:
                if pos["home_team"] == home and pos["away_team"] == away:
                    # Same game -- Rule 1's dollar cap governs this, not Rule 2.
                    # Rule 2 exists for a DIFFERENT game involving a shared team, and
                    # its team test matches both clubs of the same fixture, so without
                    # this it blocks every same-game second bet on the same date --
                    # i.e. always. That made Rule 1 pure redundancy: the count-based
                    # version never had to fire to get the same outcome, and the
                    # dollar cap that replaced it would have been inert.
                    continue
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
