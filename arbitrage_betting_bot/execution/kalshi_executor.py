"""
Places orders on Kalshi via the REST API v2 orders endpoints.

Authentication: RSA-signed requests (KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH).
See data/kalshi_auth.py for signing details.

Endpoints (as of July 2026):
  POST   /portfolio/events/orders            — create order (new v2 path)
  GET    /portfolio/orders/{id}              — check status (old path still works)
  DELETE /portfolio/events/orders/{id}       — cancel order (new v2 path)

Order model:
  - side:           "bid" (buy YES) or "ask" (sell YES = buy NO)
  - price:          fixed-point dollar string, e.g. "0.4000" (YES price)
  - count:          fixed-point contract count string, e.g. "25.00"
  - time_in_force:  "good_till_canceled" (American spelling, one L) — resting limit order

Side mapping from our internal yes/no convention:
  yes → bid,  price = yes_ask (market_price as passed in)
  no  → ask,  price = 1 - no_ask  (convert no price to the YES price we're selling at)

Fee model:
  Kalshi charges a real fee whenever an order crosses the spread at placement (an
  economic "taker" action), regardless of GTC vs IOC order type — step 2 below ("GTC
  at ask") is designed to cross immediately and often does incur this fee. The actual
  fee charged is read directly from the order's maker_fees_dollars/taker_fees_dollars
  fields (GET /portfolio/orders/{id}) after every fill rather than inferred/assumed,
  since guessing this got it wrong before (see _actual_fee_dollars()).

  Maker fees are NOT reliably zero, though they are close to it in practice. Measured
  across 139 filled orders on 2026-08-15: 125 carried a taker fee, 13 carried no fee
  of any kind, and exactly ONE carried a maker fee (KXEPLGAME-26AUG23BRIAVL-AVL, 1
  contract @ $0.31, $0.0038). That single data point implies a maker rate of 0.0178
  — essentially the 0.0175 (25% of the taker rate) assumed in config.py — so the
  formula appears correct and is simply seldom levied on the series we trade. The one
  charged fill was EPL and all 13 free ones were MLB/MLS, which HINTS at a
  series-dependent schedule, but n=1 is not evidence of that; do not code against it.
  Anything that must assume a maker fee should assume it IS charged (conservative).

Execution strategy:
  Step 1: GTC at mid price, adaptive timeout (2–10 min based on game time).
          Skipped when edge >= config.LARGE_EDGE_SKIP_PASSIVE (large edges are
          taken immediately rather than risked on a passive fill). While resting,
          periodically re-checks the live Kalshi price and cancels early if it has
          moved against us by config.PASSIVE_ADVERSE_MOVE_CANCEL or more.
  Step 2: GTC at ask price, short timeout (30 s).
"""
from __future__ import annotations

import json as _json
import logging
import math
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_BASE_URL     = "https://api.elections.kalshi.com/trade-api/v2"
_ORDERS_URL   = f"{_BASE_URL}/portfolio/events/orders"   # POST + DELETE
_STATUS_URL   = f"{_BASE_URL}/portfolio/orders"          # GET (old path still works)

# Kalshi shards the exchange across separate matching engines, each with its own
# segregated cash balance. -1 tells Kalshi "work out the shard from the ticker"
# rather than defaulting to shard 0. See _place_raw_order and _cancel_order for
# the measurements behind this; note the two routes need DIFFERENT arguments.
_AUTO_ROUTE = -1


# Backoff schedule for reading an order back immediately after placing it. See
# _get_order_status(retries=...) — deliberately short; the whole point is to
# outlast a propagation lag of well under a second, not to survive an outage.
_STATUS_RETRY_BACKOFF = (0.25, 0.75, 1.5)


def _get_order_status(order_id: str, retries: int = 0) -> dict | None:
    """
    Fetch current order status. Returns the order dict, or None on error.

    retries: extra attempts (with _STATUS_RETRY_BACKOFF sleeps) before giving up.
    Defaults to 0 so the polling loops, which call this every few seconds anyway
    and would only slow themselves down, keep their existing behaviour.

    Why retries exist at all: Kalshi does not make an order queryable at this
    endpoint the instant the POST that created it returns. Reading the fee back
    immediately after an at-placement fill therefore races that propagation and
    404s — measured at 26 occurrences in 24h on 2026-08-15, roughly 8% of taker
    fills. The failure was silent and one-directional: _fee_breakdown() returned
    (0, 0), so the position recorded entry_fee_paid=$0.00 for a fill that really
    did pay a taker fee, understating costs and overstating P&L. Confirmed to be
    a race and not a bad identifier: all 12 most recent stored order_ids were
    queryable when retried later, including the exact id that had just 404'd.

    Only ever retries a GET, never the order POST — an automatic retry on a
    placement could double-place a real trade (see data/kalshi_auth.py's session()
    for the same reasoning applied to the HTTPAdapter).
    """
    from data.kalshi_auth import auth_headers, session

    url = f"{_STATUS_URL}/{order_id}"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, headers=auth_headers("GET", url), timeout=10)
            resp.raise_for_status()
            return resp.json().get("order", {})
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(_STATUS_RETRY_BACKOFF[min(attempt, len(_STATUS_RETRY_BACKOFF) - 1)])

    logger.warning(
        "Could not fetch order status for %s after %d attempt(s): %s",
        order_id, retries + 1, last_err,
    )
    return None


def _actual_fee_dollars(order_id: str) -> float:
    """
    The real fee Kalshi charged for an order (maker + taker combined), read directly
    from the order's own record. Do not infer/assume this — an earlier version of this
    code hardcoded "always maker, 0% fee" and was wrong for any fill that crossed the
    spread at placement.
    """
    maker_fee, taker_fee = _fee_breakdown(order_id)
    return round(maker_fee + taker_fee, 6)


def _fee_breakdown(order_id: str) -> tuple[float, float]:
    """(maker_fee, taker_fee) in dollars, straight from Kalshi's own order record.

    Retries, because this is the one caller that reads an order back immediately
    after it was created and so races Kalshi making it queryable. Failing here
    silently records a $0.00 fee on a fill that really paid one — an error that
    only ever flatters P&L, which is the worst direction for it to be wrong in.
    """
    status = _get_order_status(order_id, retries=len(_STATUS_RETRY_BACKOFF))
    if not status:
        logger.error(
            "FEE UNKNOWN for order %s — Kalshi never returned its record, so this "
            "fill is being recorded with a $0.00 fee it may not have had. Real cost "
            "is understated by whatever was actually charged.", order_id,
        )
        return 0.0, 0.0
    return (float(status.get("maker_fees_dollars", 0) or 0),
            float(status.get("taker_fees_dollars", 0) or 0))


def _classify_fill(order_id: str, crossed_at_placement: bool) -> tuple[str, float]:
    """
    Return ("maker" | "taker", total_fee) for a filled order.

    This used to be `"taker" if fee_paid > 0 else "maker"`, which is CIRCULAR:
    it defines a maker fill as one that paid nothing, so "every maker fill was
    free" became true by construction and the label could never disagree with
    the fee. That silently mislabelled any genuine maker fill Kalshi did charge
    as a "taker" fill, and made our own database useless for answering "does
    Kalshi charge makers?" — which it is not, empirically: across 139 filled
    orders sampled 2026-08-15, Kalshi returned a non-zero maker_fees_dollars on
    exactly one (KXEPLGAME-26AUG23BRIAVL-AVL, 1 contract @ $0.31, $0.0038,
    implying a rate of 0.0178 against our assumed 0.0175 — so the FORMULA is
    right, it is simply rarely levied on the series we trade).

    Kalshi's own fee fields are authoritative when either is non-zero. When both
    are zero the fee cannot disambiguate, so fall back to what the order actually
    did: one that filled the instant it was placed necessarily crossed the book
    (taker); one that sat and was later filled by a counterparty coming to us is
    a maker fill.
    """
    maker_fee, taker_fee = _fee_breakdown(order_id)
    total = round(maker_fee + taker_fee, 6)
    if taker_fee > 0:
        return "taker", total
    if maker_fee > 0:
        return "maker", total
    return ("taker" if crossed_at_placement else "maker"), total


def _cancel_order(order_id: str, ticker: str) -> bool:
    """Cancel a resting GTC order. Returns True if cancelled successfully.

    `ticker` is REQUIRED, and is not optional the way it looks. Cancel routes
    differently from create: create carries the ticker in its JSON body, so
    exchange_index=-1 is enough to auto-route it, but cancel addresses the order
    by id in the path and Kalshi cannot infer the shard from that alone.
    Measured against the live API on 2026-08-26:

        no params at all         -> 404 not_found        (what this used to send)
        exchange_index=-1 only   -> 400 market_ticker_is_required_when_exchange_index=-1
        exchange_index=-1+ticker -> 200
        exchange_index=<n> only  -> 200

    The first line is the dangerous one. Every caller here treats a failed
    cancel as "already gone", so before this fix a resting quote on a non-zero
    shard would have been left LIVE and untracked -- the same class of orphan
    that _drop_prior and the MM sweep exist to prevent. We pass ticker rather
    than a hard-coded shard so this keeps working after the next re-shard.
    """
    try:
        from data.kalshi_auth import auth_headers, session
        url = f"{_ORDERS_URL}/{order_id}"
        headers = auth_headers("DELETE", url)
        resp = session().delete(
            url,
            params={"exchange_index": _AUTO_ROUTE, "market_ticker": ticker},
            headers=headers,
            timeout=10,
        )
        if resp.ok:
            return True

        # A rejected cancel is ambiguous, and guessing either way is bad: assume
        # "gone" and we may leak a live order; assume "live" and we cry wolf on
        # every already-terminal order, which is how real leak warnings get
        # ignored. So ask. Observed in production 2026-08-26: three GTC orders
        # that had rested their full 900s returned 404 not_found on cancel and
        # were ALREADY `canceled` with 0 filled -- Kalshi had terminated them
        # first. Nothing leaked, but the blind warning said otherwise.
        return _confirm_gone(order_id, ticker, resp.status_code, resp.text[:160])
    except Exception as e:
        logger.warning("Could not cancel order %s on %s: %s", order_id, ticker, e)
        return False


_TERMINAL_ORDER_STATES = ("canceled", "cancelled", "executed", "closed", "expired")


def _confirm_gone(order_id: str, ticker: str, code: int, body: str) -> bool:
    """After a rejected cancel, read the order back and say what really happened.

    Returns True if the order is confirmed terminal (nothing resting), False if
    it is still live or could not be established -- the caller should treat False
    as "there may be real exposure out there".
    """
    status = _get_order_status(order_id, retries=1)
    if status is None:
        logger.error(
            "Cancel REJECTED for %s on %s (HTTP %s %s) AND its status could not be "
            "read back. Treating as possibly LIVE — reconcile by hand.",
            order_id, ticker, code, body,
        )
        return False

    state = str(status.get("status", "")).lower()
    remaining = float(status.get("remaining_count_fp")
                      or status.get("remaining_count") or 0)
    if state in _TERMINAL_ORDER_STATES and remaining <= 0:
        logger.info(
            "Cancel for %s on %s returned HTTP %s, but the order is already %s "
            "with nothing resting — no exposure.",
            order_id, ticker, code, state,
        )
        return True

    logger.critical(
        "ORDER STILL LIVE after a rejected cancel — %s on %s is %r with %g "
        "contract(s) remaining (cancel returned HTTP %s %s). REAL UNTRACKED "
        "EXPOSURE; it can still fill.",
        order_id, ticker, state, remaining, code, body,
    )
    return False


# Serialises order POSTs across every thread. See
# config.KALSHI_ORDER_MIN_SPACING_SECONDS for the incident this exists to prevent.
_ORDER_POST_LOCK = threading.Lock()
_last_order_post_at: float = 0.0


def _throttle_order_post() -> None:
    """Block until this thread may POST an order.

    The lock is deliberately held across the sleep: that is what makes concurrent
    callers come out one at a time, spaced, rather than all waking together and
    re-creating the burst.
    """
    global _last_order_post_at
    spacing = getattr(config, "KALSHI_ORDER_MIN_SPACING_SECONDS", 0.0)
    if spacing <= 0:
        return
    with _ORDER_POST_LOCK:
        now = time.monotonic()
        wait = _last_order_post_at + spacing - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_order_post_at = now


def _place_raw_order(
    ticker: str,
    api_side: str,
    yes_price: float,
    count: int,
    time_in_force: str,
    client_order_id: str,
) -> dict:
    """
    Post a single order to Kalshi. Returns the API response dict.
    """
    from data.kalshi_auth import auth_headers, session
    payload = {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": api_side,
        "price": f"{yes_price:.4f}",
        "count": f"{count:.2f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
        # Route by ticker. Kalshi split the exchange into shards on 2026-08-24
        # 12:00 ET; new tennis/baseball events are created on shard 3 while
        # everything else stays on shard 0. A write that omits this field is
        # routed to shard 0, which has never heard of a shard-3 ticker and
        # rejects it with 404 market_not_found -- that is what silently killed
        # every MLB prop and totals order from 2026-08-24T23:22 onward (540
        # rejections in two days, verified A/B: identical payload 404s without
        # this field and returns 201 with it).
        #
        # -1 means "auto-route by ticker", which is correct on EVERY shard and
        # survives the next re-shard without a code change. Verified against
        # both shard 0 and shard 3. The cost is that an auto-routed write bills
        # every shard's rate-limit bucket rather than just one; at our order
        # volume (tens per 45-minute scan) that is not close to binding.
        "exchange_index": _AUTO_ROUTE,
    }
    _throttle_order_post()
    headers = auth_headers("POST", _ORDERS_URL)
    resp = session().post(_ORDERS_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _contract_count(stake_dollars: float, price: float) -> int:
    """
    Whole contracts to buy for a target dollar stake, rounded to NEAREST rather than
    down, subject to the hard risk caps.

    Why not floor(): contracts are indivisible, so at a small bankroll Kelly's output
    lives inside the rounding error. Measured on 44 reproducible live bets (2026-08-14):
    floor() under-sized 42 of them and over-sized none — median -11.6%, p10 -29.7%,
    worst -47.4%. 72% of bets were 1-3 contracts, where one contract is a 33-100% step,
    so the bias was large and entirely one-directional. Rounding to nearest makes the
    error symmetric instead of a systematic haircut.

    No cap is re-applied here. `stake_dollars` arrives already capped by the sizing
    layer against the LIVE bankroll (calculate_kelly's max_bet_dollars /
    max_pct_bankroll), which this module cannot see -- config.BANKROLL is only a static
    fallback and would be the wrong number to clamp against. Rounding up overshoots by
    at most half a contract price (<$0.50 at observed prices), which is immaterial
    against those caps.

    Never returns less than 1: a sub-contract target still has to buy one contract or
    not bet at all, and that decision belongs to the caller (see main.py's minimum-stake
    gate), not here.
    """
    if price <= 0:
        return 1
    return max(1, round(stake_dollars / price))


def _limit_timeout(commence_time: datetime | None) -> int:
    """Return the appropriate GTC limit-order timeout in seconds based on time to game."""
    if commence_time is None:
        return config.LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS
    now = datetime.now(timezone.utc)
    if commence_time.tzinfo is None:
        commence_time = commence_time.replace(tzinfo=timezone.utc)
    minutes_to_game = (commence_time - now).total_seconds() / 60.0
    if minutes_to_game <= config.NEAR_GAME_THRESHOLD_MINUTES:
        return config.LIMIT_ORDER_TIMEOUT_NEAR_GAME_SECONDS
    if minutes_to_game <= config.PRE_GAME_THRESHOLD_HOURS * 60:
        return config.LIMIT_ORDER_TIMEOUT_PRE_GAME_SECONDS
    return config.LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS


def _fetch_ticker_price(ticker: str) -> tuple[float, float] | None:
    """Best-effort live (yes_bid, yes_ask) lookup for repricing a resting order —
    a network error here just skips that reprice check, it doesn't fail the order."""
    try:
        from data.kalshi_client import KalshiClient
        return KalshiClient().fetch_ticker_price(ticker)
    except Exception as e:
        logger.debug("Reprice check: live price fetch failed for %s: %s", ticker, e)
        return None


def place_order(
    ticker: str,
    side: str,
    stake_dollars: float,
    market_price: float,
    kalshi_spread: float = 0.0,
    commence_time: datetime | None = None,
    edge: float | None = None,
    maker_only: bool = False,
) -> tuple[str, str, str, float, str, float]:
    """
    Place a Kalshi order using GTC limit orders.

    Args:
        ticker:         Kalshi market ticker
        side:           "yes" or "no" (internal convention)
        stake_dollars:  dollar amount to wager
        market_price:   ask price of the side we're buying (0.0 – 1.0)
        kalshi_spread:  bid-ask spread in dollars (used to compute mid price)
        commence_time:  game start time (UTC) — used to compute adaptive limit timeout
        edge:           the opportunity's edge over market_price, if known. When
                         >= config.LARGE_EDGE_SKIP_PASSIVE, step 1 is skipped
                         entirely — the edge is worth taking now rather than
                         risking it evaporating while a passive order rests.
                         Ignored when maker_only=True (see below).
        maker_only:     True when the opportunity's edge only clears the bar at
                         the mid price (0% fee) — see core/value_detector.py::
                         _eval_edge(). Crossing to ask would not be worth it, so
                         step 1 is never skipped regardless of `edge`, and if it
                         goes unfilled, this gives up instead of falling back to
                         step 2 — no bet is better than a fee-negative one.

    Returns:
        (order_id, execution_status, failure_reason, actual_stake, fill_type, fee_paid)
        execution_status: "submitted" | "failed"
        failure_reason:   empty string on success, human-readable error on failure
        actual_stake:     dollars actually filled; 0.0 on failure
        fill_type:        "maker" | "taker" on success (derived from fee_paid) | "" on failure
        fee_paid:         actual dollars charged by Kalshi for this fill; 0.0 on failure

    Execution strategy:
        Step 1: GTC at mid price, adaptive timeout (2–10 min based on game time).
                While resting, the live Kalshi price is periodically re-checked
                (config.PASSIVE_REPRICE_CHECK_INTERVAL_SECONDS); if it has moved
                against us by config.PASSIVE_ADVERSE_MOVE_CANCEL or more, the
                order is cancelled early instead of waiting out the full timeout.
                Skipped entirely when edge >= config.LARGE_EDGE_SKIP_PASSIVE
                (never skipped when maker_only=True).
        Step 2: GTC at ask price, short timeout (30 s) — this step crosses the book
                immediately by design and often incurs a real taker fee. Skipped
                entirely when maker_only=True.
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place order")
        return "", "failed", "KALSHI_API_KEY not configured", 0.0, "", 0.0

    price = max(0.01, min(0.99, market_price))
    count = _contract_count(stake_dollars, price)

    if side == "yes":
        api_side = "bid"
        yes_price_ask = price
        # Round to nearest cent — Kalshi prices are on a 1¢ grid (step=0.01)
        yes_price_mid = round(max(0.01, price - kalshi_spread / 2.0), 2)
    else:
        api_side = "ask"
        yes_price_ask = 1.0 - price
        yes_price_mid = round(min(0.99, yes_price_ask + kalshi_spread / 2.0), 2)

    # ── Step 1: GTC at mid price ──────────────────────────────────────────────
    skip_passive = (not maker_only) and edge is not None and edge >= config.LARGE_EDGE_SKIP_PASSIVE
    if skip_passive:
        logger.info(
            "Edge %.1f%% >= large-edge threshold %.1f%% — skipping passive mid step for %s, taking ask now",
            edge * 100, config.LARGE_EDGE_SKIP_PASSIVE * 100, ticker,
        )

    timeout = _limit_timeout(commence_time)
    client_order_id = str(uuid.uuid4())
    if not skip_passive:
        try:
            data = _place_raw_order(ticker, api_side, yes_price_mid, count, "good_till_canceled", client_order_id)
            order_id = data.get("order_id", client_order_id)
            filled = float(data.get("fill_count", 0) or 0)

            if filled >= count:
                actual_stake = round(filled * price, 2)
                # Filled the instant it was placed: the mid must have crossed
                # the live book, so this is economically a TAKER fill even though
                # it was priced at the mid.
                fill_type, fee_paid = _classify_fill(order_id, crossed_at_placement=True)
                logger.info(
                    "Kalshi GTC mid fill (immediate): %s %s %g contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f",
                    api_side.upper(), ticker, filled, yes_price_mid, actual_stake, fee_paid,
                )
                return order_id, "submitted", "", actual_stake, fill_type, fee_paid

            # Poll for fill up to adaptive timeout, periodically re-checking the live
            # Kalshi price so an adverse move doesn't leave us waiting out the full
            # timeout on a price the market has already moved past.
            deadline = time.time() + timeout
            last_reprice_check = time.time()
            early_cancel_reason = ""
            while time.time() < deadline and filled < count:
                time.sleep(5)
                status = _get_order_status(order_id)
                if status:
                    filled = float(status.get("fill_count_fp", 0) or 0)
                    if filled >= count:
                        break

                now = time.time()
                if now - last_reprice_check >= config.PASSIVE_REPRICE_CHECK_INTERVAL_SECONDS:
                    last_reprice_check = now
                    live = _fetch_ticker_price(ticker)
                    if live is not None:
                        live_yes_bid, live_yes_ask = live
                        live_price = live_yes_ask if side == "yes" else (1.0 - live_yes_bid)
                        if live_price - price >= config.PASSIVE_ADVERSE_MOVE_CANCEL:
                            early_cancel_reason = (
                                f"live price moved to {live_price:.2f} (resting at "
                                f"{price:.2f}, mid order at {yes_price_mid:.2f})"
                            )
                            break

            if filled > 0:
                actual_stake = round(filled * price, 2)
                # Rested at the mid and a counterparty came to us: a real maker fill.
                fill_type, fee_paid = _classify_fill(order_id, crossed_at_placement=False)
                logger.info(
                    "Kalshi GTC mid fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f (order_id=%s)",
                    api_side.upper(), ticker, filled, count, yes_price_mid, actual_stake, fee_paid, order_id,
                )
                _cancel_order(order_id, ticker)
                return order_id, "submitted", "", actual_stake, fill_type, fee_paid

            _cancel_order(order_id, ticker)
            if maker_only:
                reason = (
                    f"GTC mid cancelled early — {early_cancel_reason}" if early_cancel_reason
                    else f"GTC mid unfilled after {timeout}s"
                )
                logger.info(
                    "Kalshi GTC mid unfilled for %s — maker_only, giving up (no ask fallback): %s",
                    ticker, reason,
                )
                return order_id, "failed", reason, 0.0, "", 0.0
            if early_cancel_reason:
                logger.info(
                    "Kalshi GTC mid cancelled early for %s — %s — trying GTC at ask",
                    ticker, early_cancel_reason,
                )
            else:
                logger.info(
                    "Kalshi GTC mid unfilled after %ds — trying GTC at ask for %s",
                    timeout, ticker,
                )
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            body = e.response.text if e.response is not None else ""
            if maker_only:
                logger.warning("Kalshi GTC mid failed [%s] for %s — maker_only, giving up", code, ticker)
                return client_order_id, "failed", f"GTC mid HTTP {code}", 0.0, "", 0.0
            logger.warning("Kalshi GTC mid failed [%s] for %s — trying ask step", code, ticker)
            logger.debug("GTC mid error body: %s", body[:300])
        except requests.RequestException as e:
            if maker_only:
                logger.warning("GTC mid network error for %s — maker_only, giving up: %s", ticker, e)
                return client_order_id, "failed", f"GTC mid network error: {str(e)[:200]}", 0.0, "", 0.0
            logger.warning("GTC mid network error for %s — trying ask step: %s", ticker, e)

    # ── Step 2: GTC at ask price ──────────────────────────────────────────────
    ask_timeout = config.LIMIT_ORDER_ASK_TIMEOUT_SECONDS
    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price_ask, count, "good_till_canceled", client_order_id)
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)

        if filled >= count:
            actual_stake = round(filled * price, 2)
            # Step 2 is designed to cross the book immediately: a TAKER fill.
            fill_type, fee_paid = _classify_fill(order_id, crossed_at_placement=True)
            logger.info(
                "Kalshi GTC ask fill (immediate): %s %s %g contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f",
                api_side.upper(), ticker, filled, yes_price_ask, actual_stake, fee_paid,
            )
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        deadline = time.time() + ask_timeout
        while time.time() < deadline and filled < count:
            time.sleep(5)
            status = _get_order_status(order_id)
            if status:
                filled = float(status.get("fill_count_fp", 0) or 0)
                if filled >= count:
                    break

        if filled > 0:
            actual_stake = round(filled * price, 2)
            # Priced at the ask but did NOT fill on placement, so the ask had
            # already moved up and this order rested below it before filling.
            fill_type, fee_paid = _classify_fill(order_id, crossed_at_placement=False)
            logger.info(
                "Kalshi GTC ask fill: %s %s %g/%d contracts @ %.4f  actual_stake=$%.2f  fee=$%.4f (order_id=%s)",
                api_side.upper(), ticker, filled, count, yes_price_ask, actual_stake, fee_paid, order_id,
            )
            _cancel_order(order_id, ticker)
            return order_id, "submitted", "", actual_stake, fill_type, fee_paid

        _cancel_order(order_id, ticker)
        reason = f"GTC ask unfilled after {ask_timeout}s — no resting liquidity at ask"
        logger.warning("Kalshi GTC ask zero fill for %s @ %.4f", ticker, yes_price_ask)
        return order_id, "failed", reason, 0.0, "", 0.0

    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        reason = f"HTTP {code}"
        try:
            err = _json.loads(body)
            err_obj = err.get("error", err)
            msg = err_obj.get("message", "")
            if msg:
                reason = f"HTTP {code}: {msg}"
        except Exception:
            if body:
                reason = f"HTTP {code}: {body[:200]}"
        logger.error("Kalshi GTC ask failed [%s]: %s", code, body[:300])
        return client_order_id, "failed", reason, 0.0, "", 0.0
    except requests.RequestException as e:
        reason = f"Network error: {str(e)[:200]}"
        logger.error("Kalshi GTC ask network error: %s", e)
        return client_order_id, "failed", reason, 0.0, "", 0.0


def get_order_status(order_id: str) -> dict | None:
    """Public wrapper around _get_order_status() — used by market_maker.py to check
    whether a resting quote has filled since it was placed."""
    return _get_order_status(order_id)


def list_resting_orders() -> list[dict]:
    """
    Every currently-resting order on the account, paginated. Used by
    market_maker.py to rebuild its in-memory quote-tracking state after a process
    restart — Kalshi's own order book is the authoritative source of what's
    actually still resting, closing the gap that used to require manually
    cancelling resting orders before restarting the process (see the 2026-08-12
    incident: a real 5-contract fill on a resting order went unrecorded for 3.5+
    hours after a restart, because the old in-memory-only tracking had no idea
    that order still existed).
    """
    try:
        from data.kalshi_auth import auth_headers, session
        orders: list[dict] = []
        cursor = None
        while True:
            params: dict = {"status": "resting", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            headers = auth_headers("GET", _STATUS_URL)
            resp = session().get(_STATUS_URL, headers=headers, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            orders.extend(data.get("orders", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return orders
    except Exception as e:
        logger.warning("Could not list resting orders: %s", e)
        return []


def order_fee_paid(order_id: str) -> float:
    """Public wrapper around _actual_fee_dollars()."""
    return _actual_fee_dollars(order_id)


def cancel_quote(order_id: str, ticker: str) -> bool:
    """Public wrapper around _cancel_order() — cancel a resting market-making quote.

    `ticker` is required for shard routing; see _cancel_order for why an order id
    alone no longer addresses an order unambiguously.
    """
    return _cancel_order(order_id, ticker)


def place_resting_quote(
    ticker: str,
    side: str,
    price: float,
    count: int,
) -> tuple[str, float, float]:
    """
    Rest a single plain GTC limit order for market making — deliberately NOT
    place_order()'s two-step "escalate to cross the book if unfilled" behavior.
    A market-making quote is meant to sit passively at a price we chose (inside the
    spread, away from touch); if it doesn't fill, that's fine — the caller's requote
    loop (execution/market_maker.py) will cancel and reprice it next tick, not chase
    a fill by crossing the spread (that would defeat the purpose of quoting as a
    maker instead of paying the taker fee).

    Args:
        ticker: Kalshi market ticker
        side:   "yes" or "no" (internal convention) — the side we're buying
        price:  desired price of `side` (0.0-1.0)
        count:  contracts to quote

    Returns:
        (order_id, filled_count, fee_paid) — filled_count/fee_paid are usually 0 at
        placement time (the whole point is to rest unfilled), but Kalshi may match
        immediately if this price already crosses the live book, so both are
        checked and returned rather than assumed zero.
    """
    if not config.KALSHI_API_KEY:
        logger.error("KALSHI_API_KEY not set — cannot place quote")
        return "", 0.0, 0.0

    p = max(0.01, min(0.99, price))
    if side == "yes":
        api_side = "bid"
        yes_price = p
    else:
        api_side = "ask"
        yes_price = 1.0 - p

    client_order_id = str(uuid.uuid4())
    try:
        data = _place_raw_order(ticker, api_side, yes_price, count, "good_till_canceled", client_order_id)
        order_id = data.get("order_id", client_order_id)
        filled = float(data.get("fill_count", 0) or 0)
        fee_paid = _actual_fee_dollars(order_id) if filled > 0 else 0.0
        logger.debug(
            "MM quote resting: %s %s %g contracts @ %.4f (order_id=%s, filled=%g)",
            api_side.upper(), ticker, count, yes_price, order_id, filled,
        )
        return order_id, filled, fee_paid
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text if e.response is not None else ""
        logger.warning("MM quote placement failed [%s] for %s: %s", code, ticker, body[:300])
        return "", 0.0, 0.0
    except requests.RequestException as e:
        logger.warning("MM quote placement network error for %s: %s", ticker, e)
        return "", 0.0, 0.0


