"""Tests for run_mm_tick()'s requote behaviour and decision logging.

These pin the two defects that explain the user-visible symptoms "I do not see
that many orders placed" and "I do not have clear visibility into the logic for
how/when it decided on opportunities":

  - every resting leg was cancelled and re-placed on EVERY tick, unconditionally,
    even when the recomputed price was identical. Kalshi fills same-price orders
    in time priority, so at MM_INTERVAL_SECONDS=30 that surrendered queue
    position 120 times an hour, permanently behind anyone who quoted once and
    left it.
  - every rejection path was logger.debug (off in production) and the module's
    only INFO line was the fill log, so a tick that evaluated ~60 candidates and
    quoted 1 emitted nothing at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import execution.market_maker as mm
from core.market_matcher import MatchedEvent
from data.kalshi_client import KalshiMarket
from data.odds_fetcher import OddsEvent


def _market(ticker="T1", bid=0.45, ask=0.55, volume=5000.0,
            volume_24h=5000.0) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker, title="A at B Winner?", yes_team="A", no_team="B",
        yes_price=(bid + ask) / 2, no_price=1 - (bid + ask) / 2,
        yes_bid=bid, yes_ask=ask, volume=volume, volume_24h=volume_24h,
        close_time="2026-09-01T00:00:00Z", category="Sports",
        event_ticker="KXTEST-26AUG20AB", bet_type="h2h",
    )


def _candidate(km: KalshiMarket, consensus=0.50) -> dict:
    event = OddsEvent(
        event_id="e1", sport_key="soccer_usa_mls", home_team="A", away_team="B",
        commence_time=datetime.now(timezone.utc) + timedelta(days=1),
    )
    return {
        "matched_event": MatchedEvent(odds_event=event, kalshi_market=km,
                                      kalshi_outcome="yes"),
        "team_name": "A",
        "consensus_prob": consensus,
        "bookmaker_count": 5,
        "consensus_std": 0.01,
        "kalshi_spread": km.spread,
    }


class _Tracker:
    def is_allowed(self, opp, stake, is_mm=False):
        return True, ""


class _BM:
    bankroll = 1000.0
    mm_exposure = 0.0


@pytest.fixture
def rig(monkeypatch):
    """Stub every side effect run_mm_tick reaches for, and record the calls."""
    import execution.kalshi_executor as ke
    state = {
        "placed": [],      # (ticker, side, price, count)
        "cancelled": [],   # order_id
        "decisions": [],   # rows handed to db.log_mm_decisions
        "fills": {},       # order_id -> filled count reported by get_order_status
        "next_id": [0],
    }

    def _place(ticker, side, price, count):
        state["next_id"][0] += 1
        oid = f"o{state['next_id'][0]}"
        state["placed"].append((ticker, side, price, count))
        return oid, 0.0, 0.0

    monkeypatch.setattr(ke, "place_resting_quote", _place)
    monkeypatch.setattr(ke, "cancel_quote",
                        lambda oid, ticker: (state["cancelled"].append(oid), True)[1])
    monkeypatch.setattr(ke, "get_order_status",
                        lambda oid: {"fill_count_fp": state["fills"].get(oid, 0.0)})
    monkeypatch.setattr(ke, "order_fee_paid", lambda oid: 0.0)

    monkeypatch.setattr(mm.db, "position_exists_for_order_id", lambda oid: False)
    monkeypatch.setattr(mm.db, "get_open_positions", lambda is_paper: [])
    monkeypatch.setattr(mm.db, "add_position", lambda **kw: 1)
    monkeypatch.setattr(mm.db, "log_mm_decisions",
                        lambda tick_id, entries: state["decisions"].extend(entries))
    # A live tick would otherwise reach Kalshi to rebuild _resting_quotes.
    monkeypatch.setattr(mm, "_startup_synced", True, raising=False)
    mm._resting_quotes.clear()
    return state


def _tick(cands, markets, is_paper=False):
    return mm.run_mm_tick(cands, {m.ticker: m for m in markets},
                          _Tracker(), _BM(), is_paper)


# ── #3 queue priority ─────────────────────────────────────────────────────────

def test_unchanged_quote_is_not_cancelled_and_replaced(rig):
    """THE queue-priority fix. Two identical ticks against an unchanged book must
    place two legs in total, not four, and cancel nothing."""
    km = _market()
    cands = [_candidate(km)]

    _tick(cands, [km])
    assert len(rig["placed"]) == 2, "first tick places YES and NO"
    first_ids = {s: leg["order_id"]
                 for s, leg in mm._resting_quotes["T1"].items() if leg}

    _tick(cands, [km])
    assert len(rig["placed"]) == 2, "second tick must place nothing new"
    assert rig["cancelled"] == [], "an unchanged quote must not be cancelled"
    still = {s: leg["order_id"]
             for s, leg in mm._resting_quotes["T1"].items() if leg}
    assert still == first_ids, "the original order ids must survive"


def test_moved_price_does_cancel_and_replace(rig):
    """The skip must be price-conditional, not a blanket 'never requote'."""
    km = _market()
    cands = [_candidate(km)]
    _tick(cands, [km])
    placed_before = len(rig["placed"])

    moved = _market(bid=0.38, ask=0.52)          # book moved, quote must follow
    _tick([_candidate(moved, consensus=0.45)], [moved])
    assert len(rig["placed"]) > placed_before
    assert rig["cancelled"], "the stale leg must be cancelled before replacing"


def test_partial_fill_is_replaced_not_kept(rig):
    """A partly filled leg has already spent its queue position and is no longer
    the size it was sized to, so keeping it would misstate exposure."""
    km = _market()
    cands = [_candidate(km)]
    _tick(cands, [km])
    yes_id = mm._resting_quotes["T1"]["yes"]["order_id"]
    count = 0
    for t, side, price, c in rig["placed"]:
        if side == "yes":
            count = c
    rig["fills"][yes_id] = count - 1          # partially filled

    placed_before = len(rig["placed"])
    _tick(cands, [km])
    assert yes_id in rig["cancelled"]
    assert len(rig["placed"]) > placed_before


def test_dropping_out_of_candidates_gate_cancels_the_resting_quote(rig):
    """Every path that stops quoting a market must cancel what it left behind --
    otherwise a gate rejection creates exactly the orphans of 2026-08-13."""
    km = _market()
    _tick([_candidate(km)], [km])
    assert mm._resting_quotes.get("T1")

    # Same market, but the book has narrowed back into directionally-tradeable
    # territory, so MM should stand down.
    narrow = _market(bid=0.49, ask=0.51)
    _tick([_candidate(narrow)], [narrow])
    assert rig["cancelled"], "resting legs must be cancelled when MM stands down"
    assert not mm._resting_quotes.get("T1")


# ── #1 visibility ─────────────────────────────────────────────────────────────

def test_every_candidate_produces_a_decision_row(rig):
    """Including the rejected ones -- that is the whole point. Before this a tick
    that rejected 59 of 60 candidates recorded nothing."""
    good = _market(ticker="GOOD")
    dead = _market(ticker="DEAD", volume=0.0, volume_24h=0.0)   # never traded
    wrong = _market(ticker="WRONG")                        # consensus off-book
    cands = [_candidate(good), _candidate(dead),
             _candidate(wrong, consensus=0.80)]
    _tick(cands, [good, dead, wrong])

    by_ticker = {d["kalshi_ticker"]: d for d in rig["decisions"]}
    assert set(by_ticker) == {"GOOD", "DEAD", "WRONG"}
    assert by_ticker["GOOD"]["action"] == "placed"
    assert by_ticker["DEAD"]["reason"] == "insufficient_volume"
    assert by_ticker["WRONG"]["reason"] == "consensus_outside_spread"


def test_decision_rows_carry_the_inputs_needed_to_audit_the_call(rig):
    km = _market()
    _tick([_candidate(km)], [km])
    d = rig["decisions"][0]
    for field in ("kalshi_bid", "kalshi_ask", "kalshi_spread", "kalshi_volume",
                  "consensus_prob", "bookmaker_count", "consensus_std",
                  "yes_quote", "no_quote", "net_per_pair", "contracts"):
        assert d[field] is not None, f"{field} missing from the decision row"


def test_kept_quotes_are_recorded_as_kept_not_placed(rig):
    km = _market()
    cands = [_candidate(km)]
    _tick(cands, [km])
    rig["decisions"].clear()
    _tick(cands, [km])
    assert rig["decisions"][0]["action"] == "kept"


def test_tick_summary_is_logged_at_info(rig, caplog):
    """logger.debug is off in production. The summary has to survive at INFO or
    the visibility gap is unchanged."""
    km = _market()
    with caplog.at_level("INFO"):
        _tick([_candidate(km)], [km])
    assert "MM tick:" in caplog.text
    assert "Outcomes:" in caplog.text


def test_decision_log_failure_does_not_break_trading(rig, monkeypatch):
    """Visibility must never be able to take down the trader."""
    def _boom(tick_id, entries):
        raise RuntimeError("disk full")
    monkeypatch.setattr(mm.db, "log_mm_decisions", _boom)
    km = _market()
    _tick([_candidate(km)], [km])           # must not raise
    assert len(rig["placed"]) == 2


# ── #4 concurrency ────────────────────────────────────────────────────────────

def test_more_than_one_candidate_can_be_quoted_per_tick(rig):
    """The one-candidate ceiling. With a clip capped at the FULL MM budget the
    first candidate exhausted it and every other was rejected by the aggregate
    cap -- observed live as ~60 candidates per scan and 1 quote."""
    markets = [_market(ticker=f"T{i}") for i in range(6)]
    _tick([_candidate(m) for m in markets], markets)
    quoted = {d["kalshi_ticker"] for d in rig["decisions"]
              if d["action"] in ("placed", "kept")}
    assert len(quoted) >= 2, f"only {len(quoted)} candidate(s) quoted"
    assert len(quoted) <= config.MM_MAX_CONCURRENT_QUOTES + 1, (
        "the aggregate cap must still bind")


def test_aggregate_exposure_cap_still_binds(rig):
    """Loosening the per-clip cap must not loosen the total."""
    markets = [_market(ticker=f"T{i}") for i in range(40)]
    _tick([_candidate(m) for m in markets], markets)
    resting = sum(leg["price"] * leg["count"]
                  for legs in mm._resting_quotes.values()
                  for leg in legs.values() if leg)
    assert resting <= config.MM_MAX_EXPOSURE_PCT * _BM.bankroll + 1e-6
