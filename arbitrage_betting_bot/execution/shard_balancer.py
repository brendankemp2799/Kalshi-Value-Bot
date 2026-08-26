"""Keep spendable cash on the exchange shards where we actually trade.

Kalshi split its matching engine into shards on 2026-08-24 12:00 ET. Each shard
holds a SEPARATE cash balance, and Kalshi's collateral check runs inside the
matching engine, so "programmatic traders must preallocate collateral on a given
exchange shard before order placement". New tennis and baseball events are
created on shard 3; everything else is still shard 0.

Sizing is deliberately NOT shard-aware and must stay that way. Kelly sizes
against total wealth, and the bankroll is the bankroll wherever the cash happens
to sit -- sizing baseball against only the shard-3 balance would under-bet by
roughly the ratio of the split. What is shard-aware is COLLATERAL: the money has
to be standing in the right place when the order goes out. That is this module's
entire job.

Measured on 2026-08-26, the first scan after shard routing was fixed: 30 orders
were attempted on shard 3, 2 filled for $9.26, and the remaining 28 died with
`400 insufficient_balance` after the $10 float ran dry. Resting orders hold
collateral too, so the requirement tracks peak concurrent orders, not just
filled positions.

Kalshi does have a managed auto-rebalancer, but it is institutional-only -- this
account gets `403 target_balance_allocation_is_not_enabled_for_this_user`. Hence
doing it ourselves.

SAFETY. The transfer endpoint is asynchronous and, per Kalshi's own docs,
NOT atomic: "Cross-exchange-index subaccount transfers run in up to three
non-atomic steps. If a later step fails, completed steps are not undone." It is
therefore also not idempotent, so a failed transfer is NEVER retried
automatically -- a blind retry can move the money twice. Every transfer is
capped, rate-limited, recorded before it is attempted, and reconciled after.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
_BALANCE_URL = f"{_BASE_URL}/portfolio/balance"
_ORDERS_URL = f"{_BASE_URL}/portfolio/orders"
_STATUS_URL = f"{_BASE_URL}/exchange/status"
_XFER_URL = f"{_BASE_URL}/portfolio/intra_exchange_instance_transfer"      # POST
_XFER_HIST_URL = f"{_BASE_URL}/portfolio/intra_exchange_instance_transfers"  # GET

# Kalshi's transfer amount is in CENTICENTS -- one hundredth of a cent. This is a
# third money unit, distinct from the integer cents used by the subaccount
# transfer endpoint and from the fixed-point dollar strings used by orders.
# Getting it wrong moves 100x or 1/100th of the intended amount.
CENTICENTS_PER_DOLLAR = 10_000

# `event_contract` is the prediction-market instance (vs `margined`). It is NOT
# the shard number -- the shard goes in source_exchange_shard/destination_.
_INSTANCE = "event_contract"


@dataclass(frozen=True)
class Transfer:
    """One planned movement of cash. Dollars, not centicents."""
    source: int
    destination: int
    dollars: float


def parse_targets(spec: str) -> dict[int, float]:
    """"0:50,3:50" -> {0: 0.5, 3: 0.5}. Raises if the percentages do not sum to 100.

    Refusing to normalise a bad spec is deliberate: silently rescaling a typo'd
    allocation would move real money to a split nobody chose.
    """
    out: dict[int, float] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        shard, _, pct = part.partition(":")
        out[int(shard)] = float(pct) / 100.0
    if not out:
        return {}
    total = sum(out.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(
            f"shard allocation must total 100%, got {total * 100:.4g}% from {spec!r}"
        )
    return out


def plan_transfers(
    cash: dict[int, float],
    resting: dict[int, float],
    targets: dict[int, float],
    *,
    min_transfer: float,
    max_transfer: float,
) -> list[Transfer]:
    """Work out the moves that bring `cash` toward `targets`. Pure function.

    `resting` is the value tied up in live resting orders per shard. That money
    is spoken for and cannot be moved, so a shard's surplus is capped by what is
    actually free -- moving it would cancel-or-fail somebody's working order.

    Deficits are filled largest-first from the largest surplus, so when funds are
    short the shard furthest from target is served first rather than whichever
    happens to be enumerated first.
    """
    if not targets:
        return []

    total = sum(cash.values())
    if total <= 0:
        return []

    surplus: dict[int, float] = {}
    deficit: dict[int, float] = {}
    for shard, pct in targets.items():
        want = total * pct
        have = cash.get(shard, 0.0)
        gap = want - have
        if gap > 0:
            deficit[shard] = gap
        elif gap < 0:
            free = max(0.0, have - resting.get(shard, 0.0))
            movable = min(-gap, free)
            if movable > 0:
                surplus[shard] = movable

    plans: list[Transfer] = []
    for dst in sorted(deficit, key=lambda s: -deficit[s]):
        need = deficit[dst]
        for src in sorted(surplus, key=lambda s: -surplus[s]):
            if need < min_transfer:
                break
            avail = surplus[src]
            if avail <= 0:
                continue
            amount = min(need, avail, max_transfer)
            if amount < min_transfer:
                continue
            amount = round(amount, 2)
            plans.append(Transfer(source=src, destination=dst, dollars=amount))
            surplus[src] -= amount
            need -= amount
    return plans


# ── I/O ──────────────────────────────────────────────────────────────────────

def _get(url: str) -> dict:
    from data.kalshi_auth import auth_headers, session
    resp = session().get(url, headers=auth_headers("GET", url.split("?")[0]), timeout=20)
    resp.raise_for_status()
    return resp.json()


def shard_cash() -> dict[int, float]:
    """Spendable cash per shard, from Kalshi."""
    data = _get(_BALANCE_URL)
    return {int(e["exchange_index"]): float(e["balance"])
            for e in data.get("balance_breakdown", [])}


def resting_value_by_shard() -> dict[int, float]:
    """Dollars committed to live resting orders, per shard.

    Kalshi exposes no per-shard resting-order total, so it is summed from the
    open-order list. Anything not clearly resting is ignored rather than guessed
    at; the consequence of under-counting is a refused transfer, not a lost one.
    """
    out: dict[int, float] = {}
    try:
        data = _get(f"{_ORDERS_URL}?limit=200")
    except Exception as e:
        logger.warning("Could not read resting orders for rebalance: %s", e)
        return out
    for o in data.get("orders", []):
        if o.get("status") not in ("resting", "open", "pending"):
            continue
        shard = o.get("exchange_index")
        if shard is None:
            continue
        try:
            remaining = float(o.get("remaining_count_fp")
                              or o.get("remaining_count") or 0)
            price = float(o.get("yes_price_fp") or o.get("price") or 0)
        except (TypeError, ValueError):
            continue
        out[int(shard)] = out.get(int(shard), 0.0) + remaining * price
    return out


def transfers_active(shards: list[int]) -> bool:
    """Kalshi can block transfers per shard; check before moving anything."""
    try:
        data = _get(_STATUS_URL)
    except Exception as e:
        logger.warning("Could not read exchange status: %s", e)
        return False
    gates = {int(e["exchange_index"]): e for e in data.get("exchange_index_statuses", [])}
    for s in shards:
        if not gates.get(s, {}).get("intra_exchange_transfers_active"):
            logger.warning("Transfers are not active on shard %s — skipping rebalance", s)
            return False
    return True


def submit_transfer(t: Transfer) -> str | None:
    """POST one transfer. Returns transfer_id, or None on failure. NEVER retried.

    A retry here can move the money twice: the endpoint is documented as
    non-atomic across up to three steps, so a failure part-way leaves completed
    steps in place. The caller must treat None as "unknown, stop and look",
    not as "didn't happen".
    """
    from data.kalshi_auth import auth_headers, session
    body = {
        "source": _INSTANCE,
        "destination": _INSTANCE,
        "source_exchange_shard": int(t.source),
        "destination_exchange_shard": int(t.destination),
        "amount": int(round(t.dollars * CENTICENTS_PER_DOLLAR)),
    }
    try:
        resp = session().post(_XFER_URL, json=body,
                              headers=auth_headers("POST", _XFER_URL), timeout=25)
    except Exception as e:
        logger.error(
            "TRANSFER FAILED TO SEND shard %s->%s $%.2f: %s. NOT retrying — this "
            "endpoint is non-atomic and a retry could move it twice. Verify by hand.",
            t.source, t.destination, t.dollars, e,
        )
        return None
    if resp.status_code not in (200, 201):
        logger.error(
            "TRANSFER REJECTED shard %s->%s $%.2f: HTTP %s %s. NOT retrying.",
            t.source, t.destination, t.dollars, resp.status_code, resp.text[:200],
        )
        return None
    return (resp.json() or {}).get("transfer_id")


def wait_for_settlement(transfer_id: str, timeout: float = 60.0,
                        interval: float = 3.0) -> bool:
    """Poll until the transfer reports complete. Acceptance is not settlement.

    Returns False on timeout, which must be treated as "still in flight", never
    as "failed" — issuing another transfer after this returns False is exactly
    the double-move this module exists to avoid.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = _get(f"{_XFER_HIST_URL}?limit=20")
            for row in data.get("transfers", []):
                if row.get("transfer_id") == transfer_id:
                    if row.get("status") == "complete":
                        return True
                    break
        except Exception as e:
            logger.debug("Transfer poll error (will retry the READ): %s", e)
        time.sleep(interval)
    logger.warning(
        "Transfer %s did not report complete within %.0fs — treating as IN FLIGHT, "
        "not as failed. No further transfer will be issued this cycle.",
        transfer_id, timeout,
    )
    return False


def rebalance_once(dry_run: bool | None = None) -> list[tuple[Transfer, str | None]]:
    """One rebalancing pass. Returns [(transfer, transfer_id_or_None)].

    Safe to call every scan: when allocations are already on target it plans
    nothing and makes no write calls.
    """
    if dry_run is None:
        dry_run = not getattr(config, "SHARD_REBALANCE_ENABLED", False)

    try:
        targets = parse_targets(getattr(config, "SHARD_TARGET_ALLOCATION", ""))
    except ValueError as e:
        logger.error("Bad SHARD_TARGET_ALLOCATION, not rebalancing: %s", e)
        return []
    if not targets:
        return []

    cash = shard_cash()
    resting = resting_value_by_shard()
    plans = plan_transfers(
        cash, resting, targets,
        min_transfer=float(getattr(config, "SHARD_MIN_TRANSFER_DOLLARS", 5.0)),
        max_transfer=float(getattr(config, "SHARD_MAX_TRANSFER_DOLLARS", 100.0)),
    )
    if not plans:
        return []

    logger.info(
        "Shard rebalance: cash=%s resting=%s -> %d transfer(s)%s",
        {k: round(v, 2) for k, v in sorted(cash.items())},
        {k: round(v, 2) for k, v in sorted(resting.items())},
        len(plans), " [DRY RUN]" if dry_run else "",
    )

    results: list[tuple[Transfer, str | None]] = []
    if dry_run:
        for t in plans:
            logger.info("  would move $%.2f shard %s -> %s", t.dollars, t.source, t.destination)
            results.append((t, None))
        return results

    if not transfers_active(sorted({s for t in plans for s in (t.source, t.destination)})):
        return []

    for t in plans:
        tid = submit_transfer(t)
        results.append((t, tid))
        if tid is None:
            logger.error("Halting rebalance after a failed transfer — not continuing.")
            break
        logger.info("Moved $%.2f shard %s -> %s (transfer_id=%s)",
                    t.dollars, t.source, t.destination, tid)
        if not wait_for_settlement(tid):
            logger.warning("Halting rebalance — %s still in flight.", tid)
            break
    return results
