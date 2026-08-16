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
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
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

    The gap this docstring used to describe as out-of-scope — a ticker dropping out
    of mm_candidates while something is still resting on it — is now handled by
    sweep_orphaned_quotes(), after it stopped being hypothetical: 7 orders across 4
    tickers were found resting 3-4 days, one of which had filled into real untracked
    exposure.

    Duplicate handling: MM's invariant is one resting quote per (ticker, side), and
    _resting_quotes can only represent one. If Kalshi reports more, the extras are
    cancelled here rather than silently dropped — see the inline note below.
    """
    global _startup_synced
    if _startup_synced:
        return
    _startup_synced = True

    from execution.kalshi_executor import list_resting_orders
    orders = list_resting_orders()
    if not orders:
        return

    from execution.kalshi_executor import cancel_quote

    recovered: dict[str, dict] = {}
    duplicates: list[tuple[str, str, str]] = []   # (ticker, side, order_id)

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
        slot = recovered.setdefault(ticker, {"yes": None, "no": None})

        # MM's invariant is exactly ONE resting quote per (ticker, side). If Kalshi
        # reports two, something already went wrong upstream — most likely a placement
        # that looked like it failed but actually landed, so a replacement was issued
        # alongside it. The dict can only hold one, and the previous version silently
        # let the later order overwrite the earlier, leaving the earlier one live and
        # untracked forever. That is the same defect that produced the orphaned pairs
        # found on 2026-08-15 (two 9-contract orders each on CINNYC-CIN, CINNYC-NYC
        # and CLBMTL-MTL). Keep the first, cancel the rest, and say so loudly.
        if slot[leg_side] is not None:
            dup_id = o.get("order_id")
            duplicates.append((ticker, leg_side, dup_id))
            if dup_id and not cancel_quote(dup_id):
                logger.error(
                    "MM recovery: FAILED to cancel duplicate %s quote %s on %s — it "
                    "is still live and untracked.", leg_side.upper(), dup_id, ticker,
                )
            continue

        slot[leg_side] = {
            "order_id": o.get("order_id"), "price": price, "count": remaining,
        }

    if duplicates:
        logger.critical(
            "MM recovery: found %d DUPLICATE resting quote(s) — more than one order on "
            "the same ticker+side, which breaks MM's one-quote-per-side invariant. "
            "Kept the first of each and cancelled the extras: %s",
            len(duplicates), duplicates,
        )

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
    # Why this decision was reached — "ok" when quoting, otherwise the specific
    # gate that rejected it. Exists so run_mm_tick() can report WHY a candidate
    # wasn't quoted instead of silently dropping it: every rejection path in this
    # module used to be logger.debug, which is off in production, so a tick that
    # evaluated ~60 candidates and rejected 59 emitted nothing at all.
    reason: str = ""
    net_per_pair: float | None = None    # expected profit per matched pair, after fees


def _maker_fee_per_contract(price: float) -> float:
    """Kalshi's maker fee is 25% of the taker formula (0.07 * p * (1-p) per
    contract) and there is no retail maker rebate. Kalshi rounds the fee UP to the
    whole cent PER ORDER, which this does not model — so at small clip sizes the
    real fee is higher than this estimate, never lower."""
    return config.MM_MAKER_FEE_RATE * price * (1.0 - price)


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


def _seconds_to_kickoff(candidate: dict) -> float | None:
    """Seconds until this market's game starts, or None if it can't be determined.

    Uses the Odds API's commence_time rather than KalshiMarket.game_time: Kalshi's
    close_time is a settlement deadline (~2 weeks out), and game_time reconstructs
    kickoff by splicing the event_ticker's date onto close_time's hour — good enough
    for display, but this value gates a real risk stop and deserves the sportsbook's
    actual scheduled start.

    Returning None (rather than 0) when it's unknown deliberately leaves the gate
    OPEN. An unparseable timestamp should not silently halt all market making; the
    orphan sweeper remains the backstop for anything that slips through.
    """
    event = candidate.get("matched_event")
    commence = getattr(getattr(event, "odds_event", None), "commence_time", None)
    if commence is None:
        return None
    try:
        if commence.tzinfo is None:
            commence = commence.replace(tzinfo=timezone.utc)
        return (commence - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


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
    Eligibility gates, in order, each returning MMActionKind.NONE with a `reason`:
    no_consensus, outside_fair_value_band, spread_too_narrow, insufficient_volume,
    too_few_books, high_disagreement, consensus_outside_spread, crossed_book,
    below_fee_floor, clip_zero.

    `kalshi_volume`, `yes_bid`, `yes_ask`, `bookmaker_count` and `consensus_std` are
    each OPTIONAL: when a key is absent its gate is skipped. run_mm_tick() always
    supplies volume/bid/ask from the live book and the scan supplies the consensus
    quality fields, so the live path is fully gated. The skip exists for
    mm_backtest.py, which replays historical candlesticks and genuinely cannot
    reconstruct per-candle sportsbook book counts — making those hard requirements
    would silently reject every historical candle and have the backtest report zero
    opportunities rather than fail loudly.
    """
    consensus = candidate.get("consensus_prob")
    spread = candidate.get("kalshi_spread")
    if consensus is None or spread is None:
        return MMAction(kind=MMActionKind.NONE, reason="no_consensus")

    lo, hi = config.MM_FAIR_VALUE_BAND
    if not (lo <= consensus <= hi):
        return MMAction(kind=MMActionKind.NONE, reason="outside_fair_value_band")

    if spread < config.MM_MIN_SPREAD_TO_QUOTE:
        return MMAction(kind=MMActionKind.NONE, reason="spread_too_narrow")

    # Kickoff. A resting quote does not expire when the game starts, and once the
    # event leaves mm_candidates this function is never called for it again — so
    # the LAST chance to cancel is while there is still time on the clock. See
    # config.MM_STOP_QUOTING_BEFORE_KICKOFF_SECONDS for the full incident shape.
    # Checked before the liquidity gates because it is a hard risk stop: it must
    # fire even for a market that looks perfectly healthy on every other measure.
    secs_to_kickoff = candidate.get("seconds_to_kickoff")
    if (secs_to_kickoff is not None
            and secs_to_kickoff < config.MM_STOP_QUOTING_BEFORE_KICKOFF_SECONDS):
        return MMAction(kind=MMActionKind.NONE, reason="too_close_to_kickoff")

    # Liquidity, measured as recent FLOW (24h traded contracts), not as a lifetime
    # stock. On Kalshi sports a wide spread usually means nobody is there at all
    # rather than that a fat spread is on offer: of 230 live wide-spread markets
    # surveyed 2026-08-14, 179 (78%) had never traded. A quote in a market with no
    # counterparty flow cannot fill no matter how well it is priced.
    volume = candidate.get("kalshi_volume_24h")
    if volume is not None and volume < config.MM_MIN_VOLUME:
        return MMAction(kind=MMActionKind.NONE, reason="insufficient_volume")

    # Fair-value confidence. A two-sided quote is centered on consensus, so low
    # confidence in that center hurts more here than for a directional bet —
    # both legs are mispriced at once, in opposite directions.
    books = candidate.get("bookmaker_count")
    if books is not None and books < config.MM_MIN_BOOKMAKERS:
        return MMAction(kind=MMActionKind.NONE, reason="too_few_books")

    std = candidate.get("consensus_std")
    if std is not None and std > config.MM_MAX_CONSENSUS_STD:
        return MMAction(kind=MMActionKind.NONE, reason="high_disagreement")

    # Centering. MM's premise is that the market is roughly right and we are paid
    # to wait inside it. A consensus outside Kalshi's touch asserts the opposite —
    # that the market is WRONG — which is a directional signal, not a spread to
    # capture, and quoting it produces a book-crossing order (see the guard below).
    yes_bid = candidate.get("yes_bid")
    yes_ask = candidate.get("yes_ask")
    have_book = (yes_bid is not None and yes_ask is not None
                 and yes_ask > yes_bid > 0)
    if have_book:
        tol = config.MM_CENTERING_TOLERANCE
        if not (yes_bid - tol <= consensus <= yes_ask + tol):
            return MMAction(kind=MMActionKind.NONE, reason="consensus_outside_spread")

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

    # Crossing guard. place_resting_quote() rests a plain GTC limit order, and its
    # docstring treats immediate matching as a benign edge case — it is not. A
    # crossing quote pays the TAKER fee (4x maker) at a price the directional
    # model never validated as +EV, and only the crossing leg fills, leaving naked
    # directional exposure instead of the matched pair that equal-contract sizing
    # exists to guarantee. Buying YES at >= yes_ask crosses; buying NO at price p
    # is economically selling YES at 1-p, which crosses when 1-p <= yes_bid.
    if have_book:
        yes_bid_price = min(yes_bid_price, round(yes_ask - 0.01, 2))
        no_bid_price = min(no_bid_price, round(1.0 - yes_bid - 0.01, 2))
        if yes_bid_price < 0.01 or no_bid_price < 0.01:
            return MMAction(kind=MMActionKind.NONE, reason="crossed_book")

    # Fee floor, checked AFTER clamping because clamping inward can consume the
    # whole margin. A matched pair costs yes_bid_price + no_bid_price and always
    # pays exactly $1, so gross capture is 1 - pair_cost, and both legs pay a
    # maker fee. Below ~1.3c of spread this is negative however the quote is
    # placed — which is why the median 1c-spread Kalshi market is unquotable.
    pair_cost = yes_bid_price + no_bid_price
    net_per_pair = (1.0 - pair_cost
                    - _maker_fee_per_contract(yes_bid_price)
                    - _maker_fee_per_contract(no_bid_price))
    if net_per_pair < config.MM_MIN_NET_PER_PAIR:
        return MMAction(kind=MMActionKind.NONE, reason="below_fee_floor",
                        net_per_pair=net_per_pair)

    clip = mm_clip_size(spread, bankroll=bankroll)
    if clip <= 0:
        return MMAction(kind=MMActionKind.NONE, reason="clip_zero")

    return MMAction(
        kind=MMActionKind.QUOTE,
        reservation_price=reservation,
        yes_bid_price=yes_bid_price,
        no_bid_price=no_bid_price,
        clip_dollars=clip,
        reason="ok",
        net_per_pair=net_per_pair,
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

    QUEUE PRIORITY. A leg whose freshly-computed price and size are identical to
    what is already resting is LEFT ALONE — not cancelled and re-placed. Kalshi's
    book fills same-price orders in time priority, so the previous
    cancel-and-replace-unconditionally behavior sent our order to the back of the
    queue every MM_INTERVAL_SECONDS (120x/hour), permanently behind anyone who
    placed a quote and left it. That cost nothing while our candidate markets had
    no flow at all, but it makes a fill nearly impossible in the ones that do.

    VISIBILITY. Every candidate's outcome — quoted, kept, cancelled or rejected,
    with the specific gate that rejected it — is written to db.log_mm_decisions()
    and summarised at INFO. Before this, all four rejection paths were
    logger.debug (off in production) and the module's only INFO line was the fill
    log, so a tick that evaluated ~60 candidates and quoted 1 was silent.

    Returns the number of legs filled this tick.
    """
    from execution.kalshi_executor import (
        place_resting_quote, cancel_quote, get_order_status, order_fee_paid,
    )

    if not is_paper:
        _sync_resting_quotes_from_kalshi()

    filled_count = 0
    kept_legs = 0
    placed_legs = 0
    seen_tickers: set[str] = set()
    pending_notional = _resting_notional()  # carried over from the prior tick
    cap_dollars = config.MM_MAX_EXPOSURE_PCT * bm.bankroll
    decisions: list[dict] = []
    reasons: Counter = Counter()

    def _record(candidate: dict, live_km: KalshiMarket, action_name: str,
                reason: str, mm: MMAction | None = None,
                contracts: int | None = None) -> None:
        """One row per candidate for db.log_mm_decisions() / the INFO summary."""
        reasons[reason] += 1
        event = candidate["matched_event"].odds_event
        decisions.append({
            "sport": event.sport_key,
            "kalshi_ticker": live_km.ticker,
            "team_name": candidate.get("team_name"),
            "bet_type": live_km.bet_type,
            "kalshi_bid": live_km.yes_bid,
            "kalshi_ask": live_km.yes_ask,
            "kalshi_spread": live_km.spread,
            "kalshi_volume": live_km.volume,
            "consensus_prob": candidate.get("consensus_prob"),
            "bookmaker_count": candidate.get("bookmaker_count"),
            "consensus_std": candidate.get("consensus_std"),
            "yes_quote": mm.yes_bid_price if mm else None,
            "no_quote": mm.no_bid_price if mm else None,
            "net_per_pair": mm.net_per_pair if mm else None,
            "clip_dollars": mm.clip_dollars if mm else None,
            "contracts": contracts,
            "action": action_name,
            "reason": reason,
        })

    def _drop_prior(ticker: str, legs: dict) -> None:
        """Cancel whatever survived the fill check and forget the ticker. Used on
        every path that stops quoting a market — without it, a candidate rejected
        by a gate would leave its old quote resting on a market we've decided not
        to make, which is precisely how the orphans of 2026-08-13 accumulated."""
        for leg in (legs.get("yes"), legs.get("no")):
            if leg:
                cancel_quote(leg["order_id"])
        _resting_quotes.pop(ticker, None)

    for candidate in mm_candidates:
        km = candidate["matched_event"].kalshi_market
        ticker = km.ticker
        live_km = fresh_kalshi.get(ticker, km)
        if ticker in seen_tickers:
            _record(candidate, live_km, "rejected", "duplicate_ticker")
            continue
        seen_tickers.add(ticker)

        # Overlay the LIVE book on the cached candidate. kalshi_spread was already
        # refreshed here; volume, the raw bid/ask and time-to-kickoff are new, and
        # feed the liquidity gate, the centering gate, the crossing guard and the
        # kickoff stop respectively.
        candidate = {
            **candidate,
            "kalshi_spread": live_km.spread,
            "kalshi_volume": live_km.volume,
            "kalshi_volume_24h": live_km.volume_24h,
            "yes_bid": live_km.yes_bid,
            "yes_ask": live_km.yes_ask,
            "seconds_to_kickoff": _seconds_to_kickoff(candidate),
        }

        # ── Phase A: resolve whatever was resting from the previous tick ───────
        # Check fills BEFORE deciding anything else — the normal way a maker quote
        # fills is a counterparty crossing it later, not instantly at placement,
        # and an unrecorded fill means no bankroll accounting, no stop-loss and no
        # correlation tracking. Unlike the previous version this does NOT cancel
        # here: an unfilled leg whose price is unchanged is kept below, and
        # cancelling it first would throw away its queue position for nothing.
        prior = _resting_quotes.pop(ticker, None)
        surviving: dict[str, dict | None] = {"yes": None, "no": None}
        if prior and not is_paper:
            for side, leg in (("yes", prior.get("yes")), ("no", prior.get("no"))):
                if not leg:
                    continue
                # This leg's old notional is being resolved one way or another
                # (filled, cancelled, or kept and re-added below) — either way it
                # comes out of the running total here and is re-counted later.
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
                # Only a completely untouched leg is a candidate for keeping. A
                # partial fill has already consumed its queue position and is no
                # longer the size we sized it to be, so it gets replaced.
                if filled <= 0:
                    surviving[side] = leg
                elif filled < leg["count"]:
                    cancel_quote(leg["order_id"])

        # ── Phase B: should we still be quoting this market at all? ────────────
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
            _drop_prior(ticker, surviving)
            _record(candidate, live_km, "cancelled" if prior else "rejected",
                    "spread_narrowed_to_tradeable")
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
            _drop_prior(ticker, surviving)
            _record(candidate, live_km, "cancelled" if prior else "rejected",
                    "stale_consensus_drift")
            continue

        net_inventory = _net_inventory_contracts(ticker, is_paper)
        action = evaluate_mm_candidate(candidate, net_inventory, bankroll=bm.bankroll)
        if action.kind == MMActionKind.NONE:
            _drop_prior(ticker, surviving)
            _record(candidate, live_km, "cancelled" if prior else "rejected",
                    action.reason or "no_action", action)
            continue

        opp = _candidate_opportunity(candidate)
        allowed, reason = tracker.is_allowed(opp, action.clip_dollars, is_mm=True)
        if not allowed:
            _drop_prior(ticker, surviving)
            _record(candidate, live_km, "cancelled" if prior else "rejected",
                    f"blocked:{reason}", action)
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
        new_notional = count * pair_cost

        # Aggregate cap, on top of is_allowed()'s per-candidate check above: would
        # placing BOTH legs of THIS candidate, added to everything already filled
        # (bm.mm_exposure, queried fresh so it reflects any fill just recorded a
        # few lines up) plus everything still resting from earlier in this very
        # tick (pending_notional), push total committed notional past the cap?
        committed = bm.mm_exposure + (0.0 if is_paper else pending_notional)
        if committed + new_notional > cap_dollars + 1e-6:
            _drop_prior(ticker, surviving)
            _record(candidate, live_km, "cancelled" if prior else "rejected",
                    "aggregate_exposure_cap", action, count)
            continue

        if is_paper:
            # Paper mode never places real orders. A quote is treated as filled
            # this tick only if the LIVE top-of-book already crosses through our
            # intended price — i.e., if we'd been resting there, we'd already be
            # filled. This makes paper mode a faithful (if conservative — it can't
            # see fills that happen mid-interval, only at each tick's snapshot)
            # preview of live behavior instead of assuming instant fills the way
            # the directional strategy's paper mode does.
            paper_fills = 0
            if live_km.yes_ask > 0 and live_km.yes_ask <= action.yes_bid_price:
                _record_fill(candidate, "yes", action.yes_bid_price, count, 0.0, True)
                paper_fills += 1
            if live_km.yes_bid > 0 and (1.0 - live_km.yes_bid) <= action.no_bid_price:
                _record_fill(candidate, "no", action.no_bid_price, count, 0.0, True)
                paper_fills += 1
            filled_count += paper_fills
            _record(candidate, live_km, "placed",
                    "paper_filled" if paper_fills else "ok", action, count)
            continue

        # ── Phase C: keep unchanged legs, replace only what actually moved ─────
        # Kalshi fills same-price orders in time priority. Cancelling and
        # re-placing a leg at a price it is already resting at surrenders that
        # priority for no benefit — every MM_INTERVAL_SECONDS, which at the
        # default 30s is 120 times an hour, permanently behind anyone who quoted
        # once and left it.
        new_legs: dict[str, dict | None] = {"yes": None, "no": None}
        for side, price in (("yes", action.yes_bid_price), ("no", action.no_bid_price)):
            old = surviving.get(side)
            if old and old["price"] == price and old["count"] == count:
                new_legs[side] = old
                kept_legs += 1
                continue
            if old:
                cancel_quote(old["order_id"])
            order_id, leg_filled, fee = place_resting_quote(ticker, side, price, count)
            placed_legs += 1
            if leg_filled > 0:
                _record_fill(candidate, side, price, leg_filled, fee, False,
                             order_id=order_id)
                filled_count += 1
            if leg_filled < count:
                new_legs[side] = {"order_id": order_id, "price": price, "count": count}

        _resting_quotes[ticker] = new_legs

        # Whatever's left resting (kept, or newly placed and not filled at
        # placement) is real pending exposure for the rest of this tick's
        # aggregate check.
        pending_notional += sum(
            leg["price"] * leg["count"]
            for leg in (new_legs["yes"], new_legs["no"]) if leg
        )
        _record(candidate, live_km,
                "kept" if (new_legs["yes"] and new_legs["yes"] is surviving.get("yes")
                           and new_legs["no"] and new_legs["no"] is surviving.get("no"))
                else "placed",
                "ok", action, count)

    # Naked-exposure check. A matched pair is near-riskless; a leg that filled
    # alone is a directional position the bot never decided to take. This is the
    # single most important number about whether MM is actually working, and
    # nothing reported it until now — the 8 unpaired RSLDAL contracts sat
    # unnoticed for two days. Logged every tick that finds any, at WARNING, so it
    # survives production log levels.
    try:
        pairing = [p for p in db.get_mm_pairing(is_paper) if p["unpaired"] > 0]
        if pairing:
            total = sum(p["unpaired_dollars"] for p in pairing)
            logger.warning(
                "MM UNPAIRED EXPOSURE: $%.2f across %d ticker(s) — these legs filled "
                "on one side only and are naked directional risk, not spread capture: "
                "%s", total, len(pairing),
                ", ".join(f"{p['ticker']} {p['unpaired']:.0f}x {p['naked_side'].upper()} "
                          f"(${p['unpaired_dollars']:.2f})" for p in pairing[:5]),
            )
    except Exception as e:
        logger.debug("MM pairing check failed: %s", e)

    if decisions:
        quoted = sum(1 for d in decisions if d["action"] in ("placed", "kept"))
        logger.info(
            "MM tick: %d candidate(s) -> %d quoted (%d leg(s) placed, %d kept), "
            "%d fill(s). Outcomes: %s",
            len(decisions), quoted, placed_legs, kept_legs, filled_count,
            ", ".join(f"{r}={n}" for r, n in reasons.most_common()),
        )
        try:
            db.log_mm_decisions(uuid.uuid4().hex, decisions)
        except Exception as e:
            # Visibility must never be able to take down trading.
            logger.warning("MM: could not persist decision log: %s", e)

    return filled_count


def sweep_orphaned_quotes(active_tickers: set[str], is_paper: bool,
                          min_age_seconds: float = 3600.0) -> int:
    """
    Cancel resting orders on Kalshi that nothing is managing any more.

    THE PROBLEM
    -----------
    run_mm_tick()'s per-candidate loop only revisits tickers present in the CURRENT
    tick's candidate list. Once a ticker drops out (game started, spread narrowed, it
    fell off the scan) anything still resting on it becomes invisible: never
    fill-checked, never cancelled, still live on Kalshi and still able to fill.

    Documented as an accepted gap in _sync_resting_quotes_from_kalshi(), then it
    happened. On 2026-08-13 an audit found 9 resting orders across 5 tickers, oldest 3
    days. One of them, KXMLSGAME-26AUG19RSLDAL-RSL, kept filling while unobserved: it
    grew from 1 contract to 8 ($4.80 of real money) over two days, while the positions
    table held 11 rows for that market all saying status='failed', stake $0.00. That
    exposure was invisible to the risk caps, the stop-loss and correlation blocking.

    WHY THIS READS FROM KALSHI, NOT FROM _resting_quotes
    ----------------------------------------------------
    The first version of this swept the in-memory _resting_quotes dict, which has two
    fatal weaknesses for this job:
      - it is keyed {ticker: {yes: leg, no: leg}}, so two orders on the SAME ticker and
        side collapse to one and the other survives the sweep (the real orphan set
        contained exactly such duplicate pairs);
      - it only reflects what this process placed or recovered at startup.
    Kalshi's own resting-order list is authoritative for "what is actually live", so
    that is what gets swept.

    WHY THE AGE GATE
    ----------------
    Sweeping every non-candidate ticker would also cancel the DIRECTIONAL strategy's
    in-flight GTC orders, which legitimately rest while place_order() polls them.
    Rather than depend on loop-ordering to keep those apart, only orders older than
    min_age_seconds are touched. The directional path's longest timeout is
    LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS (900s) and MM requotes every
    MM_INTERVAL_SECONDS (30s), so a resting order more than an hour old is by
    construction unmanaged by either.

    Fills found here are NOT written to positions: _record_fill() needs the full
    candidate (odds_event + kalshi_market) to build a row and an orphan has none by
    definition. Fabricating one from partial data would be worse than the gap. They are
    logged at CRITICAL so reconciliation and a human both see them; the cancel is what
    stops the bleeding.

    Returns the number of orders cancelled.
    """
    if is_paper:
        return 0

    from execution.kalshi_executor import cancel_quote, list_resting_orders

    try:
        orders = list_resting_orders()
    except Exception as e:
        logger.warning("MM sweep: could not list resting orders: %s", e)
        return 0

    now = datetime.now(timezone.utc)
    cancelled, swept_tickers = 0, set()

    for o in orders:
        ticker = o.get("ticker")
        order_id = o.get("order_id")
        if not ticker or not order_id or ticker in active_tickers:
            continue

        created = o.get("created_time")
        if not created:
            continue
        try:
            age = (now - _parse_ts(created)).total_seconds()
        except Exception:
            continue
        if age < min_age_seconds:
            continue

        filled = float(o.get("fill_count_fp", 0) or 0)
        if filled > 0 and not db.position_exists_for_order_id(order_id):
            logger.critical(
                "MM ORPHAN FILL — %s filled %g contract(s) on an order resting since "
                "%s (order_id=%s) that no longer has an MM candidate, so no position "
                "row can be built. REAL UNTRACKED EXPOSURE — reconcile by hand. "
                "Cancelling the remainder now.",
                ticker, filled, created[:19], order_id,
            )
        if cancel_quote(order_id):
            cancelled += 1
            swept_tickers.add(ticker)
        else:
            logger.error(
                "MM sweep: FAILED to cancel orphaned order %s on %s — it is still "
                "live and can still fill.", order_id, ticker,
            )

    # Drop any in-memory tracking for tickers we just cancelled out from under.
    for t in swept_tickers:
        _resting_quotes.pop(t, None)

    if cancelled:
        logger.warning(
            "MM: swept %d orphaned resting order(s) older than %.0fs across %d "
            "ticker(s): %s", cancelled, min_age_seconds, len(swept_tickers),
            sorted(swept_tickers),
        )
    return cancelled


def _parse_ts(s: str) -> datetime:
    """Kalshi timestamps carry a variable number of fractional-second digits."""
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*?)(\.\d+)?([+-]\d{2}:\d{2})$", s)
    if m and m.group(2):
        s = f"{m.group(1)}.{(m.group(2)[1:] + '000000')[:6]}{m.group(3)}"
    return datetime.fromisoformat(s)
