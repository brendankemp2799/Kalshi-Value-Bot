"""
Trailing stop and stop-loss risk management for open positions.

Kalshi has no native stop/conditional order type (verified against their API
reference), so both of these simulate one via polling — each scan, compute the
currently achievable exit price for a position (same convention as market_price at
entry) and decide whether to close it. They're symmetric and independent:

  Trailing stop (protects gains): track the best price seen since entry (peak_price),
  and once armed (peak has moved far enough favorably), trigger a full-position close
  if price retraces to the trailing stop level.

      arm:   peak_price - entry_price >= _dynamic_arm_move(pos)
      stop:  entry_price + config.TRAILING_STOP_LOCK_FRACTION * (peak_price - entry_price)

  The arm move itself is time-into-game dependent, not a flat constant — see
  _dynamic_arm_move() below. A move minutes into a game has far more time to revert
  than the same move with the game nearly over, so the threshold linearly ramps down
  from config.TRAILING_STOP_ARM_MOVE_EARLY at kickoff to config.TRAILING_STOP_ARM_MOVE_LATE
  by the sport's expected game duration (config.SPORT_EXPECTED_DURATION_MINUTES).

  The stop rises as peak_price rises — protects more of the gain the further a winner
  runs, with no cap on the upside while the position keeps working.

  Stop loss (limits losses): no peak tracking (unlike the trailing stop, it can't
  ratchet). Triggers a full-position close as soon as price has moved against entry
  by a threshold. Added after real bet history showed positions that never armed the
  trailing stop (and so had no risk management applied at all) were the dominant
  source of losses.

      stop:  entry_price - _stop_loss_move(pos)      [confirmed over N checks]

  The threshold is per bet type (config.STOP_LOSS_MOVE_BY_BET_TYPE), not flat, and
  not time-ramped. Measured 2026-08-17: of positions that fell 20c below entry, 10.7%
  of totals still won vs 31.8% of h2h. Since stopping beats holding exactly when the
  exit proceeds exceed the hold-win-probability, that single fact makes a 20c stop
  strongly correct on totals and wrong on h2h. See _stop_loss_move() and the config
  block for the full derivation.

  A trigger must also PERSIST for config.STOP_LOSS_CONFIRM_CHECKS consecutive checks
  before the position is cut (tracked in positions.stop_breach_count, the same
  persist-risk-state-on-the-row pattern peak_price already uses for the trailing
  stop). This replaced the old totals-only time ramp: both exist to stop a thin
  one-tick quote spike from closing a good position (position #315), but the ramp paid
  for that with a wider stop across the whole early game, while a confirmation counter
  pays ~30s of extra exposure and nothing else.

The two never conflict — the trailing stop only ever triggers above entry price, the
stop-loss only ever triggers below it.

See config.TRAILING_STOP_*/config.ENABLE_TRAILING_STOP and
config.STOP_LOSS_MOVE/config.STOP_LOSS_MOVE_BY_BET_TYPE/config.ENABLE_STOP_LOSS for
the parameters and master on/off switches. Only ever call
execute_trailing_stop()/execute_stop_loss() from the main scan loop (main.py) — never
from the dashboard — since they can place real orders.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)


class ActionKind(str, Enum):
    NONE = "none"
    UPDATE_PEAK = "update_peak"
    UPDATE_BREACH = "update_breach"   # stop level breached, not yet confirmed
    TRIGGER_CLOSE = "trigger_close"


@dataclass
class Action:
    kind: ActionKind
    peak_price: float | None = None    # set for UPDATE_PEAK
    exit_price: float | None = None    # set for TRIGGER_CLOSE
    breach_count: int | None = None    # set for UPDATE_BREACH
    trigger_price: float | None = None  # set for TRIGGER_CLOSE: the level that fired,
                                        # kept alongside the realised fill so slippage
                                        # is measured rather than inferred


def _achievable_exit_price(side: str, market: dict) -> float | None:
    """
    Price we could actually exit at right now, same convention as market_price:
    YES position -> yes_bid_dollars ; NO position -> 1 - yes_ask_dollars.
    Returns None if the market has no live quote on that side.

    An EMPTY book quotes 0 (or, for a NO position, a yes_ask of 1) — that means "no
    one is bidding", not "the price is zero". Returning it as a real price made every
    stop threshold trigger at once, at an exit price of 0.00, on a position that by
    definition could not have been sold anyway. It has never happened in production
    (no live position has quoted below 2c) but it did silently invalidate the first
    run of the stop-loss backtest, which is exactly how it would present live.
    """
    try:
        if side == "yes":
            bid = market.get("yes_bid_dollars")
            price = float(bid) if bid not in (None, "") else None
        else:
            ask = market.get("yes_ask_dollars")
            price = (1.0 - float(ask)) if ask not in (None, "") else None
    except (TypeError, ValueError):
        return None

    if price is None or price <= 0.0:
        return None
    return price


def _elapsed_fraction(pos) -> float:
    """
    Fraction of the sport's expected game duration elapsed since commence_time,
    clamped to [0, 1] (pre-game and overtime/extra-innings both clamp rather than
    extrapolate). Used only by _dynamic_arm_move() now — the totals stop-loss ramp
    that also called it was removed on 2026-08-17 in favour of a per-bet-type
    threshold plus a confirmation counter (see _stop_loss_move).

    Falls back to 0.0 (i.e. "just started" — the safer, more tolerant end for every
    current caller) if `commence_time` is missing or unparseable, same fallback
    convention as the existing commence_time handling in execution/auto_settle.py.
    """
    commence_str = pos["commence_time"] if "commence_time" in pos.keys() else None
    if not commence_str:
        return 0.0

    try:
        commence = datetime.fromisoformat(commence_str)
        if commence.tzinfo is None:
            commence = commence.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0

    sport = pos["sport"] if "sport" in pos.keys() else None
    duration_minutes = config.SPORT_EXPECTED_DURATION_MINUTES.get(
        sport, config.SPORT_EXPECTED_DURATION_DEFAULT_MINUTES
    )

    elapsed_minutes = (datetime.now(timezone.utc) - commence).total_seconds() / 60.0
    return max(0.0, min(1.0, elapsed_minutes / duration_minutes))


def _dynamic_arm_move(pos) -> float:
    """
    Trailing-stop arm threshold as a function of elapsed time since game start —
    larger (harder to arm, more tolerant of noise) near kickoff, smaller (arms more
    readily, protects gains sooner) as the game approaches/passes its expected
    duration. Linear ramp between config.TRAILING_STOP_ARM_MOVE_EARLY and
    config.TRAILING_STOP_ARM_MOVE_LATE.
    """
    elapsed_fraction = _elapsed_fraction(pos)
    early = config.TRAILING_STOP_ARM_MOVE_EARLY
    late = config.TRAILING_STOP_ARM_MOVE_LATE
    return early - elapsed_fraction * (early - late)


def _stop_loss_move(pos) -> float:
    """
    Adverse move that triggers a cut, per bet type. Not time-dependent (unlike
    _dynamic_arm_move above, and unlike the totals ramp this replaced on 2026-08-17).

    WHY THIS IS PER BET TYPE. Stopping out realises `s` per contract with certainty;
    holding is worth `p`, the probability the position still wins from here. So
    stopping beats holding exactly when `s > p` — the threshold question is really an
    empirical one about how often a position recovers from a given adverse move.
    Measured across every settled position's real candlestick path (n=92):

        drop    totals: s      p      s-p        h2h: s      p      s-p
        0.10            0.335  0.286  +0.050          0.284  0.367  -0.082
        0.20            0.230  0.107  +0.123          0.220  0.318  -0.099
        0.30            0.134  0.040  +0.094          0.154  0.143  +0.011

    Totals says STOP at every threshold tested (a plateau, i.e. a real effect rather
    than a fitted point); h2h says HOLD at every threshold but one knife-edge. Same
    base win rate (44.9% vs 47.4%), opposite behaviour after a move — see the config
    block for the accumulation-vs-mean-reversion mechanism.
    """
    bet_type = pos["bet_type"] if "bet_type" in pos.keys() else None
    return config.STOP_LOSS_MOVE_BY_BET_TYPE.get(bet_type, config.STOP_LOSS_MOVE)


def evaluate_trailing_stop(pos, market: dict) -> Action:
    """
    Pure decision function — no side effects, no I/O. `pos` is a positions row
    (sqlite3.Row) with at least: side, market_price, peak_price, commence_time, sport.
    """
    side = (pos["side"] or "yes").lower()
    entry_price = pos["market_price"]
    peak_price = pos["peak_price"] if pos["peak_price"] is not None else entry_price

    achievable = _achievable_exit_price(side, market)
    if achievable is None:
        return Action(kind=ActionKind.NONE)

    _EPS = 1e-9  # floating-point tolerance, e.g. 0.58-0.48 == 0.09999999999999998 in binary float

    if achievable > peak_price + _EPS:
        return Action(kind=ActionKind.UPDATE_PEAK, peak_price=achievable)

    armed = (peak_price - entry_price) >= _dynamic_arm_move(pos) - _EPS
    if not armed:
        return Action(kind=ActionKind.NONE)

    stop_level = entry_price + config.TRAILING_STOP_LOCK_FRACTION * (peak_price - entry_price)
    if achievable <= stop_level + _EPS:
        return Action(kind=ActionKind.TRIGGER_CLOSE, exit_price=achievable)

    return Action(kind=ActionKind.NONE)


def evaluate_stop_loss(pos, market: dict) -> Action:
    """
    Pure decision function — no side effects, no I/O. `pos` is a positions row
    (sqlite3.Row) with at least: side, market_price, bet_type, stop_breach_count.

    Carries one piece of state, positions.stop_breach_count: the number of CONSECUTIVE
    checks the price has sat at/below the stop level. The position is only cut once
    that reaches config.STOP_LOSS_CONFIRM_CHECKS; any check back above the level
    resets it to 0. A single bad print therefore cannot close a position, which is what
    makes the tight totals stop safe (see the module docstring and position #315).

    Returning UPDATE_BREACH rather than mutating `pos` keeps this pure and testable,
    exactly like UPDATE_PEAK in evaluate_trailing_stop().
    """
    side = (pos["side"] or "yes").lower()
    entry_price = pos["market_price"]
    breaches = (pos["stop_breach_count"] or 0) if "stop_breach_count" in pos.keys() else 0

    achievable = _achievable_exit_price(side, market)
    if achievable is None:
        # No quote is not evidence of recovery, so don't reset the counter — but don't
        # advance it either. A stop we cannot execute is not a stop.
        return Action(kind=ActionKind.NONE)

    _EPS = 1e-9

    stop_level = entry_price - _stop_loss_move(pos)
    if achievable > stop_level + _EPS:
        if breaches:
            return Action(kind=ActionKind.UPDATE_BREACH, breach_count=0)
        return Action(kind=ActionKind.NONE)

    confirmed = breaches + 1
    if confirmed >= max(1, config.STOP_LOSS_CONFIRM_CHECKS):
        return Action(
            kind=ActionKind.TRIGGER_CLOSE,
            exit_price=achievable,
            trigger_price=stop_level,
        )
    return Action(kind=ActionKind.UPDATE_BREACH, breach_count=confirmed)


def _fetch_live_contract_count(ticker: str) -> float | None:
    """Authoritative held-contract count from Kalshi's own portfolio data (free, not credit-metered)."""
    try:
        from data.kalshi_auth import auth_headers, session
        url = "https://external-api.kalshi.com/trade-api/v2/portfolio/positions"
        headers = auth_headers("GET", url)
        resp = session().get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        for p in resp.json().get("market_positions", []):
            if p.get("ticker") == ticker:
                return abs(float(p.get("position_fp", 0)))
        return None
    except Exception as e:
        logger.warning("Could not fetch live Kalshi position for %s: %s", ticker, e)
        return None


def _execute_close(pos, exit_price: float, is_paper: bool, reason: str,
                   trigger_price: float | None = None) -> bool:
    """
    Shared TRIGGER_CLOSE execution, used by both execute_trailing_stop() and
    execute_stop_loss() — fetch live contract count, close via IOC, record P&L under
    the given `reason` (so trailing-stop and stop-loss exits stay distinguishable in
    `positions.close_reason` for future analysis, same as `close_reason` already
    distinguishes either from natural settlement). Returns True only when the
    position was actually closed this call — callers (auto_settle.py) use this to
    decide whether to also skip natural settlement that cycle; a close attempt that
    merely fails must NOT be treated as "handled," or a position can end up with no
    risk management applied at all for as long as the failure keeps recurring.
    """
    from storage.db import close_position_early

    pos_id = pos["id"]
    side = (pos["side"] or "yes").lower()
    ticker = pos["market_ticker"]

    if is_paper:
        pnl = close_position_early(pos_id, exit_price, reason=reason,
                                   trigger_price=trigger_price)
        logger.info(
            "[PAPER] %s triggered: position #%d closed @ %.4f  P&L=$%.2f",
            reason, pos_id, exit_price, pnl,
        )
        return True

    contracts = _fetch_live_contract_count(ticker)
    if not contracts or contracts <= 0:
        logger.warning(
            "%s triggered for position #%d but no live Kalshi position "
            "found for %s — skipping close this cycle", reason, pos_id, ticker,
        )
        return False

    from execution.kalshi_executor import close_position
    order_id, status, fail_reason, filled, fill_price, exit_fee = close_position(
        ticker, side, contracts, exit_price,
    )
    if status != "submitted" or filled <= 0:
        logger.warning(
            "%s close FAILED for position #%d (%s): %s — will retry next scan",
            reason, pos_id, ticker, fail_reason,
        )
        return False

    if filled < contracts:
        # Partial-fill accounting isn't automated (v1 is a full-position-exit design,
        # not a partial-position ledger) — leave the position open and flag loudly
        # for manual review rather than silently misrecord P&L.
        logger.error(
            "%s PARTIAL fill for position #%d (%s): closed %g/%g contracts "
            "@ %.4f (order_id=%s). Position left OPEN for manual review — remaining "
            "contracts are still exposed on Kalshi.",
            reason, pos_id, ticker, filled, contracts, fill_price, order_id,
        )
        return False

    # fill_price is what Kalshi actually gave us; trigger_price is the level that
    # fired. Storing BOTH is the point -- slippage has had to be backed out of P&L
    # until now, and that inference was wrong by 12c the first time it was tried.
    pnl = close_position_early(pos_id, fill_price, reason=reason, exit_fee=exit_fee,
                               trigger_price=trigger_price)
    logger.info(
        "[LIVE] %s triggered: position #%d closed %g contracts @ %.4f  "
        "P&L=$%.2f  (order_id=%s, exit_fee=$%.4f)",
        reason, pos_id, filled, fill_price, pnl, order_id, exit_fee,
    )
    return True


def execute_trailing_stop(pos, action: Action, is_paper: bool) -> bool:
    """
    Apply the decision from evaluate_trailing_stop(): update DB state or close the
    position. Returns True only when the position was actually closed this call.
    """
    from storage.db import set_peak_price

    if action.kind == ActionKind.NONE:
        return False

    if action.kind == ActionKind.UPDATE_PEAK:
        set_peak_price(pos["id"], action.peak_price)
        return False

    return _execute_close(pos, action.exit_price, is_paper, reason="trailing_stop",
                          trigger_price=action.trigger_price)


def execute_stop_loss(pos, action: Action, is_paper: bool) -> bool:
    """
    Apply the decision from evaluate_stop_loss(): advance/reset the breach counter, or
    close the position once the breach is confirmed. Returns True only when the
    position was actually closed this call.
    """
    from storage.db import set_stop_breach_count

    if action.kind == ActionKind.NONE:
        return False

    if action.kind == ActionKind.UPDATE_BREACH:
        set_stop_breach_count(pos["id"], action.breach_count)
        return False

    return _execute_close(
        pos, action.exit_price, is_paper, reason="stop_loss",
        trigger_price=action.trigger_price,
    )
