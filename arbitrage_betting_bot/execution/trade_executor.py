"""
Executes a Kalshi order for a given ValueOpportunity.

Usage (from main.py):
    from execution.trade_executor import execute_trade
    order_id, status = execute_trade(opp, sizing)
"""
from __future__ import annotations

import logging
import re

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.value_detector import ValueOpportunity, Outcome
from core.kelly_calculator import BetSizing

logger = logging.getLogger(__name__)


# Outcomes whose edge is computed on the Kalshi YES side, so the bet must buy YES.
# Enumerated explicitly rather than left to a fallthrough: on 2026-08-22 BTTS, RFI and
# PLAYER were added as Outcome members without being listed here, dropped through to
# the AWAY branch, and were bet on NO -- the exact opposite of the side the edge was
# computed for. 10 positions and $13.95 went on before it was caught. Every one of
# those props prices Kalshi's YES against the sportsbook's matching side.
_YES_SIDE_OUTCOMES = frozenset({
    Outcome.DRAW, Outcome.OVER, Outcome.UNDER, Outcome.COVER,
    Outcome.BTTS, Outcome.RFI, Outcome.PLAYER,
})


def resolve_side(opp: ValueOpportunity) -> str:
    """Return the Kalshi side ('yes' or 'no') to bet for this opportunity.

    Raises on an unhandled Outcome. A new market type must not be able to silently
    inherit a side -- that is precisely how the BTTS/RFI/PLAYER bets ended up
    inverted, and nothing downstream noticed because a NO bet at a plausible price
    looks entirely normal.
    """
    me = opp.matched_event
    # NO_OVER = buying the NO (Under) side of an Over market
    if opp.outcome == Outcome.NO_OVER:
        return "no"
    if opp.outcome in _YES_SIDE_OUTCOMES:
        return "yes"
    if opp.outcome == Outcome.HOME:
        return me.kalshi_outcome or "yes"
    if opp.outcome == Outcome.AWAY:
        return "no" if (me.kalshi_outcome or "yes") == "yes" else "yes"
    raise ValueError(
        f"resolve_side has no case for {opp.outcome!r} — add it to _YES_SIDE_OUTCOMES "
        f"or handle it explicitly rather than letting it default to a side"
    )


def _resolve_club(label: str, home: str, away: str) -> str | None:
    """Which club a free-text label refers to, or None if it cannot be told."""
    from core.value_detector import _sb_team_scores

    home_score, away_score = _sb_team_scores(label, home, away)
    if home_score == away_score or (home_score == 0 and away_score == 0):
        return None
    return home if home_score > away_score else away


def _label_line(label: str) -> float | None:
    """The signed line trailing a label: 'Chicago Cubs -1.5' -> -1.5, 'Over 3.5' -> 3.5."""
    m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*$", label or "")
    return float(m.group(1)) if m else None


def verify_market_identity(opp: ValueOpportunity) -> str | None:
    """
    Last gate before real money: does the Kalshi market we are about to buy actually
    pay out on the outcome we priced? Returns a rejection reason, or None if it checks
    out.

    WHY THIS EXISTS SEPARATELY FROM THE MATCHER'S OWN GUARDS. Position #930 bought
    "Chicago WS wins by over 1.5 runs" while pricing "Chicago Cubs -1.5" -- the
    opposite team -- because a name lookup resolved a tie in favour of home. The
    ambiguity guard in value_detector._sb_team_match now refuses that specific case,
    but it only catches matches the matcher KNOWS are uncertain. A lookup that is
    confidently wrong (scores 80 against the wrong club, 25 against the right one)
    would pass it, log nothing, and place the bet.

    So this check validates the RESULT rather than the confidence of any one lookup:
    resolve OUR label and KALSHI'S OWN label independently, and refuse if they land on
    different clubs. Kalshi's yes_sub_title is authoritative about what the contract
    pays -- it is the one description in the whole pipeline that cannot be wrong about
    its own market.

    A rejection here is a bug somewhere upstream, not a normal skip, so callers should
    treat it as loud.
    """
    me = opp.matched_event
    km = me.kalshi_market
    ev = me.odds_event
    side = resolve_side(opp)

    # ── team-identified outcomes: the failure mode that motivated this ──────────
    if opp.outcome in (Outcome.HOME, Outcome.AWAY, Outcome.COVER):
        ours_label = re.sub(r"\s*[-+]?\d+(?:\.\d+)?\s*$", "", opp.team_name or "").strip()
        ours = _resolve_club(ours_label, ev.home_team, ev.away_team)
        if ours is None:
            return (f"cannot resolve our own label {opp.team_name!r} to either team "
                    f"in {ev.away_team} @ {ev.home_team}")

        theirs = _resolve_club(km.yes_team or "", ev.home_team, ev.away_team)
        if theirs is None:
            return (f"Kalshi's YES label {km.yes_team!r} does not unambiguously "
                    f"identify a team in {ev.away_team} @ {ev.home_team}")

        # Buying YES pays the YES team; buying NO pays the other one.
        other = ev.away_team if theirs == ev.home_team else ev.home_team
        pays = theirs if side == "yes" else other
        if ours != pays:
            return (f"we priced {ours!r} but buying {side.upper()} on {km.ticker} pays "
                    f"{pays!r} (Kalshi YES = {km.yes_team!r})")

        # Spread lines must agree in magnitude too: Kalshi's threshold is always the
        # covering margin, our label carries the sportsbook's signed handicap.
        if opp.outcome == Outcome.COVER and km.threshold is not None:
            line = _label_line(opp.team_name)
            if line is not None and abs(abs(line) - abs(km.threshold)) > 0.01:
                return (f"line mismatch: priced {line:+g} but {km.ticker} is "
                        f"{km.threshold:+g}")

    # ── totals: direction and line must both match ──────────────────────────────
    elif opp.outcome in (Outcome.OVER, Outcome.UNDER, Outcome.NO_OVER):
        if km.threshold is not None:
            line = _label_line(opp.team_name)
            if line is not None and abs(line - km.threshold) > 0.01:
                return (f"totals line mismatch: priced {line:g} but {km.ticker} is "
                        f"{km.threshold:g}")
        label = (opp.team_name or "").lower()
        yes_label = (km.yes_team or "").lower()
        # Buying YES on an "Over" market must mean we priced an Over, and so on.
        if "over" in yes_label and side == "yes" and "over" not in label:
            return (f"direction mismatch: {km.ticker} YES is {km.yes_team!r} but we "
                    f"priced {opp.team_name!r}")
        if "under" in yes_label and side == "yes" and "under" not in label:
            return (f"direction mismatch: {km.ticker} YES is {km.yes_team!r} but we "
                    f"priced {opp.team_name!r}")

    # ── binary props: the side must be YES, and the market must be the right one ──
    elif opp.outcome in (Outcome.BTTS, Outcome.RFI, Outcome.PLAYER):
        if side != "yes":
            return (f"{opp.outcome.value} edge is computed on Kalshi's YES side but "
                    f"this order buys {side.upper()}")
        if opp.outcome == Outcome.BTTS and "both teams" not in (km.yes_team or "").lower():
            return f"priced BTTS but {km.ticker} YES is {km.yes_team!r}"
        if opp.outcome == Outcome.PLAYER:
            who = getattr(km, "participant", None)
            if not who or who.lower() not in (opp.team_name or "").lower():
                return (f"player mismatch: priced {opp.team_name!r} but market is for "
                        f"{who!r}")

    # ── draw/tie: the market must actually be the tie market ────────────────────
    elif opp.outcome == Outcome.DRAW:
        yes_label = (km.yes_team or "").lower()
        if not any(w in yes_label for w in ("tie", "draw")):
            return (f"priced a draw but {km.ticker} YES is {km.yes_team!r}")

    return None


def execute_trade(opp: ValueOpportunity, sizing: BetSizing) -> tuple[str, str, str, str, float, str, float]:
    """
    Place a live Kalshi order for the given opportunity.

    Returns (order_id, execution_status, side, failure_reason, actual_stake, fill_type, fee_paid).
    execution_status: "submitted" | "failed"
    side: "yes" | "no"
    failure_reason: empty string on success, human-readable error on failure
    actual_stake: dollars actually filled (0.0 on failure)
    fill_type: "maker" | "taker" on success (derived from fee_paid) | "" on failure
    fee_paid: actual dollars Kalshi charged for this fill (0.0 on failure)
    """
    from execution import kalshi_executor

    me = opp.matched_event
    km = me.kalshi_market
    side = resolve_side(opp)

    # Last gate before real money. A failure here means the market we are about to buy
    # does not pay out on the outcome we priced -- an upstream bug, not a normal skip,
    # so it is logged at ERROR and recorded for investigation rather than passed over.
    mismatch = verify_market_identity(opp)
    if mismatch:
        logger.error(
            "REFUSING ORDER on %s — market identity check failed: %s",
            km.ticker, mismatch,
        )
        try:
            from storage.db import log_ambiguous_match
            log_ambiguous_match(
                context="preorder_identity_check",
                kalshi_ticker=km.ticker,
                kalshi_name=km.yes_team or "",
                home_team=me.odds_event.home_team,
                away_team=me.odds_event.away_team,
                sport=me.odds_event.sport_key,
                bet_type=km.bet_type,
            )
        except Exception:
            pass
        return "", "failed", side, f"market identity check failed: {mismatch}", 0.0, "", 0.0

    order_id, status, reason, actual_stake, fill_type, fee_paid = kalshi_executor.place_order(
        ticker=km.ticker,
        side=side,
        stake_dollars=sizing.recommended_dollars,
        market_price=opp.market_price,
        kalshi_spread=km.spread,
        commence_time=me.odds_event.commence_time,
        edge=opp.edge,
        maker_only=opp.maker_only,
    )
    return order_id, status, side, reason, actual_stake, fill_type, fee_paid
