"""
Market-making execution mode.

Not a separate bot: this evaluates the exact same matched markets the directional
value-edge strategy already scans, using the exact same sportsbook-consensus fair
value (see core/value_detector.py's mm_candidates output), for the subset of markets
whose Kalshi spread is too wide to cross directionally. Instead of skipping them, it
rests a YES-bid and a NO-bid inside the spread and lets other traders cross to us,
capturing the spread net of Kalshi's maker fee.

Fills are recorded into the same `positions` table as directional bets
(positions.strategy='market_making'), so they're automatically covered by the
existing trailing-stop / stop-loss risk management with no separate exit-risk code,
and they count against the same shared bankroll exposure caps (see
core/bankroll_manager.py's MM_MAX_EXPOSURE_PCT sub-cap).

See config.py's "Market Making" block for parameters and mm_backtest.py for the
empirical validation those were calibrated from. Master switch:
config.ENABLE_MARKET_MAKING (defaults False).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.value_detector import Outcome, ValueOpportunity, _kalshi_url
from core.kelly_calculator import mm_clip_size
from core.correlation_tracker import CorrelationTracker
from core.bankroll_manager import BankrollManager
from data.kalshi_client import KalshiMarket
from storage import db

logger = logging.getLogger(__name__)

# Process-local record of currently-resting (unfilled) quote orders, keyed by
# ticker → {"yes_order_id": str | None, "no_order_id": str | None}. Not persisted:
# if the bot restarts, any real orders still resting on Kalshi become untracked
# here (though still visible/cancellable manually on Kalshi, and still subject to
# the same reconciliation pass everything else goes through). Acceptable for a v1
# that ships inert by default — same category of limitation as the module-level
# series cache in data/kalshi_client.py.
_resting_quotes: dict[str, dict] = {}


class MMActionKind(str, Enum):
    NONE = "none"
    QUOTE = "quote"


@dataclass
class MMAction:
    kind: MMActionKind
    reservation_price: float | None = None
    yes_bid_price: float | None = None   # price of YES we'd buy at
    no_bid_price: float | None = None    # price of NO we'd buy at
    clip_dollars: float | None = None


def _net_inventory_contracts(ticker: str, is_paper: bool) -> float:
    """Net YES-equivalent contracts already held on this ticker from prior MM fills
    (positive = net long YES, negative = net long NO). Used to skew the reservation
    price so a filled leg makes flattening relatively more attractive to quote next,
    rather than piling further onto a one-sided position (classic Avellaneda-Stoikov
    inventory adjustment)."""
    net = 0.0
    for pos in db.get_open_positions(is_paper=is_paper):
        if pos["market_ticker"] != ticker or pos["strategy"] != "market_making":
            continue
        contracts = pos["stake"] / pos["market_price"] if pos["market_price"] else 0.0
        net += contracts if pos["side"] == "yes" else -contracts
    return net


def evaluate_mm_candidate(candidate: dict, net_inventory_contracts: float = 0.0) -> MMAction:
    """
    Pure decision function — no side effects, no I/O. `candidate` is one entry from
    core/value_detector.py::detect_value()'s mm_candidates output.
    """
    consensus = candidate.get("consensus_prob")
    spread = candidate.get("kalshi_spread")
    if consensus is None or spread is None:
        return MMAction(kind=MMActionKind.NONE)

    lo, hi = config.MM_FAIR_VALUE_BAND
    if not (lo <= consensus <= hi):
        return MMAction(kind=MMActionKind.NONE)

    if spread < config.MM_MIN_SPREAD_TO_QUOTE:
        return MMAction(kind=MMActionKind.NONE)

    # Inventory skew: 1c per net contract held, capped at +/-5c, shifting the
    # reservation price away from raw consensus so quotes lean toward flattening
    # existing exposure rather than adding to it.
    skew = max(-0.05, min(0.05, -0.01 * net_inventory_contracts))
    reservation = max(0.02, min(0.98, consensus + skew))

    half = config.MM_QUOTE_HALF_SPREAD_FRACTION * spread
    yes_bid_price = round(max(0.01, reservation - half), 2)
    no_bid_price = round(max(0.01, 1.0 - min(0.99, reservation + half)), 2)

    clip = mm_clip_size(spread)
    if clip <= 0:
        return MMAction(kind=MMActionKind.NONE)

    return MMAction(
        kind=MMActionKind.QUOTE,
        reservation_price=reservation,
        yes_bid_price=yes_bid_price,
        no_bid_price=no_bid_price,
        clip_dollars=clip,
    )


def _candidate_opportunity(candidate: dict) -> ValueOpportunity:
    """Build a minimal ValueOpportunity so CorrelationTracker.is_allowed() — written
    for the directional strategy — can be reused as-is for MM's pre-trade checks.
    Only matched_event/team_name/consensus_prob are actually read by is_allowed();
    outcome/market_price/edge are placeholders, never used for MM's own sizing."""
    me = candidate["matched_event"]
    km = me.kalshi_market
    return ValueOpportunity(
        matched_event=me,
        outcome=Outcome.HOME,
        team_name=candidate["team_name"],
        consensus_prob=candidate["consensus_prob"],
        market_price=candidate["consensus_prob"],
        edge=0.0,
        market_url=_kalshi_url(km.ticker, km.event_ticker),
        bookmaker_count=candidate.get("bookmaker_count", 0),
        consensus_std=candidate.get("consensus_std", 0.0),
    )


def _record_fill(candidate: dict, side: str, price: float, filled: float,
                  fee_paid: float, is_paper: bool) -> None:
    me = candidate["matched_event"]
    event = me.odds_event
    km = me.kalshi_market
    stake = round(filled * price, 2)
    db.add_position(
        sport=event.sport_key,
        home_team=event.home_team,
        away_team=event.away_team,
        team_name=candidate["team_name"],
        platform="Kalshi",
        stake=stake,
        market_price=price,
        is_paper=is_paper,
        execution_status="paper" if is_paper else "submitted",
        market_ticker=km.ticker,
        side=side,
        edge=0.0,
        bookmaker_count=candidate.get("bookmaker_count"),
        consensus_std=candidate.get("consensus_std"),
        kalshi_spread=km.spread,
        commence_time=event.commence_time.isoformat(),
        bet_type=km.bet_type,
        threshold=km.threshold,
        fill_type="maker",
        entry_fee_paid=fee_paid,
        strategy="market_making",
    )
    mode_tag = "[PAPER]" if is_paper else "[LIVE]"
    logger.info(
        "%s MM fill: %s %s %g contracts @ %.4f  stake=$%.2f  fee=$%.4f",
        mode_tag, side.upper(), km.ticker, filled, price, stake, fee_paid,
    )


def run_mm_tick(
    mm_candidates: list[dict],
    fresh_kalshi: dict[str, KalshiMarket],
    tracker: CorrelationTracker,
    bm: BankrollManager,
    is_paper: bool,
) -> int:
    """
    One market-making requote pass — called from the fast, Kalshi-only tick in
    main.py::_run_variable_loop(), independent of the Odds-API scan cadence.

    mm_candidates: output of the most recent due-sport scan's
        detect_value(..., mm_candidates=[...]) call (reused as-is — no new
        sportsbook fetch here, zero incremental Odds API cost).
    fresh_kalshi: ticker -> KalshiMarket from a fresh, free
        kalshi_client.fetch_sports_markets() call this tick, so quotes reprice
        against the live book even between due-sport scans.

    Returns the number of legs filled this tick.
    """
    from execution.kalshi_executor import place_resting_quote, cancel_quote

    filled_count = 0
    seen_tickers: set[str] = set()

    for candidate in mm_candidates:
        km = candidate["matched_event"].kalshi_market
        ticker = km.ticker
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

        live_km = fresh_kalshi.get(ticker, km)
        candidate = {**candidate, "kalshi_spread": live_km.spread}

        # Cancel any quote left resting from the previous tick before repricing —
        # cheap and avoids ever having two stale + fresh quotes resting at once.
        prior = _resting_quotes.pop(ticker, None)
        if prior and not is_paper:
            for oid in (prior.get("yes_order_id"), prior.get("no_order_id")):
                if oid:
                    cancel_quote(oid)

        net_inventory = _net_inventory_contracts(ticker, is_paper)
        action = evaluate_mm_candidate(candidate, net_inventory)
        if action.kind == MMActionKind.NONE:
            continue

        opp = _candidate_opportunity(candidate)
        allowed, reason = tracker.is_allowed(opp, action.clip_dollars, is_mm=True)
        if not allowed:
            logger.debug("MM quote blocked for %s: %s", ticker, reason)
            continue

        yes_count = max(1, math.floor(action.clip_dollars / action.yes_bid_price))
        no_count = max(1, math.floor(action.clip_dollars / action.no_bid_price))

        if is_paper:
            # Paper mode never places real orders. A quote is treated as filled
            # this tick only if the LIVE top-of-book already crosses through our
            # intended price — i.e., if we'd been resting there, we'd already be
            # filled. This makes paper mode a faithful (if conservative — it can't
            # see fills that happen mid-interval, only at each tick's snapshot)
            # preview of live behavior instead of assuming instant fills the way
            # the directional strategy's paper mode does.
            if live_km.yes_ask > 0 and live_km.yes_ask <= action.yes_bid_price:
                _record_fill(candidate, "yes", action.yes_bid_price, yes_count, 0.0, True)
                filled_count += 1
            if live_km.yes_bid > 0 and (1.0 - live_km.yes_bid) <= action.no_bid_price:
                _record_fill(candidate, "no", action.no_bid_price, no_count, 0.0, True)
                filled_count += 1
            continue

        yes_order_id, yes_filled, yes_fee = place_resting_quote(ticker, "yes", action.yes_bid_price, yes_count)
        no_order_id, no_filled, no_fee = place_resting_quote(ticker, "no", action.no_bid_price, no_count)

        _resting_quotes[ticker] = {
            "yes_order_id": yes_order_id if yes_filled < yes_count else None,
            "no_order_id": no_order_id if no_filled < no_count else None,
        }

        if yes_filled > 0:
            _record_fill(candidate, "yes", action.yes_bid_price, yes_filled, yes_fee, False)
            filled_count += 1
        if no_filled > 0:
            _record_fill(candidate, "no", action.no_bid_price, no_filled, no_fee, False)
            filled_count += 1

    return filled_count
