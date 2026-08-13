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
import time
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
# "no": {...} | None}. Not persisted across restarts by itself — but
# _sync_resting_quotes_from_kalshi() rebuilds it from Kalshi's own resting-order
# list once per process startup, so a restart no longer loses track of orders
# that were already resting (see that function's docstring for the incident that
# motivated this).
_resting_quotes: dict[str, dict] = {}

# Guards _sync_resting_quotes_from_kalshi() to run at most once per process.
_startup_synced = False


def _sync_resting_quotes_from_kalshi() -> None:
    """
    Rebuild _resting_quotes from Kalshi's own resting-order list, once per
    process lifetime. Before this existed, a restart while MM had real orders
    resting meant those orders became permanently invisible to the fill-check in
    run_mm_tick() — confirmed 2026-08-12: a real 5-contract NO fill on
    KXMLSGAME-26AUG19PORSD-POR went completely unrecorded (no bankroll
    accounting, no stop-loss, no correlation tracking) for 3.5+ hours, caught
    only by execution/reconciliation.py's mismatch log, with nothing acting on
    it until a human noticed. Kalshi's own order book is the authoritative
    source of what's actually resting, so syncing from it on startup makes
    recovery automatic instead of relying on "cancel everything before
    restarting" discipline.

    Known residual gap: this only recovers state at startup. A ticker that
    later drops out of mm_candidates entirely (e.g. game already started, no
    longer quoted) while something of ours is still resting on it won't be
    revisited by run_mm_tick()'s per-candidate loop, since that loop only checks
    tickers present in the current tick's candidate list. Not addressed here —
    narrower and rarer than the restart gap this fixes, and out of scope unless
    asked for.
    """
    global _startup_synced
    if _startup_synced:
        return
    _startup_synced = True

    from execution.kalshi_executor import list_resting_orders
    orders = list_resting_orders()
    if not orders:
        return

    recovered: dict[str, dict] = {}
    for o in orders:
        ticker = o.get("ticker")
        remaining = float(o.get("remaining_count_fp", 0) or 0)
        if not ticker or remaining <= 0:
            continue
        # outcome_side is Kalshi's own field for which economic side a
        # sell-yes/buy-no order represents — matches the "yes leg" / "no leg"
        # distinction _resting_quotes already uses everywhere else here.
        leg_side = "yes" if o.get("outcome_side") == "yes" else "no"
        price_field = "yes_price_dollars" if leg_side == "yes" else "no_price_dollars"
        price = float(o.get(price_field, 0) or 0)
        recovered.setdefault(ticker, {"yes": None, "no": None})[leg_side] = {
            "order_id": o.get("order_id"), "price": price, "count": remaining,
        }

    if recovered:
        logger.warning(
            "MM: recovered %d ticker(s) with resting orders from before this "
            "process started: %s", len(recovered), sorted(recovered),
        )
        _resting_quotes.update(recovered)


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

    Widens the quote as candidate["scanned_at"] ages: consensus_prob is a snapshot
    from whenever the last due-sport Odds API scan happened (up to
    config.POLL_INTERVAL_DEFAULT_SECONDS ago), reused unchanged by every tick in
    between — unlike kalshi_spread, which run_mm_tick() refreshes every tick. An
    old snapshot deserves less confidence, so the quote sits further from its
    (possibly stale) center rather than pricing with false precision. Missing
    scanned_at (e.g. a candidate dict built directly, as in tests) is treated as
    age zero — no widening, matches pre-staleness-handling behavior.
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

    age = time.time() - candidate.get("scanned_at", time.time())
    age_frac = min(1.0, max(0.0, age / config.POLL_INTERVAL_DEFAULT_SECONDS))
    staleness_widen = 1.0 + (config.MM_STALE_WIDEN_MAX_MULTIPLIER - 1.0) * age_frac
    half = config.MM_QUOTE_HALF_SPREAD_FRACTION * staleness_widen * spread
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

    Also pauses quoting a candidate if Kalshi's own live mid has drifted more than
    config.MM_STALE_DRIFT_CANCEL from where it was when this candidate's
    consensus_prob was captured — a free early-warning signal (no extra Odds API
    cost) that the sportsbook-derived reservation price evaluate_mm_candidate()
    is quoting around may no longer reflect real fair value. See
    evaluate_mm_candidate()'s age-based widening for the complementary mechanism
    (quotes get wider, not just paused, as consensus_prob ages).

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

    if not is_paper:
        _sync_resting_quotes_from_kalshi()

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
                # order_id is normally checked exactly once (this block), right
                # before being cancelled or fully consumed, so filled here always
                # meant "new since we placed it." That stopped being true once
                # _sync_resting_quotes_from_kalshi() started recovering orders
                # that may have already been (manually or automatically)
                # backfilled from a prior restart — guard against re-recording
                # the same historical fill twice.
                if filled > 0 and not db.position_exists_for_order_id(leg["order_id"]):
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

        # Staleness trip-wire: consensus_prob (the quote's center) is a snapshot
        # from this candidate's last due-sport scan, not refreshed by this tick —
        # only kalshi_spread is. If Kalshi's own live price has already moved
        # meaningfully since that scan, that's real evidence something changed
        # (news, sharp money, lineups) even though we haven't paid for an Odds API
        # call to confirm it. Pause quoting this candidate rather than keep
        # resting a price built on a reference point the market has since moved
        # away from — cheaper than a stale-quote adverse-selection loss, and
        # costs nothing extra (both prices are already being fetched for free).
        baseline_mid = (km.yes_bid + km.yes_ask) / 2.0
        live_mid = (live_km.yes_bid + live_km.yes_ask) / 2.0
        if abs(live_mid - baseline_mid) >= config.MM_STALE_DRIFT_CANCEL:
            logger.debug(
                "MM quote paused for %s: Kalshi mid drifted %.3f -> %.3f since "
                "this candidate's last scan (>= %.2f threshold)",
                ticker, baseline_mid, live_mid, config.MM_STALE_DRIFT_CANCEL,
            )
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

        # Both legs get the SAME contract count, not the same dollar amount. The
        # whole reason two-sided quoting is close to risk-free is that
        # yes_bid_price + no_bid_price < $1 (guaranteed by the half-spread math
        # above) — so every MATCHED pair (one YES + one NO contract) costs less
        # than $1 combined while exactly one of them pays out $1 at settlement,
        # a guaranteed profit regardless of outcome. Sizing each leg by dividing
        # clip_dollars by ITS OWN price (the old approach) breaks this: since the
        # two prices differ, the same dollar budget buys different contract
        # counts on each side, leaving the excess contracts on the cheaper side
        # completely unhedged — a naked directional bet wearing a market-making
        # costume, not the safe spread capture this is supposed to be.
        pair_cost = action.yes_bid_price + action.no_bid_price
        count = max(1, math.floor(action.clip_dollars / pair_cost)) if pair_cost > 0 else 0
        yes_count = count
        no_count = count
        new_notional = count * pair_cost

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

    filled_count += _sweep_orphaned_quotes(seen_tickers, is_paper)
    return filled_count


def _sweep_orphaned_quotes(seen_tickers: set[str], is_paper: bool) -> int:
    """
    Cancel resting quotes on tickers that are no longer in mm_candidates.

    The per-candidate loop above only ever revisits tickers present in the CURRENT
    tick's candidate list, so once a ticker drops out (game started, spread narrowed,
    it fell off the scan) anything still resting on it becomes invisible forever:
    never fill-checked, never cancelled, still live on Kalshi.

    This was documented as a known, accepted gap in _sync_resting_quotes_from_kalshi()
    ("narrower and rarer than the restart gap... out of scope unless asked for"). It
    then happened: on 2026-08-13 an audit found 9 resting orders across 5 tickers, the
    oldest 3 days old, none of them present in the positions table at all — and one
    (KXMLSGAME-26AUG19RSLDAL-RSL) had partially filled into a real position that was
    invisible to bankroll accounting, the stop-loss and correlation blocking.

    Deliberately does NOT try to record a fill it finds here. _record_fill() needs the
    full candidate (odds_event + kalshi_market) to build a position row, and by
    definition an orphaned ticker no longer has one. Fabricating a row from partial
    data would be worse than the gap. Instead: cancel unconditionally (which stops any
    FURTHER orphan fills, the actual bleeding), and log a fill we cannot record at
    CRITICAL so execution/reconciliation.py's mismatch check and a human both see it.
    """
    if is_paper:
        _resting_quotes.clear()
        return 0

    from execution.kalshi_executor import cancel_quote, get_order_status

    orphans = [t for t in _resting_quotes if t not in seen_tickers]
    if not orphans:
        return 0

    for ticker in orphans:
        legs = _resting_quotes.pop(ticker, None) or {}
        for side, leg in (("yes", legs.get("yes")), ("no", legs.get("no"))):
            if not leg:
                continue
            order_id = leg["order_id"]
            status = get_order_status(order_id)
            filled = float(status.get("fill_count_fp", 0) or 0) if status else 0.0
            if filled > 0 and not db.position_exists_for_order_id(order_id):
                logger.critical(
                    "MM ORPHAN FILL — %s leg on %s filled %g contract(s) @ %.4f "
                    "(order_id=%s) but the ticker is no longer an MM candidate, so no "
                    "position row can be built. Real untracked exposure: reconcile by "
                    "hand. Cancelling any remainder now.",
                    side.upper(), ticker, filled, leg["price"], order_id,
                )
            if filled < leg["count"]:
                cancel_quote(order_id)

    logger.warning(
        "MM: swept %d orphaned ticker(s) no longer in candidates: %s",
        len(orphans), sorted(orphans),
    )
    return 0
