"""
Detects value opportunities by comparing Kalshi prices to the de-vigged
consensus from traditional sportsbooks (via The Odds API).

detect_value() accepts an optional scan_log list. When provided, every
evaluated candidate is appended — including rejections — so the dashboard
can show a full picture of why each bet was or wasn't placed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.market_matcher import MatchedEvent
from core.odds_converter import consensus_stats

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    HOME    = "home"
    AWAY    = "away"
    DRAW    = "draw"
    OVER    = "over"
    UNDER   = "under"   # YES side of an explicit "Under" Kalshi market
    NO_OVER = "no_over" # NO side of an "Over" Kalshi market (= buying Under)
    COVER   = "cover"


@dataclass
class ValueOpportunity:
    matched_event: MatchedEvent
    outcome: Outcome
    team_name: str
    consensus_prob: float
    market_price: float
    edge: float
    market_url: str
    bookmaker_count: int
    consensus_std: float
    maker_only: bool = False  # True: edge only clears the bar at mid (0% fee) --
                               # execute_trade() must not fall back to crossing the ask

    @property
    def edge_pct(self) -> str:
        return f"{self.edge * 100:.1f}%"

    @property
    def market_odds_american(self) -> int:
        from core.odds_converter import prob_to_american
        return prob_to_american(self.market_price)


_SERIES_SLUG: dict[str, str] = {
    "KXNFLGAME":    "nfl-game",
    "KXNCAAFGAME":  "ncaaf-game",
    "KXNBAGAME":    "nba-game",
    "KXNCAABGAME":  "ncaab-game",
    "KXMLBGAME":    "mlb-game",
    "KXNHLGAME":    "nhl-game",
    "KXMLSGAME":    "mls-game",
    "KXEPLGAME":    "epl-game",
    "KXUCLGAME":    "uefa-champions-league-game",
    "KXNBATOTAL":   "nba-total",
    "KXMLBTOTAL":   "mlb-total",
    "KXNHLTOTAL":   "nhl-total",
    "KXEPLTOTAL":   "epl-total",
    "KXUCLTOTAL":   "ucl-total",
    "KXMLSTOTAL":   "mls-total",
    "KXNBASPREAD":  "nba-spread",
    "KXMLBSPREAD":  "mlb-spread",
    "KXNHLSPREAD":  "nhl-spread",
}


def _kalshi_url(ticker: str, event_ticker: str = "") -> str:
    event = (event_ticker if event_ticker else ticker).lower()
    series = event.split("-")[0].upper()
    slug = _SERIES_SLUG.get(series, series.lower())
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event}"


def _effective_min_edge(ask_price: float, min_edge: float) -> float:
    """
    The real edge bar a bet at this price must clear: config.MIN_EDGE plus the
    fee-adjusted cost of trading at this specific price (see
    core/kelly_calculator.py::fee_adjusted_breakeven_prob). Single source of truth
    for both _eval_edge's pass/fail check and the "no_edge" reason strings below —
    those used to display only the raw min_edge (rounded to a whole percent, so
    1.5% misleadingly showed as "2%") instead of the true, price-dependent bar.
    """
    from core.kelly_calculator import fee_adjusted_breakeven_prob

    fee_edge_cost = fee_adjusted_breakeven_prob(ask_price) - ask_price
    return min_edge + fee_edge_cost


def _eval_edge(
    consensus: float,
    ask_price: float,
    spread: float,
    min_edge: float,
) -> tuple[float, bool] | None:
    """
    Return (edge, maker_only) for the best qualifying fill, else None. The
    returned edge is still the RAW edge (consensus - price at the relevant
    step) — only the comparison threshold is fee-adjusted, not the stored
    value, since downstream code (CLV backfill, calibration dashboard)
    reconstructs consensus_prob as market_price + edge.

    Two acceptance paths, checked in order:
      1. ask_edge clears the fee-adjusted bar (_effective_min_edge, worst-case
         taker fee) -> maker_only=False. execute_trade() may safely try mid
         first, then fall back to crossing the ask if unfilled -- even the
         worst case (ask) is still worth it.
      2. Otherwise, mid_edge (priced at the actual mid, ask_price - spread/2)
         clears plain min_edge -> maker_only=True. Maker fills are very nearly
         free -- across 139 filled orders sampled 2026-08-15 Kalshi charged a
         maker fee on exactly one, at ~0.0178 * p * (1-p) per contract. This
         comment previously claimed maker fills were "confirmed genuinely 0%
         fee", which overstated it: the fee exists, it is just rarely levied on
         the series we trade (see execution/kalshi_executor.py's fee model note).
         Treating it as zero here is therefore slightly optimistic, not exact.
         Either way this is a real, if thinner, edge -- but crossing the ask
         would NOT clear the bar, so execute_trade() must only attempt the
         passive mid order and walk away (no bet, no cost) if it doesn't fill,
         rather than crossing the spread into a losing trade.
    """
    ask_edge = consensus - ask_price
    _EPS = 1e-9  # floating-point tolerance: treat 1.9999999% as 2.0%
    if ask_edge >= _effective_min_edge(ask_price, min_edge) - _EPS:
        return ask_edge, False
    mid_price = max(0.01, ask_price - spread / 2.0)
    mid_edge = consensus - mid_price
    if mid_edge >= min_edge - _EPS:
        return mid_edge, True
    return None


def _log(
    scan_log: list[dict] | None,
    me: MatchedEvent,
    team_name: str,
    kalshi_price: float | None,
    consensus_prob: float | None,
    bookmaker_count: int,
    consensus_std: float,
    edge: float | None,
    status: str,
    reason: str,
    kalshi_side: str | None = None,
    maker_only: bool = False,
) -> None:
    """Append one candidate record to scan_log if provided.

    kalshi_side: "yes" or "no" -- which side of the Kalshi ticker kalshi_price
    was quoted for (None when no side has been resolved yet, e.g. the
    low_volume rejection in detect_value() before routing to a bet-type
    handler). Used by storage/db.py::log_book_probabilities() to later compare
    a book's probability against Kalshi's own eventual yes/no resolution for
    this exact ticker -- see research/experiments/2026-08-11-book-weight-
    validation.md for why this is being collected.
    """
    if scan_log is None:
        return
    import json as _json
    km = me.kalshi_market
    event = me.odds_event
    limit_price = (
        round(max(0.01, kalshi_price - km.spread / 2.0), 4)
        if kalshi_price is not None else None
    )
    scan_log.append({
        "scanned_at":      "",          # filled in by caller (main.py)
        "sport":           event.sport_key,
        "home_team":       event.home_team,
        "away_team":       event.away_team,
        "team_name":       team_name,
        "bet_type":        km.bet_type,
        "threshold":       km.threshold,
        "kalshi_ticker":   km.ticker,
        "kalshi_side":     kalshi_side,
        "kalshi_spread":   round(km.spread, 4),
        "kalshi_volume":   round(km.volume, 0),
        "kalshi_price":    round(kalshi_price, 4) if kalshi_price is not None else None,
        "limit_price":     limit_price,
        "consensus_prob":  round(consensus_prob, 4) if consensus_prob is not None else None,
        "bookmaker_count": bookmaker_count,
        "consensus_std":   round(consensus_std, 6),
        "edge":            round(edge, 4) if edge is not None else None,
        "status":          status,
        "reason":          reason,
        "maker_only":      1 if maker_only else 0,
        "commence_time":   event.commence_time.isoformat(),
        "bookmakers_json": _json.dumps(event.bookmakers),
    })


def _quality_check(
    km,
    book_count: int,
    std_dev: float,
    bet_type: str,
    is_draw: bool = False,
) -> tuple[str, str] | None:
    """
    Check book count / spread / agreement against the quality-filter tier for
    this bet type. Returns (status, reason) if a filter fails, else None.
    """
    qf = config.quality_filters(bet_type, is_draw=is_draw)
    if book_count < qf["min_bookmaker_count"]:
        return "few_books", f"Only {book_count} books (min {qf['min_bookmaker_count']})"
    if std_dev > qf["high_uncertainty_std"] and book_count < qf["high_uncertainty_min_books"]:
        return (
            "high_uncertainty",
            f"High uncertainty: std_dev {std_dev:.3f} with only {book_count} books",
        )
    return None


def _spread_too_wide(km, bet_type: str, is_draw: bool = False) -> str | None:
    """
    Reason string if Kalshi's spread exceeds this bet type's max, else None.

    Deliberately NOT part of _quality_check(). It used to be, and because
    _quality_check() runs before the edge is computed, a `continue` there meant a
    wide-spread market's edge was NEVER EVALUATED — spread width silently became
    the routing decision and any directional value in the market was discarded
    unexamined. Splitting it out lets callers evaluate the edge first and then
    decide, which is the only way to know what routing to MM actually costs.

    See config.ALLOW_WIDE_SPREAD_MAKER for what callers do with this.
    """
    qf = config.quality_filters(bet_type, is_draw=is_draw)
    if km.spread > qf["max_kalshi_spread"]:
        return f"Kalshi spread {km.spread*100:.1f}¢ > max {qf['max_kalshi_spread']*100:.0f}¢"
    return None


def _resolve_wide_spread(
    maker_only: bool,
    wide_reason: str | None,
) -> tuple[bool, str, str]:
    """
    Decide what to do with an evaluated opportunity in a wide-spread market.

    Returns (allow, status, reason).

    A PASSIVE (maker_only) order is allowed through: it rests at the mid and, if
    nobody crosses it, expires having cost nothing — the downside is bounded at
    zero, so the wide spread costs us nothing to try.

    A CROSSING order is not, regardless of edge. max_kalshi_spread is a market
    quality signal as much as an execution-cost one: a wide spread means thin,
    stale pricing, and paying the ask into that is the trade most likely to be
    picking up someone else's information. _eval_edge() only proves the trade is
    +EV *if the consensus is right*, which is exactly the assumption a wide,
    untraded book undermines.
    """
    if wide_reason is None:
        return True, "value", "Edge found — bet placed"
    if maker_only and config.ALLOW_WIDE_SPREAD_MAKER:
        return True, "value", f"Edge found (passive only) — {wide_reason}"
    if maker_only:
        return False, "spread_too_wide", wide_reason
    return False, "spread_too_wide_take", (
        f"{wide_reason} — edge clears at the ask but crossing a wide spread is "
        f"not allowed; routed to market making instead"
    )


def detect_value(
    matched_events: list[MatchedEvent],
    min_edge: float = 0.0,
    scan_log: list[dict] | None = None,
    mm_candidates: list[dict] | None = None,
) -> list[ValueOpportunity]:
    """
    mm_candidates: when provided, every matched market rejected specifically for
    `spread_too_wide` (and no other reason) is also appended here with its already-
    computed consensus/quality data — reused as-is by execution/market_maker.py so
    market making shares the exact same fair-value computation as the directional
    strategy instead of re-deriving it. See core/market_matcher.py's MatchedEvent
    and config.QUALITY_FILTERS["*"]["max_kalshi_spread"] for what "too wide to cross
    directionally" means.
    """
    opportunities: list[ValueOpportunity] = []

    for me in matched_events:
        event = me.odds_event
        km = me.kalshi_market
        is_draw = km.bet_type == "h2h" and me.kalshi_outcome == "tie"

        # ── Filter: bet type enabled? ─────────────────────────────────────────
        # Checked before anything else so a disabled type costs no further work.
        # Note this gates the DIRECTIONAL strategy only: mm_candidates are not
        # collected for a disabled type either, since the `continue` skips the
        # whole market -- market making on a segment we refuse to trade
        # directionally would reintroduce the same exposure by another route.
        if km.bet_type not in config.ENABLED_BET_TYPES:
            reason = f"bet_type '{km.bet_type}' disabled (ENABLED_BET_TYPES)"
            logger.debug("Skip %s vs %s — %s (ticker=%s)",
                         event.home_team, event.away_team, reason, km.ticker)
            _log(scan_log, me, km.yes_team or km.title[:30], None, None,
                 0, 0.0, None, "bet_type_disabled", reason)
            continue

        # ── Filter: Kalshi volume ─────────────────────────────────────────────
        qf = config.quality_filters(km.bet_type, is_draw=is_draw)
        if km.volume < qf["min_kalshi_volume"]:
            reason = f"Volume {km.volume:.0f} < min {qf['min_kalshi_volume']:.0f}"
            logger.debug("Skip %s vs %s [%s] — %s (ticker=%s)",
                         event.home_team, event.away_team, km.bet_type, reason, km.ticker)
            _log(scan_log, me, km.yes_team or km.title[:30], None, None,
                 0, 0.0, None, "low_volume", reason)
            continue

        logger.debug("Passed filters: %s vs %s [%s] ticker=%s spread=%.2f vol=%.0f",
                     event.home_team, event.away_team, km.bet_type,
                     km.ticker, km.spread, km.volume)

        # ── Route by bet type ─────────────────────────────────────────────────
        if km.bet_type == "totals":
            _detect_totals(me, event, km, min_edge, opportunities, scan_log, mm_candidates)
        elif km.bet_type == "spread":
            _detect_spread(me, event, km, min_edge, opportunities, scan_log, mm_candidates)
        elif me.kalshi_outcome == "tie":
            _detect_h2h_tie(me, event, km, min_edge, opportunities, scan_log, mm_candidates)
        else:
            _detect_h2h(me, event, km, min_edge, opportunities, scan_log, mm_candidates)

    logger.debug("Found %d value opportunities with positive edge", len(opportunities))
    return opportunities


def _maybe_mm_candidate(
    mm_candidates: list[dict] | None,
    me: MatchedEvent,
    team_name: str,
    consensus: float,
    book_count: int,
    std_dev: float,
    status: str,
    reason: str,
) -> None:
    """Append a market-making candidate iff the ONLY reason this market was rejected
    is that Kalshi's spread is too wide to cross directionally — not low liquidity,
    too few books, or high disagreement, all of which are just as disqualifying for
    resting a quote as for taking a directional side.

    As of 2026-08-15 every caller reaches here AFTER _eval_edge has run, so a market
    arrives with its directional value already known. It is routed to MM only when
    that value was absent, or was present but could only be captured by crossing the
    wide spread (which _resolve_wide_spread refuses). A wide market with PASSIVE edge
    now becomes a directional maker_only bet instead and never reaches this function —
    previously the spread check short-circuited before the edge was computed, so such
    markets were sent here unexamined and, once MM's centering gate was added, were
    then rejected by MM too and traded by nobody."""
    if mm_candidates is None or status != "spread_too_wide":
        return
    mm_candidates.append({
        "matched_event": me,
        "team_name": team_name,
        "consensus_prob": consensus,
        "bookmaker_count": book_count,
        "consensus_std": std_dev,
        "kalshi_spread": me.kalshi_market.spread,
        # When this candidate's consensus_prob was captured — run_mm_tick() reuses
        # it unchanged for up to a full due-scan interval (see
        # execution/market_maker.py's staleness handling), so this timestamp is
        # what that logic measures age against.
        "scanned_at": time.time(),
    })


# ── H2H ───────────────────────────────────────────────────────────────────────

def _detect_h2h(me, event, km, min_edge, opportunities, scan_log, mm_candidates=None):
    # Soccer is 3-way (home / away / draw). Kalshi issues one binary market per
    # team (e.g. "Miami wins YES/NO"). The NO side of that market means "Miami
    # does NOT win" — which includes draws — NOT "opponent wins." Evaluating the
    # opponent's win probability against the NO price would produce phantom edge.
    # Skip the non-YES team for soccer so each team's Kalshi market is only
    # evaluated when it is explicitly the subject of the market.
    is_soccer = "soccer" in event.sport_key

    # Both loop iterations price the SAME Kalshi ticker (the away side off
    # 1 - yes_bid). So the MM decision cannot be made inside the loop: one side
    # may find a passive bet while the other finds nothing, and emitting an MM
    # quote on a ticker we are already betting would put the bot on both sides of
    # its own position. Hold the candidate and decide once, after the loop.
    opps_before = len(opportunities)
    pending_mm: tuple | None = None

    for outcome, team in [(Outcome.HOME, event.home_team), (Outcome.AWAY, event.away_team)]:
        if is_soccer:
            if me.kalshi_outcome == "yes" and outcome == Outcome.AWAY:
                continue
            if me.kalshi_outcome == "no" and outcome == Outcome.HOME:
                continue

        consensus, book_count, std_dev = consensus_stats(event.bookmakers, team)

        # Whether `team`'s probability is being read off the ticker's actual YES
        # side (yes_ask) or derived from the NO side (1 - yes_bid) — same
        # condition used below to pick kalshi_price, hoisted so every _log() call
        # in this function (including early-rejection paths) can record it.
        is_yes_side = (outcome == Outcome.HOME and me.kalshi_outcome == "yes") or \
                      (outcome == Outcome.AWAY and me.kalshi_outcome == "no")
        kalshi_side = "yes" if is_yes_side else "no"

        if consensus is None:
            logger.debug("No consensus prob for %s — skipping", team)
            _log(scan_log, me, team, None, None, 0, 0.0, None,
                 "no_consensus", "No sportsbook data for this team", kalshi_side)
            continue

        qcheck = _quality_check(km, book_count, std_dev, "h2h")
        if qcheck:
            status, reason = qcheck
            logger.debug("Skip %s — %s", team, reason)
            _log(scan_log, me, team, None, consensus, book_count, std_dev, None,
                 status, reason, kalshi_side)
            # Only the team that IS the Kalshi ticker's YES side gives an
            # unambiguous YES-side consensus — the other loop iteration re-derives
            # the same ticker's price from the opposite (1 - bid) direction, which
            # isn't a clean reservation-price input for a resting quote.
            # NOT an MM candidate: few_books/high_uncertainty are just as
            # disqualifying for resting a quote as for taking a side. Only
            # spread_too_wide routes to MM, and that is now decided after the
            # edge is evaluated, further down.
            continue

        # Evaluated, not short-circuited: see _spread_too_wide()'s docstring.
        wide_reason = _spread_too_wide(km, "h2h")

        if outcome == Outcome.HOME:
            if me.kalshi_outcome == "yes":
                kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
            else:
                kalshi_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else km.no_price
        else:
            if me.kalshi_outcome == "yes":
                kalshi_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else km.no_price
            else:
                kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price

        result = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
        if result is None:
            best_edge = consensus - kalshi_price
            eff_min = _effective_min_edge(kalshi_price, min_edge)
            if wide_reason:
                status, reason = "spread_too_wide", wide_reason
            else:
                status = "no_edge"
                reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
            _log(scan_log, me, team, kalshi_price, consensus, book_count, std_dev,
                 best_edge, status, reason, kalshi_side)
            if wide_reason and is_yes_side:
                pending_mm = (me, team, consensus, book_count, std_dev, status, reason)
            continue
        edge, maker_only = result
        allow, status, reason = _resolve_wide_spread(maker_only, wide_reason)
        if not allow:
            _log(scan_log, me, team, kalshi_price, consensus, book_count, std_dev,
                 edge, status, reason, kalshi_side, maker_only=maker_only)
            if is_yes_side:
                pending_mm = (me, team, consensus, book_count, std_dev,
                              "spread_too_wide", reason)
            continue
        opportunities.append(ValueOpportunity(
            matched_event=me, outcome=outcome, team_name=team,
            consensus_prob=consensus, market_price=kalshi_price, edge=edge,
            market_url=_kalshi_url(km.ticker, km.event_ticker),
            bookmaker_count=book_count, consensus_std=std_dev,
            maker_only=maker_only,
        ))
        _log(scan_log, me, team, kalshi_price, consensus, book_count, std_dev,
             edge, status, reason, kalshi_side, maker_only=maker_only)
        logger.debug("VALUE H2H: %s — edge %.1f%% net  (consensus %.1f%% vs price %.1f%%, maker_only=%s, books=%d)",
                    team, edge*100, consensus*100, kalshi_price*100, maker_only, book_count)

    # Quote this ticker only if NEITHER side of it became a directional bet.
    if pending_mm is not None and len(opportunities) == opps_before:
        _maybe_mm_candidate(mm_candidates, *pending_mm)


def _detect_h2h_tie(me, event, km, min_edge, opportunities, scan_log, mm_candidates=None):
    # Draw is always priced directly off the ticker's own YES side.
    kalshi_side = "yes"
    consensus, book_count, std_dev = consensus_stats(event.bookmakers, "Draw")
    if consensus is None:
        _log(scan_log, me, "Draw", None, None, 0, 0.0, None,
             "no_consensus", "No sportsbook data for Draw", kalshi_side)
        return
    qcheck = _quality_check(km, book_count, std_dev, "h2h", is_draw=True)
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, "Draw", None, consensus, book_count, std_dev, None,
             status, reason, kalshi_side)
        return
    wide_reason = _spread_too_wide(km, "h2h", is_draw=True)
    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    result = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if result is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        if wide_reason:
            status, reason = "spread_too_wide", wide_reason
        else:
            status = "no_edge"
            reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, "Draw", kalshi_price, consensus, book_count, std_dev,
             best_edge, status, reason, kalshi_side)
        if wide_reason:
            _maybe_mm_candidate(mm_candidates, me, "Draw", consensus, book_count,
                                 std_dev, status, reason)
        return
    edge, maker_only = result
    allow, status, reason = _resolve_wide_spread(maker_only, wide_reason)
    if not allow:
        _log(scan_log, me, "Draw", kalshi_price, consensus, book_count, std_dev,
             edge, status, reason, kalshi_side, maker_only=maker_only)
        _maybe_mm_candidate(mm_candidates, me, "Draw", consensus, book_count,
                             std_dev, "spread_too_wide", reason)
        return
    opportunities.append(ValueOpportunity(
        matched_event=me, outcome=Outcome.DRAW, team_name="Draw",
        consensus_prob=consensus, market_price=kalshi_price, edge=edge,
        market_url=_kalshi_url(km.ticker, km.event_ticker),
        bookmaker_count=book_count, consensus_std=std_dev,
        maker_only=maker_only,
    ))
    _log(scan_log, me, "Draw", kalshi_price, consensus, book_count, std_dev,
         edge, status, reason, kalshi_side, maker_only=maker_only)
    logger.debug("VALUE DRAW: %s vs %s — edge %.1f%% net (maker_only=%s)",
                event.home_team, event.away_team, edge*100, maker_only)


# ── Totals ────────────────────────────────────────────────────────────────────

def _detect_totals(me, event, km, min_edge, opportunities, scan_log, mm_candidates=None):
    if km.threshold is None:
        reason = f"No threshold parsed from title: {km.title[:50]}"
        logger.debug("Skip totals %s — %s", km.ticker, reason)
        _log(scan_log, me, km.yes_team or "Over/Under", None, None,
             0, 0.0, None, "no_threshold", reason)
        return

    direction_label = "Over" if "over" in (km.yes_team or "").lower() else "Under"
    outcome_type = Outcome.OVER if direction_label == "Over" else Outcome.UNDER
    label = f"{direction_label} {km.threshold}"

    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers, direction_label, market_key="totals", point=km.threshold)

    logger.debug(
        "Totals consensus lookup: %s vs %s  label=%s threshold=%.2f  "
        "→ consensus=%s books=%d",
        event.home_team, event.away_team, label, km.threshold,
        f"{consensus*100:.1f}%" if consensus is not None else "None", book_count,
    )

    if consensus is None:
        # Debug: show what market keys ARE present in bookmakers data
        present_keys: dict[str, list] = {}
        for _b in event.bookmakers[:3]:  # sample first 3 books
            for _m in _b.get("markets", []):
                k = _m.get("key", "?")
                pts = [o.get("point") for o in _m.get("outcomes", []) if o.get("point") is not None]
                present_keys.setdefault(k, []).extend(pts)
        logger.debug(
            "  no_consensus detail: %s vs %s  threshold=%.2f  "
            "bookmaker market keys/points: %s",
            event.home_team, event.away_team, km.threshold,
            {k: sorted(set(v))[:5] for k, v in present_keys.items()},
        )
        _log(scan_log, me, label, None, None, 0, 0.0, None,
             "no_consensus", f"No sportsbook totals data for {label}", "yes")
        return
    qcheck = _quality_check(km, book_count, std_dev, "totals")
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, label, None, consensus, book_count, std_dev, None,
             status, reason, "yes")
        return

    # As in _detect_h2h: the Over (YES) and Under (NO) evaluations below are two
    # sides of ONE ticker, so the MM decision is held until both have run.
    opps_before = len(opportunities)
    pending_mm: tuple | None = None

    wide_reason = _spread_too_wide(km, "totals")
    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    result = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if result is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        if wide_reason:
            status, reason = "spread_too_wide", wide_reason
        else:
            status = "no_edge"
            reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             best_edge, status, reason, "yes")
        if wide_reason:
            pending_mm = (me, label, consensus, book_count, std_dev, status, reason)
    else:
        edge, maker_only = result
        allow, status, reason = _resolve_wide_spread(maker_only, wide_reason)
        if not allow:
            _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
                 edge, status, reason, "yes", maker_only=maker_only)
            pending_mm = (me, label, consensus, book_count, std_dev,
                          "spread_too_wide", reason)
        opportunities.append(ValueOpportunity(
            matched_event=me, outcome=outcome_type, team_name=label,
            consensus_prob=consensus, market_price=kalshi_price, edge=edge,
            market_url=_kalshi_url(km.ticker, km.event_ticker),
            bookmaker_count=book_count, consensus_std=std_dev,
            maker_only=maker_only,
        ))
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             edge, status, reason, "yes", maker_only=maker_only)
        logger.debug("VALUE TOTALS: %s (%s vs %s) — edge %.1f%% net (maker_only=%s)",
                    label, event.home_team, event.away_team, edge*100, maker_only)

    # ── Also evaluate the NO side (Under) of this Over market ─────────────────
    if direction_label == "Over":
        no_label = f"Under {km.threshold}"
        no_consensus = 1.0 - consensus
        no_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else (1.0 - km.yes_price)
        no_result = _eval_edge(no_consensus, no_price, km.spread, min_edge)
        if no_result is None:
            no_best = no_consensus - no_price
            eff_min = _effective_min_edge(no_price, min_edge)
            if wide_reason:
                no_status, reason = "spread_too_wide", wide_reason
            else:
                no_status = "no_edge"
                reason = f"Edge {no_best*100:.2f}% net below minimum {eff_min*100:.2f}%"
            _log(scan_log, me, no_label, no_price, no_consensus, book_count, std_dev,
                 no_best, no_status, reason, "no")
        else:
            no_edge, no_maker_only = no_result
            # The NO side is priced off (1 - yes_bid), so it is the SAME book and
            # the same spread -- the wide-spread rule applies identically here.
            no_allow, no_status, reason = _resolve_wide_spread(no_maker_only, wide_reason)
            if not no_allow:
                _log(scan_log, me, no_label, no_price, no_consensus, book_count, std_dev,
                     no_edge, no_status, reason, "no", maker_only=no_maker_only)
                no_result = None
        if no_result is not None:
            opportunities.append(ValueOpportunity(
                matched_event=me, outcome=Outcome.NO_OVER, team_name=no_label,
                consensus_prob=no_consensus, market_price=no_price, edge=no_edge,
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count, consensus_std=std_dev,
                maker_only=no_maker_only,
            ))
            _log(scan_log, me, no_label, no_price, no_consensus, book_count, std_dev,
                 no_edge, no_status, "Edge found on NO side — " + reason, "no",
                 maker_only=no_maker_only)
            logger.debug("VALUE TOTALS (NO/Under): %s (%s vs %s) — edge %.1f%% net (maker_only=%s)",
                        no_label, event.home_team, event.away_team, no_edge*100, no_maker_only)

    # Quote this ticker only if NEITHER the Over nor the Under side became a bet.
    if pending_mm is not None and len(opportunities) == opps_before:
        _maybe_mm_candidate(mm_candidates, *pending_mm)


# ── Spread ────────────────────────────────────────────────────────────────────

def _sb_team_match(kalshi_name: str, home: str, away: str) -> str | None:
    """
    Return the sportsbook team name (home or away) that best matches the Kalshi
    covering-team name, or None when the answer is AMBIGUOUS.

    Kalshi names are often abbreviated or shortened ("Minnesota" vs "Minnesota
    Twins"), so this uses word-overlap scoring. The critical case is when both
    sportsbook names score the SAME -- which happens whenever the two clubs share a
    city and Kalshi abbreviates the part that distinguishes them:

        kalshi "Chicago WS" vs home "Chicago Cubs"      -> {chicago} -> 25
        kalshi "Chicago WS" vs away "Chicago White Sox" -> {chicago} -> 25

    ("WS" matches neither "white" nor "sox" as a whole word.) This used to resolve
    with `home if score(home) >= score(away) else away`, i.e. a silent coin-flip that
    always picked HOME. On 2026-08-18 that bought 19 contracts of "Chicago WS wins by
    over 1.5 runs" while believing it was "Chicago Cubs -1.5" -- the opposite team.

    The damage is not just a mislabelled bet. The returned name is what
    consensus_stats() looks up, so a wrong answer prices ONE team's cover
    probability against the OTHER team's market price. That produced a fake 12.2%
    edge, and Kelly sizes hardest on the largest edges, so the bad match also
    produced the third-largest position ever placed. Returning None and skipping the
    market is always cheaper than guessing.
    """
    home_score, away_score = _sb_team_scores(kalshi_name, home, away)

    # No signal at all -- the Kalshi name resembles neither side.
    if home_score == 0 and away_score == 0:
        return None
    # Tied -- genuinely cannot tell which club this market covers.
    if home_score == away_score:
        return None
    return home if home_score > away_score else away


def _sb_team_scores(kalshi_name: str, home: str, away: str) -> tuple[int, int]:
    """
    (home_score, away_score) for a Kalshi team name against both sportsbook names.

    Split out from _sb_team_match so a rejection can be LOGGED with the numbers that
    caused it — "both scored 25" is what makes an ambiguity fixable later, whereas
    "ambiguous" alone just says it happened. See storage/db.py::log_ambiguous_match.
    """
    def _score(sb: str) -> int:
        kl = kalshi_name.lower()
        sl = sb.lower()
        if kl == sl:
            return 100
        if kl in sl or sl in kl:
            return 80
        # Count shared words (ignoring single-char tokens)
        k_words = {w for w in kl.split() if len(w) > 1}
        s_words = {w for w in sl.split() if len(w) > 1}
        return len(k_words & s_words) * 25

    return _score(home), _score(away)


def _detect_spread(me, event, km, min_edge, opportunities, scan_log, mm_candidates=None):
    if km.threshold is None:
        reason = f"No threshold parsed from title: {km.title[:50]}"
        _log(scan_log, me, km.yes_team or "Spread", None, None,
             0, 0.0, None, "no_threshold", reason)
        return

    if not km.yes_team:
        _log(scan_log, me, "Spread", None, None, 0, 0.0, None,
             "no_consensus", "No covering team in market data")
        return

    # Resolve Kalshi team name (may be shortened) to the sportsbook's canonical name
    # so that consensus_stats can match it via exact string comparison.
    covering_team = _sb_team_match(km.yes_team, event.home_team, event.away_team)
    if covering_team is None:
        # Ambiguous -- typically a same-city matchup where Kalshi abbreviates the
        # distinguishing part of the name ("Chicago WS" against Cubs vs White Sox).
        # Guessing here prices one team's consensus against the other team's market,
        # which manufactures a large fake edge; skip instead. See _sb_team_match().
        reason = (f"Cannot tell which team '{km.yes_team}' covers "
                  f"({event.away_team} @ {event.home_team})")
        logger.warning("Ambiguous spread team for %s — %s", km.ticker, reason)
        _log(scan_log, me, km.yes_team or "Spread", None, None, 0, 0.0, None,
             "ambiguous_team", reason, "yes")
        # Durable record: a refusal is a lost opportunity, not a fix. Tracked so the
        # matcher can be taught these names later -- see storage/db.py.
        from storage.db import log_ambiguous_match
        hs, as_ = _sb_team_scores(km.yes_team or "", event.home_team, event.away_team)
        log_ambiguous_match(
            context="spread_covering_team",
            kalshi_ticker=km.ticker,
            kalshi_name=km.yes_team or "",
            home_team=event.home_team,
            away_team=event.away_team,
            sport=event.sport_key,
            bet_type="spread",
            home_score=hs,
            away_score=as_,
        )
        return

    label = f"{covering_team} {km.threshold:+.1f}"
    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers, covering_team, market_key="spreads", point=km.threshold)

    if consensus is None:
        _log(scan_log, me, label, None, None, 0, 0.0, None,
             "no_consensus", f"No sportsbook spread data for {label}", "yes")
        return
    qcheck = _quality_check(km, book_count, std_dev, "spread")
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, label, None, consensus, book_count, std_dev, None,
             status, reason, "yes")
        return

    wide_reason = _spread_too_wide(km, "spread")
    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    result = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if result is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        if wide_reason:
            status, reason = "spread_too_wide", wide_reason
        else:
            status = "no_edge"
            reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             best_edge, status, reason, "yes")
        if wide_reason:
            _maybe_mm_candidate(mm_candidates, me, label, consensus, book_count,
                                 std_dev, status, reason)
        return
    edge, maker_only = result
    allow, status, reason = _resolve_wide_spread(maker_only, wide_reason)
    if not allow:
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             edge, status, reason, "yes", maker_only=maker_only)
        _maybe_mm_candidate(mm_candidates, me, label, consensus, book_count,
                             std_dev, "spread_too_wide", reason)
        return
    opportunities.append(ValueOpportunity(
        matched_event=me, outcome=Outcome.COVER, team_name=label,
        consensus_prob=consensus, market_price=kalshi_price, edge=edge,
        market_url=_kalshi_url(km.ticker, km.event_ticker),
        bookmaker_count=book_count, consensus_std=std_dev,
        maker_only=maker_only,
    ))
    _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
         edge, status, reason, "yes", maker_only=maker_only)
    logger.debug("VALUE SPREAD: %s (%s vs %s) — edge %.1f%% net (maker_only=%s)",
                label, event.home_team, event.away_team, edge*100, maker_only)


