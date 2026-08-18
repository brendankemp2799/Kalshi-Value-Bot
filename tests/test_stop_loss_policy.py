"""Stop-loss policy: per-bet-type thresholds + breach confirmation.

Both behaviours here replaced something that was measured to be losing money, so
these tests exist to stop a future refactor quietly reinstating either:

  1. ONE THRESHOLD FOR EVERY BET TYPE. Of positions that fell 20c below entry, 10.7%
     of totals still won vs 31.8% of h2h -- so a 20c stop is strongly right on totals
     and wrong on h2h. A flat threshold splits the difference and gets both wrong.
     (research/experiments/2026-08-17-stop-loss-by-bet-type.md)

  2. FIRING ON A SINGLE QUOTE. Position #315 was closed by one thin ~24c-wide spike at
     the end of the 1st inning on a game that finished well over the total. The old fix
     was a time ramp that widened the totals stop across the whole early game; it cost
     -5.4pp of equal-weighted ROI. The confirmation counter fixes the actual cause.

evaluate_stop_loss is pure, so all of this is testable without a DB or a network.
"""
from __future__ import annotations

import pytest

import config
from execution.risk_manager import ActionKind, evaluate_stop_loss, _stop_loss_move


def pos(**kw):
    """A positions row. dict supports .keys(), which is all the code needs."""
    base = {
        "id": 1,
        "side": "yes",
        "market_price": 0.50,
        "bet_type": "h2h",
        "sport": "baseball_mlb",
        "commence_time": None,
        "stop_breach_count": 0,
    }
    base.update(kw)
    return base


def mkt(yes_bid=None, yes_ask=None):
    m = {}
    if yes_bid is not None:
        m["yes_bid_dollars"] = yes_bid
    if yes_ask is not None:
        m["yes_ask_dollars"] = yes_ask
    return m


@pytest.fixture(autouse=True)
def _fixed_policy(monkeypatch):
    """Pin the policy so these tests assert on BEHAVIOUR, not on tuned constants."""
    monkeypatch.setattr(config, "STOP_LOSS_MOVE", 0.30)
    monkeypatch.setattr(config, "STOP_LOSS_MOVE_BY_BET_TYPE", {"totals": 0.20})
    monkeypatch.setattr(config, "STOP_LOSS_CONFIRM_CHECKS", 2)


# ── 1. per-bet-type thresholds ──────────────────────────────────────────────────

def test_totals_uses_the_tight_threshold():
    assert _stop_loss_move(pos(bet_type="totals")) == 0.20


def test_h2h_uses_the_wide_threshold():
    assert _stop_loss_move(pos(bet_type="h2h")) == 0.30


def test_spread_falls_back_to_the_h2h_threshold():
    """spread isn't in the map; it must take the default, not 0 or a KeyError."""
    assert _stop_loss_move(pos(bet_type="spread")) == 0.30


def test_unknown_and_missing_bet_type_fall_back_to_the_default():
    assert _stop_loss_move(pos(bet_type="parlay_moon_shot")) == 0.30
    assert _stop_loss_move(pos(bet_type=None)) == 0.30


def test_the_same_price_stops_a_totals_position_but_not_an_h2h_one():
    """THE point of the split, as one assertion: entry 0.50, price 0.28.

    -22c is past the totals stop (0.30) and short of the h2h stop (0.20)."""
    p = pos(bet_type="totals", stop_breach_count=1)   # already confirmed once
    assert evaluate_stop_loss(p, mkt(yes_bid=0.28)).kind == ActionKind.TRIGGER_CLOSE

    p = pos(bet_type="h2h", stop_breach_count=1)
    # not past the h2h stop, so it must not close (and it clears the stale count)
    assert evaluate_stop_loss(p, mkt(yes_bid=0.28)).kind != ActionKind.TRIGGER_CLOSE


def test_no_side_threshold_is_measured_from_the_no_price():
    """A NO position's exit price is 1 - yes_ask; the threshold applies to that."""
    p = pos(side="no", bet_type="totals", market_price=0.50, stop_breach_count=1)
    # yes_ask 0.72 -> NO exit price 0.28, i.e. -22c. Past the 0.20 totals stop.
    assert evaluate_stop_loss(p, mkt(yes_ask=0.72)).kind == ActionKind.TRIGGER_CLOSE
    # yes_ask 0.65 -> NO exit price 0.35, i.e. -15c. Not past it.
    assert evaluate_stop_loss(p, mkt(yes_ask=0.65)).kind != ActionKind.TRIGGER_CLOSE


# ── 2. breach confirmation (the position-#315 defence) ──────────────────────────

def test_a_single_spike_does_not_close_the_position():
    """#315, exactly: one thin print below the stop must only ARM the counter."""
    a = evaluate_stop_loss(pos(bet_type="totals"), mkt(yes_bid=0.25))
    assert a.kind == ActionKind.UPDATE_BREACH
    assert a.breach_count == 1


def test_a_sustained_breach_closes_the_position():
    a = evaluate_stop_loss(pos(bet_type="totals", stop_breach_count=1), mkt(yes_bid=0.25))
    assert a.kind == ActionKind.TRIGGER_CLOSE
    assert a.exit_price == 0.25


def test_recovery_resets_the_counter():
    """#315's price came straight back. One good print must clear the count."""
    a = evaluate_stop_loss(pos(bet_type="totals", stop_breach_count=1), mkt(yes_bid=0.45))
    assert a.kind == ActionKind.UPDATE_BREACH
    assert a.breach_count == 0


def test_spike_recover_spike_does_not_close():
    """Two breaches that are not CONSECUTIVE must not add up to a confirmation."""
    p = pos(bet_type="totals")
    a = evaluate_stop_loss(p, mkt(yes_bid=0.25))
    assert a.breach_count == 1
    p["stop_breach_count"] = a.breach_count

    a = evaluate_stop_loss(p, mkt(yes_bid=0.45))       # recovers
    assert a.breach_count == 0
    p["stop_breach_count"] = a.breach_count

    a = evaluate_stop_loss(p, mkt(yes_bid=0.25))       # breaches again
    assert a.kind == ActionKind.UPDATE_BREACH, "non-consecutive breaches must not confirm"


def test_no_reset_write_when_there_is_nothing_to_reset():
    """A healthy position must not write to the DB every single check."""
    assert evaluate_stop_loss(pos(stop_breach_count=0), mkt(yes_bid=0.45)).kind == ActionKind.NONE


def test_confirm_checks_of_one_closes_immediately():
    """Setting the counter to 1 must reproduce the old fire-on-first-touch behaviour."""
    config.STOP_LOSS_CONFIRM_CHECKS = 1
    a = evaluate_stop_loss(pos(bet_type="totals"), mkt(yes_bid=0.25))
    assert a.kind == ActionKind.TRIGGER_CLOSE


def test_confirm_checks_of_zero_does_not_disable_the_stop():
    """A misconfigured 0 must not mean 'never fires' -- it clamps to 1."""
    config.STOP_LOSS_CONFIRM_CHECKS = 0
    a = evaluate_stop_loss(pos(bet_type="totals"), mkt(yes_bid=0.25))
    assert a.kind == ActionKind.TRIGGER_CLOSE


# ── 3. the empty-book guard ─────────────────────────────────────────────────────

def test_an_empty_book_is_not_a_price_of_zero():
    """A yes_bid of 0 means nobody is bidding. Reading it as a real price trips every
    threshold at once and tries to sell at 0.00 into a book that cannot fill it."""
    for m in (mkt(yes_bid=0.0), mkt(yes_bid=None), mkt()):
        assert evaluate_stop_loss(pos(stop_breach_count=1), m).kind == ActionKind.NONE


def test_an_empty_book_on_the_no_side_is_not_a_price_of_zero():
    """yes_ask of 1.0 -> NO exit price 0.0. Same trap, other side."""
    assert evaluate_stop_loss(
        pos(side="no", stop_breach_count=1), mkt(yes_ask=1.0)).kind == ActionKind.NONE


def test_a_missing_quote_does_not_reset_a_real_breach():
    """No quote is not evidence of recovery. Losing the count on a dropped quote would
    let a genuinely collapsing position dodge the stop indefinitely."""
    a = evaluate_stop_loss(pos(bet_type="totals", stop_breach_count=1), mkt())
    assert a.kind == ActionKind.NONE
    assert a.breach_count is None, "must not write a reset from a missing quote"


# ── 4. the trigger level is reported for slippage measurement ───────────────────

def test_trigger_close_reports_the_level_that_fired():
    """exit_price is the fill; trigger_price is the level. The difference is slippage,
    which until now had to be inferred from P&L -- and was wrong by 12c when it was."""
    a = evaluate_stop_loss(pos(bet_type="totals", market_price=0.50,
                               stop_breach_count=1), mkt(yes_bid=0.22))
    assert a.kind == ActionKind.TRIGGER_CLOSE
    assert a.exit_price == 0.22
    assert a.trigger_price == pytest.approx(0.30)


def test_rows_without_the_breach_column_still_evaluate():
    """Positions opened before the migration have no stop_breach_count key."""
    legacy = {"id": 1, "side": "yes", "market_price": 0.50, "bet_type": "totals"}
    assert evaluate_stop_loss(legacy, mkt(yes_bid=0.25)).kind == ActionKind.UPDATE_BREACH
