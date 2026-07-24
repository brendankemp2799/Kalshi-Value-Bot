"""
Detects value opportunities by comparing Kalshi prices to the de-vigged
consensus from traditional sportsbooks (via The Odds API).

detect_value() accepts an optional scan_log list. When provided, every
evaluated candidate is appended — including rejections — so the dashboard
can show a full picture of why each bet was or wasn't placed.
"""
from __future__ import annotations

import logging
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
) -> float | None:
    """
    Return edge at ask price if it clears min_edge net of Kalshi's estimated fee,
    else None. The returned value is still the RAW edge (consensus - ask_price) —
    only the comparison threshold is fee-adjusted, not the stored value, since
    downstream code (CLV backfill, calibration dashboard) reconstructs
    consensus_prob as market_price + edge and would break if edge meant something
    else here.

    Only bets where ask_edge clears the fee-adjusted bar are accepted. The executor
    still tries to fill at mid first (maker, ~0% fee) for a better price, then falls
    back to ask (crosses the book — real taker fee, see
    core/kelly_calculator.py::fee_adjusted_breakeven_prob). Gating on the ask-price
    case ensures step 2 always clears the bar too and prevents blocking scans on
    illiquid mid-only fills.
    """
    ask_edge = consensus - ask_price
    _EPS = 1e-9  # floating-point tolerance: treat 1.9999999% as 2.0%
    if ask_edge >= _effective_min_edge(ask_price, min_edge) - _EPS:
        return ask_edge
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
) -> None:
    """Append one candidate record to scan_log if provided."""
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
        "maker_only":      0,
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
    if km.spread > qf["max_kalshi_spread"]:
        return (
            "spread_too_wide",
            f"Kalshi spread {km.spread*100:.1f}¢ > max {qf['max_kalshi_spread']*100:.0f}¢",
        )
    if std_dev > qf["high_uncertainty_std"] and book_count < qf["high_uncertainty_min_books"]:
        return (
            "high_uncertainty",
            f"High uncertainty: std_dev {std_dev:.3f} with only {book_count} books",
        )
    return None


def detect_value(
    matched_events: list[MatchedEvent],
    min_edge: float = 0.0,
    scan_log: list[dict] | None = None,
) -> list[ValueOpportunity]:
    opportunities: list[ValueOpportunity] = []

    for me in matched_events:
        event = me.odds_event
        km = me.kalshi_market
        is_draw = km.bet_type == "h2h" and me.kalshi_outcome == "tie"

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
            _detect_totals(me, event, km, min_edge, opportunities, scan_log)
        elif km.bet_type == "spread":
            _detect_spread(me, event, km, min_edge, opportunities, scan_log)
        elif me.kalshi_outcome == "tie":
            _detect_h2h_tie(me, event, km, min_edge, opportunities, scan_log)
        else:
            _detect_h2h(me, event, km, min_edge, opportunities, scan_log)

    logger.debug("Found %d value opportunities with positive edge", len(opportunities))
    return opportunities


# ── H2H ───────────────────────────────────────────────────────────────────────

def _detect_h2h(me, event, km, min_edge, opportunities, scan_log):
    # Soccer is 3-way (home / away / draw). Kalshi issues one binary market per
    # team (e.g. "Miami wins YES/NO"). The NO side of that market means "Miami
    # does NOT win" — which includes draws — NOT "opponent wins." Evaluating the
    # opponent's win probability against the NO price would produce phantom edge.
    # Skip the non-YES team for soccer so each team's Kalshi market is only
    # evaluated when it is explicitly the subject of the market.
    is_soccer = "soccer" in event.sport_key

    for outcome, team in [(Outcome.HOME, event.home_team), (Outcome.AWAY, event.away_team)]:
        if is_soccer:
            if me.kalshi_outcome == "yes" and outcome == Outcome.AWAY:
                continue
            if me.kalshi_outcome == "no" and outcome == Outcome.HOME:
                continue

        consensus, book_count, std_dev = consensus_stats(event.bookmakers, team)

        if consensus is None:
            logger.debug("No consensus prob for %s — skipping", team)
            _log(scan_log, me, team, None, None, 0, 0.0, None,
                 "no_consensus", "No sportsbook data for this team")
            continue

        qcheck = _quality_check(km, book_count, std_dev, "h2h")
        if qcheck:
            status, reason = qcheck
            logger.debug("Skip %s — %s", team, reason)
            _log(scan_log, me, team, None, consensus, book_count, std_dev, None,
                 status, reason)
            continue

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

        edge = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
        if edge is None:
            best_edge = consensus - kalshi_price
            eff_min = _effective_min_edge(kalshi_price, min_edge)
            reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
            _log(scan_log, me, team, kalshi_price, consensus, book_count, std_dev,
                 best_edge, "no_edge", reason)
            continue
        opportunities.append(ValueOpportunity(
            matched_event=me, outcome=outcome, team_name=team,
            consensus_prob=consensus, market_price=kalshi_price, edge=edge,
            market_url=_kalshi_url(km.ticker, km.event_ticker),
            bookmaker_count=book_count, consensus_std=std_dev,
        ))
        _log(scan_log, me, team, kalshi_price, consensus, book_count, std_dev,
             edge, "value", "Edge found — bet placed")
        logger.debug("VALUE H2H: %s — edge %.1f%% net  (consensus %.1f%% vs price %.1f%%, books=%d)",
                    team, edge*100, consensus*100, kalshi_price*100, book_count)


def _detect_h2h_tie(me, event, km, min_edge, opportunities, scan_log):
    consensus, book_count, std_dev = consensus_stats(event.bookmakers, "Draw")
    if consensus is None:
        _log(scan_log, me, "Draw", None, None, 0, 0.0, None,
             "no_consensus", "No sportsbook data for Draw")
        return
    qcheck = _quality_check(km, book_count, std_dev, "h2h", is_draw=True)
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, "Draw", None, consensus, book_count, std_dev, None,
             status, reason)
        return
    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if edge is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, "Draw", kalshi_price, consensus, book_count, std_dev,
             best_edge, "no_edge", reason)
        return
    opportunities.append(ValueOpportunity(
        matched_event=me, outcome=Outcome.DRAW, team_name="Draw",
        consensus_prob=consensus, market_price=kalshi_price, edge=edge,
        market_url=_kalshi_url(km.ticker, km.event_ticker),
        bookmaker_count=book_count, consensus_std=std_dev,
    ))
    _log(scan_log, me, "Draw", kalshi_price, consensus, book_count, std_dev,
         edge, "value", "Edge found — bet placed")
    logger.debug("VALUE DRAW: %s vs %s — edge %.1f%% net",
                event.home_team, event.away_team, edge*100)


# ── Totals ────────────────────────────────────────────────────────────────────

def _detect_totals(me, event, km, min_edge, opportunities, scan_log):
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
             "no_consensus", f"No sportsbook totals data for {label}")
        return
    qcheck = _quality_check(km, book_count, std_dev, "totals")
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, label, None, consensus, book_count, std_dev, None,
             status, reason)
        return

    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if edge is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             best_edge, "no_edge", reason)
    else:
        opportunities.append(ValueOpportunity(
            matched_event=me, outcome=outcome_type, team_name=label,
            consensus_prob=consensus, market_price=kalshi_price, edge=edge,
            market_url=_kalshi_url(km.ticker, km.event_ticker),
            bookmaker_count=book_count, consensus_std=std_dev,
        ))
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             edge, "value", "Edge found — bet placed")
        logger.debug("VALUE TOTALS: %s (%s vs %s) — edge %.1f%% net",
                    label, event.home_team, event.away_team, edge*100)

    # ── Also evaluate the NO side (Under) of this Over market ─────────────────
    if direction_label == "Over":
        no_label = f"Under {km.threshold}"
        no_consensus = 1.0 - consensus
        no_price = (1.0 - km.yes_bid) if km.yes_bid > 0 else (1.0 - km.yes_price)
        no_edge = _eval_edge(no_consensus, no_price, km.spread, min_edge)
        if no_edge is None:
            no_best = no_consensus - no_price
            eff_min = _effective_min_edge(no_price, min_edge)
            reason = f"Edge {no_best*100:.2f}% net below minimum {eff_min*100:.2f}%"
            _log(scan_log, me, no_label, no_price, no_consensus, book_count, std_dev,
                 no_best, "no_edge", reason)
        else:
            opportunities.append(ValueOpportunity(
                matched_event=me, outcome=Outcome.NO_OVER, team_name=no_label,
                consensus_prob=no_consensus, market_price=no_price, edge=no_edge,
                market_url=_kalshi_url(km.ticker, km.event_ticker),
                bookmaker_count=book_count, consensus_std=std_dev,
            ))
            _log(scan_log, me, no_label, no_price, no_consensus, book_count, std_dev,
                 no_edge, "value", "Edge found on NO side — bet placed")
            logger.debug("VALUE TOTALS (NO/Under): %s (%s vs %s) — edge %.1f%% net",
                        no_label, event.home_team, event.away_team, no_edge*100)


# ── Spread ────────────────────────────────────────────────────────────────────

def _sb_team_match(kalshi_name: str, home: str, away: str) -> str:
    """
    Return the sportsbook team name (home or away) that best matches the
    Kalshi covering-team name. Kalshi names are often abbreviated or shortened
    (e.g. "Minnesota" vs "Minnesota Twins"), so we use word-overlap scoring.
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

    return home if _score(home) >= _score(away) else away


def _detect_spread(me, event, km, min_edge, opportunities, scan_log):
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

    label = f"{covering_team} {km.threshold:+.1f}"
    consensus, book_count, std_dev = consensus_stats(
        event.bookmakers, covering_team, market_key="spreads", point=km.threshold)

    if consensus is None:
        _log(scan_log, me, label, None, None, 0, 0.0, None,
             "no_consensus", f"No sportsbook spread data for {label}")
        return
    qcheck = _quality_check(km, book_count, std_dev, "spread")
    if qcheck:
        status, reason = qcheck
        _log(scan_log, me, label, None, consensus, book_count, std_dev, None,
             status, reason)
        return

    kalshi_price = km.yes_ask if km.yes_ask > 0 else km.yes_price
    edge = _eval_edge(consensus, kalshi_price, km.spread, min_edge)
    if edge is None:
        best_edge = consensus - kalshi_price
        eff_min = _effective_min_edge(kalshi_price, min_edge)
        reason = f"Edge {best_edge*100:.2f}% net below minimum {eff_min*100:.2f}%"
        _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
             best_edge, "no_edge", reason)
        return
    opportunities.append(ValueOpportunity(
        matched_event=me, outcome=Outcome.COVER, team_name=label,
        consensus_prob=consensus, market_price=kalshi_price, edge=edge,
        market_url=_kalshi_url(km.ticker, km.event_ticker),
        bookmaker_count=book_count, consensus_std=std_dev,
    ))
    _log(scan_log, me, label, kalshi_price, consensus, book_count, std_dev,
         edge, "value", "Edge found — bet placed")
    logger.debug("VALUE SPREAD: %s (%s vs %s) — edge %.1f%% net",
                label, event.home_team, event.away_team, edge*100)


