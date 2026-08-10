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

# Process-local record of currently-resting (unfilled-as-of-last-tick) quote
# orders, keyed by ticker → {"yes": {"order_id", "price", "count"} | None,
# "no": {...} | None}. Not persisted: if the bot restarts, any real orders still
# resting on Kalshi become untracked here (though still visible/cancellable
# manually on Kalshi, and still subject to the same reconciliation pass
# everything else goes through) — cancel any resting orders manually before
# restarting the process while MM is enabled to avoid orphaned duplicate quotes.
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


def _resting_notional() -> float:
    """Total notional currently resting (placed, unfilled) across every leg in
    _resting_quotes. BankrollManager.mm_exposure only sums FILLED positions from
    the DB — it has no visibility into how much is simply resting on Kalshi's book
    right now. A single tick can legitimately evaluate dozens of candidates, and
    each one only ever got checked against the exposure cap in isolation, so
    nothing stopped the SUM of everything resting at once from far exceeding the
    intended cap even though no single candidate ever did on its own."""
    total = 0.0
    for legs in _resting_quotes.values():
        for leg in (legs.get("yes"), legs.get("no")):
            if leg:
                total += leg["price"] * leg["count"]
    return total


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


def evaluate_mm_candidate(candidate: dict, net_inventory_contracts: float = 0.0,
                           bankroll: float = config.BANKROLL) -> MMAction:
    """
    Pure decision function — no side effects, no I/O. `candidate` is one entry from
    core/value_detector.py::detect_value()'s mm_candidates output.

    bankroll: the account's real available balance. mm_clip_size()'s own
    bankroll-percentage cap defaults to config.BANKROLL (a static $1000 fallback,
    not the real live balance) if not passed explicitly — run_mm_tick() always
    passes the real bm.bankroll here so clip sizes actually shrink to fit whatever
    the account's real size is, instead of being sized as if the bankroll were
    always $1000 and then getting blocked downstream by BankrollManager's separate,
    correctly-real-balance-aware exposure check.
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

    clip = mm_clip_size(spread, bankroll=bankroll)
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
                  fee_paid: float, is_paper: bool, order_id: str = "") -> None:
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
        order_id=order_id,
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

    Before requoting, checks whether the prior tick's resting order(s) filled in
    the meantime (get_order_status) and records any such fill before cancelling
    the remainder — the normal way a maker quote fills is a counterparty crossing
    it later, not instantly at placement, so this can't be skipped. Also re-checks
    the live spread against the directional strategy's own max_kalshi_spread
    threshold (not just MM_MIN_SPREAD_TO_QUOTE, calibrated to the same value) so a
    cached-as-too-wide candidate whose spread has since narrowed stops being
    quoted once it's back in directionally-tradeable territory.

    Tracks a running total of filled-plus-resting notional across every candidate
    processed this tick (starting from BankrollManager.mm_exposure + whatever was
    already resting from the prior tick) and stops placing new quotes once it
    would push that total past MM_MAX_EXPOSURE_PCT of bankroll — the per-candidate
    check in CorrelationTracker.is_allowed() only ever sees one candidate's clip in
    isolation, so on its own it can't prevent many simultaneously-resting
    candidates from collectively exceeding the cap even though none of them do
    individually.

    Returns the number of legs filled this tick.
    """
    from execution.kalshi_executor import (
        place_resting_quote, cancel_quote, get_order_status, order_fee_paid,
    )

    filled_count = 0
    seen_tickers: set[str] = set()
    pending_notional = _resting_notional()  # carried over from the prior tick
    cap_dollars = config.MM_MAX_EXPOSURE_PCT * bm.bankroll

    for candidate in mm_candidates:
        km = candidate["matched_event"].kalshi_market
        ticker = km.ticker
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

        live_km = fresh_kalshi.get(ticker, km)
        candidate = {**candidate, "kalshi_spread": live_km.spread}

        # Before touching whatever was resting from the previous tick, check
        # whether a real counterparty filled it in the meantime — the normal way
        # a maker quote gets filled, not the immediate-at-placement edge case.
        # Missing this meant real fills could go completely unrecorded (no
        # bankroll accounting, no stop-loss, no correlation tracking) until an
        # eventual reconciliation-mismatch log, with no automatic remediation.
        prior = _resting_quotes.pop(ticker, None)
        if prior and not is_paper:
            for side, leg in (("yes", prior.get("yes")), ("no", prior.get("no"))):
                if not leg:
                    continue
                # This leg's old notional is being resolved one way or another
                # (filled and moved into bm.mm_exposure, or cancelled) — either
                # way it's coming out of the "still just resting" running total.
                pending_notional -= leg["price"] * leg["count"]
                status = get_order_status(leg["order_id"])
                filled = float(status.get("fill_count_fp", 0) or 0) if status else 0.0
                if filled > 0:
                    fee = order_fee_paid(leg["order_id"])
                    _record_fill(candidate, side, leg["price"], filled, fee, False,
                                 order_id=leg["order_id"])
                    filled_count += 1
                if filled < leg["count"]:
                    cancel_quote(leg["order_id"])

        # Re-check the live spread against the SAME threshold the directional
        # strategy uses to decide a market is too wide to cross — not just
        # MM_MIN_SPREAD_TO_QUOTE, which is calibrated to the same value and so
        # doesn't by itself stop MM from continuing to quote a market whose
        # spread has narrowed back into directionally-tradeable territory since
        # this candidate was cached (mm_candidates only refreshes on full
        # due-scans, up to 45 min apart; this tick runs every
        # config.MM_INTERVAL_SECONDS against the live book).
        max_spread = config.quality_filters(km.bet_type, is_draw=(km.bet_type == "h2h" and candidate["team_name"] == "Draw"))["max_kalshi_spread"]
        if live_km.spread <= max_spread:
            continue

        net_inventory = _net_inventory_contracts(ticker, is_paper)
        action = evaluate_mm_candidate(candidate, net_inventory, bankroll=bm.bankroll)
        if action.kind == MMActionKind.NONE:
            continue

        opp = _candidate_opportunity(candidate)
        allowed, reason = tracker.is_allowed(opp, action.clip_dollars, is_mm=True)
        if not allowed:
            logger.debug("MM quote blocked for %s: %s", ticker, reason)
            continue

        # clip_dollars is the total per-candidate commitment across BOTH legs (it's
        # what's checked against the exposure cap), so each leg gets half of it —
        # sizing each leg at the full clip_dollars would mean one candidate's two
        # legs alone could total ~2x the cap.
        per_leg_dollars = action.clip_dollars / 2.0
        yes_count = max(1, math.floor(per_leg_dollars / action.yes_bid_price))
        no_count = max(1, math.floor(per_leg_dollars / action.no_bid_price))
        new_notional = yes_count * action.yes_bid_price + no_count * action.no_bid_price

        # Aggregate cap, on top of is_allowed()'s per-candidate check above: would
        # placing BOTH legs of THIS candidate, added to everything already filled
        # (bm.mm_exposure, queried fresh so it reflects any fill just recorded a
        # few lines up) plus everything still resting from earlier in this very
        # tick (pending_notional), push total committed notional past the cap?
        committed = bm.mm_exposure + (0.0 if is_paper else pending_notional)
        if committed + new_notional > cap_dollars + 1e-6:
            logger.debug(
                "MM quote blocked for %s: aggregate exposure would reach $%.2f "
                "(cap $%.2f)", ticker, committed + new_notional, cap_dollars,
            )
            continue

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
            "yes": ({"order_id": yes_order_id, "price": action.yes_bid_price, "count": yes_count}
                    if yes_filled < yes_count else None),
            "no": ({"order_id": no_order_id, "price": action.no_bid_price, "count": no_count}
                   if no_filled < no_count else None),
        }

        if yes_filled > 0:
            _record_fill(candidate, "yes", action.yes_bid_price, yes_filled, yes_fee, False,
                         order_id=yes_order_id)
            filled_count += 1
        if no_filled > 0:
            _record_fill(candidate, "no", action.no_bid_price, no_filled, no_fee, False,
                         order_id=no_order_id)
            filled_count += 1

        # Whatever's left resting (not filled at placement) is real pending
        # exposure for the rest of this tick's aggregate check.
        pending_notional += sum(
            leg["price"] * leg["count"]
            for leg in (_resting_quotes[ticker]["yes"], _resting_quotes[ticker]["no"])
            if leg
        )

    return filled_count
