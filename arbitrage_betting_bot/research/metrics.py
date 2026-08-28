"""
Deterministic performance metrics for the autonomous research layer.

This is the "Python computes, Claude interprets" boundary: every function here is
plain arithmetic over the production database, read-only. Agents should call
summary_report() and reason over its (small, compact) output — never loop over raw
rows themselves, and never write anything back to storage/betting_bot.db.

Schema-tolerant: some columns (e.g. `strategy`) exist locally but aren't deployed to
production yet. Every column access goes through _get()/_col_exists() rather than
assuming presence, mirroring the same defensive pattern already used in
dashboard_server.py.

CLI:
    python3 research/metrics.py                    # pretty-print summary_report()
    python3 research/metrics.py --check-thresholds  # the free, zero-LLM daily check —
        compares today's snapshot to research/findings/last_snapshot.json and writes
        a TRIGGER_<date>.md file into research/findings/ iff a real threshold is
        crossed. Exits 0 either way; presence of a TRIGGER_*.md file is the signal.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from storage.db import (  # noqa: E402  (read-only use only)
    get_connection, get_qualifying_candidates_with_outcomes,
)

RESEARCH_DIR = Path(__file__).parent
FINDINGS_DIR = RESEARCH_DIR / "findings"
SNAPSHOT_PATH = FINDINGS_DIR / "last_snapshot.json"

# ── Threshold-check config (the "free daily check" in the Phase 1 plan) ────────────
MIN_NEW_SETTLED_TRADES = 8      # roughly a week's worth at current (~1.8/day) velocity
DRAWDOWN_ALERT_FRACTION = 0.15  # trigger if total_at_risk-implied drawdown exceeds this
EDGE_CALIBRATION_BUCKETS = [
    (0.015, 0.03, "1.5-3%"),
    (0.03, 0.05, "3-5%"),
    (0.05, 0.08, "5-8%"),
    (0.08, float("inf"), "8%+"),
]

# Same rung-distance-from-anchor buckets as storage/db.py::get_dk_scaled_shadow_summary,
# kept in sync deliberately so a profitability breakdown and a calibration breakdown can
# be read side by side without re-deriving whether "the 1.5-3 bucket" means the same thing
# in both places.
DK_DISTANCE_BUCKETS = [
    (0.0, 0.5, "0-0.5"),
    (0.5, 1.5, "0.5-1.5"),
    (1.5, 3.0, "1.5-3"),
    (3.0, float("inf"), "3+"),
]


def _row_get(row, col, default=None):
    """sqlite3.Row-safe column access — returns `default` if the column doesn't
    exist on this deployment (e.g. `strategy`, not yet pushed to production)."""
    try:
        return row[col] if col in row.keys() else default
    except (IndexError, KeyError):
        return default


def load_positions(
    is_paper: bool = False,
    sport: str | None = None,
    bet_type: str | None = None,
    strategy: str | None = None,
    settled_only: bool = False,
) -> list[dict]:
    """All positions matching the filters, as plain dicts (schema-tolerant)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE is_paper = ? AND execution_status != 'failed'",
            (1 if is_paper else 0,),
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        d["strategy"] = _row_get(r, "strategy", "value_edge")
        if sport is not None and d.get("sport") != sport:
            continue
        if bet_type is not None and d.get("bet_type") != bet_type:
            continue
        if strategy is not None and d["strategy"] != strategy:
            continue
        if settled_only and d.get("status") != "closed":
            continue
        out.append(d)
    return out


def _parse_entered_at(value: str | None) -> datetime | None:
    """Same tolerant ISO parsing as core/clv_analytics.py::_parse_dt. `entered_at` is
    stored as a naive `datetime.utcnow().isoformat()` string (see storage/db.py), so a
    naive result is treated as already-UTC rather than left ambiguous."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _week_start(dt: datetime) -> str:
    """ISO date of the Monday starting dt's week -- mirrors the bucketing in
    core/clv_analytics.py::weekly_clv_series so the two dashboards agree on what
    "this week" means."""
    return (dt - timedelta(days=dt.weekday())).date().isoformat()


# ── Core stats ───────────────────────────────────────────────────────────────────

def roi(positions: list[dict]) -> tuple[float | None, int]:
    settled = [p for p in positions if p.get("pnl") is not None]
    staked = sum(p["stake"] for p in settled)
    if not settled or staked <= 0:
        return None, len(settled)
    return round(sum(p["pnl"] for p in settled) / staked * 100, 2), len(settled)


def win_rate(positions: list[dict]) -> tuple[float | None, int]:
    settled = [p for p in positions if p.get("pnl") is not None]
    if not settled:
        return None, 0
    wins = sum(1 for p in settled if p["pnl"] >= 0)
    return round(wins / len(settled) * 100, 1), len(settled)


def sharpe_ratio(positions: list[dict]) -> tuple[float | None, int]:
    """
    Per-trade Sharpe (mean pnl-as-%-of-stake / stdev), NOT annualized — trade
    frequency here is too irregular for an annualization factor to mean anything.
    Always returned with n; treat as noise below roughly n=30, and even then it's a
    rough signal, not a precise one, given how lumpy sports settlement timing is.
    """
    settled = [p for p in positions if p.get("pnl") is not None and p.get("stake")]
    n = len(settled)
    if n < 2:
        return None, n
    returns = [p["pnl"] / p["stake"] for p in settled]
    mean = sum(returns) / n
    variance = sum((x - mean) ** 2 for x in returns) / (n - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return None, n
    return round(mean / stdev, 3), n


def roi_confidence_interval(positions: list[dict], confidence: float = 0.90) -> dict:
    """
    Normal-approximation confidence interval on the mean per-trade return
    (pnl/stake, same series sharpe_ratio() builds above) via statistics.NormalDist
    -- stdlib only, no numpy/scipy in this repo. Answers the question a bare ROI
    number can't: is this result distinguishable from a zero-edge outcome at this
    sample size, or indistinguishable from noise.

    Two caveats before treating `significant` as a verdict:
      - It's a normal approximation of the sampling distribution of the mean, most
        trustworthy at larger n -- treat it as directional, not exact, below
        roughly the same n~30 threshold sharpe_ratio's docstring already uses.
      - It treats every settled bet as an independent draw and does NOT correct
        for autocorrelation across bets placed on the same day/slate (correlated
        line moves, correlated matchups). That likely understates the true CI
        width whenever volume clusters into a few slates. Known limitation,
        flagged rather than fixed here.

    Returns {"n", "mean_roi_pct", "ci_low_pct", "ci_high_pct", "confidence",
    "significant"} on a computable sample. Mirrors sharpe_ratio's/max_drawdown's
    (None, n)-style edge-case handling for n < 2 or zero-variance returns, just
    shaped as a dict since callers need more than one field here: returns
    {"n": n, "insufficient_data": True} in that case.
    """
    settled = [p for p in positions if p.get("pnl") is not None and p.get("stake")]
    n = len(settled)
    if n < 2:
        return {"n": n, "insufficient_data": True}
    returns = [p["pnl"] / p["stake"] for p in settled]
    mean = sum(returns) / n
    variance = sum((x - mean) ** 2 for x in returns) / (n - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return {"n": n, "insufficient_data": True}
    se = stdev / math.sqrt(n)
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    lo, hi = mean - z * se, mean + z * se
    return {
        "n": n,
        "mean_roi_pct": round(mean * 100, 2),
        "ci_low_pct": round(lo * 100, 2),
        "ci_high_pct": round(hi * 100, 2),
        "confidence": confidence,
        "significant": lo > 0.0 or hi < 0.0,
        # In-band, not just in the docstring: `significant=True` at n=5 and
        # `significant=True` at n=200 look identical unless the reliability
        # caveat travels with the data itself, not just the source comment.
        "note": (f"n={n} < 30 — normal approximation is directional only at this "
                  "sample size, not exact." if n < 30 else
                  f"n={n} >= 30 — normal approximation should be reasonably reliable."),
    }


def max_drawdown() -> tuple[float | None, int]:
    """Peak-to-trough decline in the bankroll_log history, as a fraction."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT log_date, bankroll FROM bankroll_log ORDER BY log_date ASC"
        ).fetchall()
    if len(rows) < 2:
        return None, len(rows)
    peak = rows[0]["bankroll"]
    max_dd = 0.0
    for r in rows:
        peak = max(peak, r["bankroll"])
        if peak > 0:
            max_dd = max(max_dd, (peak - r["bankroll"]) / peak)
    return round(max_dd, 4), len(rows)


def edge_calibration(positions: list[dict]) -> list[dict]:
    """Estimated edge bucket -> realized ROI/win-rate. The direct tool for questions
    like 'are high-edge readings actually miscalibrated' (raised, not investigated,
    earlier in this project)."""
    settled = [p for p in positions if p.get("pnl") is not None and p.get("edge") is not None]
    out = []
    for lo, hi, label in EDGE_CALIBRATION_BUCKETS:
        bucket = [p for p in settled if lo <= p["edge"] < hi]
        r, n = roi(bucket)
        w, _ = win_rate(bucket)
        out.append({
            # NB: this "n" (roi()'s settled filter: pnl is not None) and the
            # nested roi_ci["n"] (also requires a truthy stake) can disagree if
            # a settled position ever has stake 0/None -- shouldn't happen for
            # a real placed bet, but noted since nothing enforces it structurally.
            "edge_bucket": label, "n": n, "roi_pct": r, "win_rate_pct": w,
            "roi_ci": roi_confidence_interval(bucket),
        })
    return out


def weekly_performance_series(positions: list[dict]) -> list[dict]:
    """ROI/win-rate per ISO week (Monday start), oldest first -- the trend line an
    all-time roi()/win_rate() average can't show. At ~1.8 trades/day, an edge that's
    decaying (model drift, or the market adapting to the bot) would otherwise sit
    hidden inside a smoothed all-time number until it had moved the average by a lot.
    Only settled positions (pnl is not None -- same filter roi()/win_rate() already
    use) contribute; weeks with zero settled positions are omitted rather than
    fabricated with nulls, matching clv_analytics.weekly_clv_series' convention."""
    settled = [p for p in positions if p.get("pnl") is not None]
    buckets: dict[str, list[dict]] = {}
    for p in settled:
        entered = _parse_entered_at(p.get("entered_at"))
        if entered is None:
            continue
        buckets.setdefault(_week_start(entered), []).append(p)

    out = []
    for week in sorted(buckets):
        group = buckets[week]
        r, n = roi(group)
        w, _ = win_rate(group)
        out.append({"week": week, "n": n, "roi_pct": r, "win_rate_pct": w})
    return out


def weekly_edge_calibration_series(positions: list[dict], recent_weeks: int = 4) -> dict:
    """Calibration DRIFT check: recent `recent_weeks` weeks of settled positions vs.
    everything older, each run through the existing edge_calibration() unmodified.

    Deliberately two buckets, not a per-week x per-edge-bucket grid -- at this bot's
    trade volume a full grid would be mostly empty cells (most edge buckets settle a
    handful of trades a week at most), which would mislead more than it would reveal.
    Two buckets is the coarsest split that still isolates "is the edge model's
    real-world accuracy holding up recently, or has it drifted from its own history."
    """
    settled = [p for p in positions if p.get("pnl") is not None]
    dated = [(dt, p) for p in settled
             for dt in [_parse_entered_at(p.get("entered_at"))] if dt is not None]

    def _side(group: list[dict], dts: list[datetime]) -> dict:
        if not group:
            return {"calibration": edge_calibration([]), "n": 0, "week_range": None}
        return {
            "calibration": edge_calibration(group),
            "n": len(group),
            "week_range": f"{_week_start(min(dts))} to {_week_start(max(dts))}",
        }

    if not dated:
        empty = _side([], [])
        return {
            "recent": empty,
            "older": empty,
            "note": "No settled positions with a parseable entered_at -- nothing to compare.",
        }

    latest = max(dt for dt, _ in dated)
    cutoff = latest - timedelta(weeks=recent_weeks)
    recent_pairs = [(dt, p) for dt, p in dated if dt > cutoff]
    older_pairs = [(dt, p) for dt, p in dated if dt <= cutoff]

    recent_side = _side([p for _, p in recent_pairs], [dt for dt, _ in recent_pairs])
    older_side = _side([p for _, p in older_pairs], [dt for dt, _ in older_pairs])

    # Same "don't over-read a small sample" convention as summary_report()'s and
    # scanned_candidates_summary()'s sample_size_warning fields, just tightened to
    # n<20 here since each side is further split into 4 edge buckets internally --
    # by the time a side's n=20, most of its buckets are still single digits.
    notes = []
    if recent_side["n"] < 20:
        notes.append(f"recent n={recent_side['n']} < 20 -- treat as a first look, not a conclusion")
    if older_side["n"] < 20:
        notes.append(f"older n={older_side['n']} < 20 -- treat as a first look, not a conclusion")
    note = "; ".join(notes) if notes else "Both sides have n>=20 -- a real drift comparison, not just noise."

    return {"recent": recent_side, "older": older_side, "note": note}


def breakdown_by(positions: list[dict], key: str) -> list[dict]:
    """Generic groupby — key is any position field (sport, bet_type, strategy,
    fill_type, close_reason, ...)."""
    groups: dict = {}
    for p in positions:
        groups.setdefault(p.get(key), []).append(p)
    out = []
    for val, group in groups.items():
        r, n = roi(group)
        w, _ = win_rate(group)
        out.append({key: val, "n": n, "roi_pct": r, "win_rate_pct": w})
    return sorted(out, key=lambda x: -(x["n"] or 0))


def load_scanned_candidates() -> list[dict]:
    """DANGER: loads the ENTIRE book_probability_log into memory -- 98,644 rows and
    ~678 MB as of 2026-08-15, which is what OOM-killed the 200 MB daily cron job.
    That table is never pruned by design, so this only gets worse. No caller remains;
    scanned_candidates_summary() aggregates in SQL instead. Kept for ad-hoc analysis
    where you genuinely need the rows -- run it outside the memory-capped cron, and
    prefer a WHERE clause.

    Every row in book_probability_log -- not just settled bets, every candidate
    the bot ever scored a Kalshi price against a sportsbook consensus for, passed or
    placed. Rows predating the 2026-08-11 schema widening have edge/status/reason
    etc. as None; callers should filter on those explicitly rather than assume
    presence. See storage/db.py::log_book_probabilities()."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM book_probability_log").fetchall()
    return [dict(r) for r in rows]


def scanned_candidates_summary() -> dict:
    """Deterministic breakdown of book_probability_log, the general scanned-candidate
    archive (widened 2026-08-11 from a narrower book-calibration-only table). This is
    a much larger, less selection-biased sample than `positions` (every candidate
    evaluated, not just the ones that became bets) -- useful for questions about
    where the edge/quality-filter thresholds should sit, not for realized P&L (most
    rows never became a bet, so they have no pnl/stake to report)."""
    # Aggregated in SQL, NOT by loading rows. book_probability_log is the one
    # unbounded table in this database -- it is never pruned by design (see its DDL)
    # and had grown to 98,644 rows by 2026-08-15. load_scanned_candidates() builds a
    # dict per row, which took this function's peak RSS to 678 MB against the cron
    # job's 200 MB cap: the daily check had been silently OOM-killed for days
    # ("Killed" twice in threshold_check.log), leaving the bot with no automated
    # monitoring at all, since the weekly LLM pass is also disabled. Every figure
    # below is a COUNT or an AVG, so none of it ever needed the rows in memory.
    near_miss_lo, near_miss_hi = 0.0, 0.015  # below today's MIN_EDGE=1% floor with margin
    with get_connection() as conn:
        def _scalar(sql: str, args: tuple = ()) -> int:
            return conn.execute(sql, args).fetchone()[0] or 0

        n_total = _scalar("SELECT COUNT(*) FROM book_probability_log")
        n_widened = _scalar(
            "SELECT COUNT(*) FROM book_probability_log WHERE edge IS NOT NULL")

        # Placed candidates carry status=NULL, so they fold into "value".
        status_rows = conn.execute(
            "SELECT COALESCE(status, 'value') AS s, COUNT(*) AS n, AVG(edge) AS avg_edge "
            "FROM book_probability_log WHERE edge IS NOT NULL "
            "GROUP BY s ORDER BY n DESC"
        ).fetchall()
        status_breakdown = [
            {"status": r["s"], "n": r["n"],
             "avg_edge_pct": round((r["avg_edge"] or 0.0) * 100, 2)}
            for r in status_rows
        ]

        n_near_miss = _scalar(
            "SELECT COUNT(*) FROM book_probability_log WHERE edge IS NOT NULL "
            "AND position_id IS NULL AND edge >= ? AND edge < ?",
            (near_miss_lo, near_miss_hi))
        n_placed = _scalar(
            "SELECT COUNT(*) FROM book_probability_log "
            "WHERE edge IS NOT NULL AND position_id IS NOT NULL")
        n_with_outcome = _scalar(
            "SELECT COUNT(*) FROM book_probability_log "
            "WHERE edge IS NOT NULL AND actual_outcome IS NOT NULL")

    return {
        "n_total_rows": n_total,
        "n_widened_schema": n_widened,
        "note": (
            f"{n_total - n_widened} rows predate the 2026-08-11 schema widening and "
            "only have book-calibration fields (no edge/status/reason)."
            if n_total > n_widened else
            "All rows have the widened schema."
        ),
        "by_status": status_breakdown,
        "n_rejected_near_miss_sub_1.5pct_edge": n_near_miss,
        "n_placed_linked_to_position": n_placed,
        "n_with_backfilled_outcome": n_with_outcome,
        "sample_size_warning": (
            f"n_widened_schema={n_widened} — this dataset only started accumulating "
            "widened rows on 2026-08-11; treat early breakdowns as a first look, not "
            "a conclusion, until it's had more scan cycles to build up."
        ),
    }


def fee_and_fill_stats(positions: list[dict]) -> dict:
    settled = [p for p in positions if p.get("pnl") is not None]
    n = len(settled)
    if n == 0:
        return {"n": 0}
    maker = sum(1 for p in settled if p.get("fill_type") == "maker")
    fees = [p.get("entry_fee_paid") or 0.0 for p in settled]
    return {
        "n": n,
        "maker_pct": round(maker / n * 100, 1),
        "avg_fee_paid": round(sum(fees) / n, 4),
    }


def _hypothetical_positions(candidates: list[dict], unit_stake: float) -> list[dict]:
    """Turn book_probability_log candidate rows into position-shaped {pnl, stake}
    dicts at a flat unit_stake, so roi()/win_rate() (which only know how to read
    `pnl`/`stake`) can be reused unchanged instead of reimplementing that math
    here a third time.

    P&L formula mirrors storage/db.py::settle_position() exactly (won:
    stake*(1-price)/price, lost: -stake) with price=kalshi_price and win/loss
    read off actual_outcome (1.0/0.0 -- see get_qualifying_candidates_with_
    outcomes()'s docstring for the encoding). Unlike settle_position(), no
    entry_fee_paid is subtracted: these candidates were never filled, so there
    is no real fee to read, only the estimate real trades don't rely on
    elsewhere in this file. Rows with a missing/non-positive kalshi_price or a
    missing actual_outcome are skipped (can't price a hypothetical bet)."""
    out = []
    for c in candidates:
        price = c.get("kalshi_price")
        outcome = c.get("actual_outcome")
        if price is None or price <= 0 or outcome is None:
            continue
        pnl = unit_stake * (1.0 - price) / price if outcome else -unit_stake
        out.append({"pnl": pnl, "stake": unit_stake})
    return out


def counterfactual_backtest(
    candidates: list[dict],
    real_positions: list[dict],
    unit_stake: float = 10.0,
    seed: int = 42,
) -> dict:
    """Would the bot have done just as well betting EVERY qualifying candidate,
    or a random same-sized subset of them, instead of ranking by composite
    score (edge x Kelly fraction x book_confidence x agreement) and taking the
    top 5/day? Compares three legs at the same flat unit_stake so they're
    apples-to-apples:

      flat_all_qualifiers    -- every row in `candidates` bet at unit_stake.
      random_n_of_qualifiers -- one random.Random(seed) draw (not resampled or
        averaged -- a single seeded draw keeps this reproducible) of
        len([c for c in candidates if c.get("position_id")]) candidates from
        the full pool, i.e. the same NUMBER of bets the bot actually placed
        out of this qualifying population, picked without the ranking.
      actual                 -- the real realized numbers from `real_positions`,
        via the existing roi()/win_rate() (not reimplemented here).

    Deterministic: candidates/real_positions are plain data (no DB access in
    this function) and random.Random(seed) is a fresh, locally-scoped
    generator (not the global `random` module state), so identical inputs
    always produce identical output regardless of call order or what else in
    the process has touched the `random` module -- but note this also depends
    on `candidates` arriving in a stable order (see get_qualifying_candidates_
    with_outcomes()'s ORDER BY).

    Each of flat_roi/rand_roi can independently be None (not enough priced/
    settled candidates on that leg specifically -- e.g. a random draw of size
    0 when nothing has ever been placed yet). The verdict logic below treats
    "this baseline isn't computable" as its own case, distinct from "actual
    beat/lagged it" -- conflating a None comparison with a loss would silently
    misreport "no data" as "the ranking underperformed."
    """
    all_hyp = _hypothetical_positions(candidates, unit_stake)
    flat_roi, flat_n = roi(all_hyp)
    flat_win, _ = win_rate(all_hyp)

    n_actual_placed = len([c for c in candidates if c.get("position_id")])
    k = min(n_actual_placed, len(candidates))
    sampled = random.Random(seed).sample(candidates, k) if k > 0 else []
    sampled_hyp = _hypothetical_positions(sampled, unit_stake)
    rand_roi, rand_n = roi(sampled_hyp)
    rand_win, _ = win_rate(sampled_hyp)

    act_roi, act_n = roi(real_positions)
    act_win, _ = win_rate(real_positions)

    baselines_available = [b for b in (("flat-all-qualifiers", flat_roi), ("random subset", rand_roi))
                           if b[1] is not None]

    if act_roi is None or act_n == 0:
        verdict = "NO DATA — no settled real positions to compare against the baselines."
    elif not baselines_available:
        verdict = (f"BASELINES UNAVAILABLE — actual ROI is {act_roi}% (n={act_n}) but "
                   "neither baseline has enough priced/settled qualifying candidates "
                   "to compare against yet.")
    elif len(baselines_available) == 1:
        name, base_roi = baselines_available[0]
        missing = "random subset" if name == "flat-all-qualifiers" else "flat-all-qualifiers"
        beat = act_roi > base_roi
        verdict = (f"PARTIAL COMPARISON — actual {act_roi}% {'beat' if beat else 'lagged'} "
                   f"the only available baseline ({name}, {base_roi}%); {missing} isn't "
                   "computable yet (not enough priced/settled candidates on that leg), "
                   "so this is not a full verdict.")
    else:
        beats_flat = act_roi > flat_roi
        beats_random = act_roi > rand_roi
        if beats_flat and beats_random:
            verdict = (f"RANKING ADDS VALUE — actual {act_roi}% beat both flat-all-"
                       f"qualifiers ({flat_roi}%) and a random same-sized subset "
                       f"({rand_roi}%).")
        elif not beats_flat and not beats_random:
            verdict = (f"RANKING NOT ADDING VALUE — actual {act_roi}% lagged both "
                       f"flat-all-qualifiers ({flat_roi}%) and a random same-sized "
                       f"subset ({rand_roi}%); the composite-score ranking is "
                       "indistinguishable from (or worse than) not ranking at all.")
        else:
            verdict = (f"MIXED — actual {act_roi}% beat one baseline but not the other "
                       f"(flat-all-qualifiers {flat_roi}%, random subset {rand_roi}%); "
                       "not enough signal yet to call the ranking a clear net positive.")

    return {
        "unit_stake": unit_stake,
        "seed": seed,
        "flat_all_qualifiers": {"n": flat_n, "roi_pct": flat_roi, "win_rate_pct": flat_win},
        "random_n_of_qualifiers": {
            "n": rand_n, "roi_pct": rand_roi, "win_rate_pct": rand_win,
            "n_sampled": k,
        },
        "actual": {"n": act_n, "roi_pct": act_roi, "win_rate_pct": act_win},
        "verdict": verdict,
    }


def _dk_hypothetical_positions(rows: list[dict], unit_stake: float) -> list[dict]:
    """Same shape and formula as _hypothetical_positions() above, applied to
    dk_scaled_shadow_log rows instead of book_probability_log rows: won =
    stake*(1-price)/price, lost = -stake, price = kalshi_price, win/loss read
    off actual_outcome (1.0/0.0 -- see storage/db.py's DDL comment on that
    column). Kept as a separate function rather than reused across both
    tables because the two source tables have different column sets and
    different callers reason about them independently; the formula itself is
    intentionally identical."""
    out = []
    for r in rows:
        price = r.get("kalshi_price")
        outcome = r.get("actual_outcome")
        if price is None or price <= 0 or outcome is None:
            continue
        pnl = unit_stake * (1.0 - price) / price if outcome else -unit_stake
        out.append({"pnl": pnl, "stake": unit_stake})
    return out


def dk_scaled_shadow_backtest(rows: list[dict], unit_stake: float = 10.0) -> dict:
    """Would the DK-scaled player-prop estimator's picks actually have made
    money, not just been well-calibrated? storage/db.py::
    get_dk_scaled_shadow_summary()'s Brier score answers "was scaled_prob close
    to the truth" -- that is necessary but not sufficient for profitability,
    and it is not the question that decides whether DK_SCALED_SHADOW_MODE gets
    flipped off. This is.

    Two top-line comparisons, both hypothetical (nothing here was actually
    filled -- shadow mode never places an order):
      would_bet   -- ROI/win-rate on only the rows that cleared every real gate
                     (would_bet=1). This is the EXACT population that becomes
                     real trades the day this feature goes live -- the number
                     that matters most.
      all_settled -- ROI/win-rate on every settled estimate regardless of
                     would_bet. Comparing the two shows whether the edge/
                     quality gate is actually selecting the better bets, or
                     just cutting volume without improving the average.

    Plus a by-distance-bucket profitability breakdown on the would_bet subset,
    using the same rung buckets as get_dk_scaled_shadow_summary's Brier
    breakdown (DK_DISTANCE_BUCKETS, kept in sync with it on purpose). This is
    the direct test for a confound flagged when that Brier breakdown first came
    back non-monotonic (worse at 1.5-3 rungs than at 0.5-1.5, then BEST of all
    at 3+): Brier score alone can't distinguish "this bucket is well-calibrated"
    from "this bucket is full of extreme, easy-to-score long shots" -- ROI can.
    If the 3+ bucket's ROI is also strong, that's real signal; if it's flat or
    negative despite the good Brier, the Brier result was a probability-
    extremity artifact, not evidence the estimator is trustworthy that far out.
    """
    would_bet_rows = [r for r in rows if r.get("would_bet")]
    would_bet_hyp = _dk_hypothetical_positions(would_bet_rows, unit_stake)
    wb_roi, wb_n = roi(would_bet_hyp)
    wb_win, _ = win_rate(would_bet_hyp)
    wb_ci = roi_confidence_interval(would_bet_hyp)

    all_hyp = _dk_hypothetical_positions(rows, unit_stake)
    all_roi, all_n = roi(all_hyp)
    all_win, _ = win_rate(all_hyp)

    by_distance = []
    for lo, hi, label in DK_DISTANCE_BUCKETS:
        bucket_rows = [r for r in would_bet_rows if lo <= abs(r.get("distance") or 0.0) < hi]
        bucket_hyp = _dk_hypothetical_positions(bucket_rows, unit_stake)
        b_roi, b_n = roi(bucket_hyp)
        b_win, _ = win_rate(bucket_hyp)
        by_distance.append({
            "range": label, "n": b_n, "roi_pct": b_roi, "win_rate_pct": b_win,
            "roi_ci": roi_confidence_interval(bucket_hyp),
        })

    if wb_roi is None:
        verdict = ("NO DATA — no would-bet estimate has both a priced kalshi_price "
                   "and a settled outcome yet.")
    else:
        sig = wb_ci.get("significant")
        sig_text = "significant" if sig else "not yet distinguishable from zero"
        conf_pct = wb_ci.get("confidence", 0.9) * 100
        verdict = (f"WOULD-BET ROI {wb_roi}% (n={wb_n}, win rate {wb_win}%) — "
                   f"{sig_text} at {conf_pct:.0f}% confidence.")

    return {
        "unit_stake": unit_stake,
        "would_bet": {"n": wb_n, "roi_pct": wb_roi, "win_rate_pct": wb_win, "roi_ci": wb_ci},
        "all_settled": {"n": all_n, "roi_pct": all_roi, "win_rate_pct": all_win},
        "by_distance_bucket": by_distance,
        "verdict": verdict,
    }


# ── Top-level report ─────────────────────────────────────────────────────────────

def mm_health(days: int = 7, is_paper: bool = False) -> dict:
    """
    The three questions that decide whether market making is worth keeping, in the
    order they have to be answered — each one is only meaningful if the previous
    one passed.

      1. Does it ever QUOTE?  If the market universe has no trading activity,
         nothing downstream matters. Measured from mm_daily_stats.
      2. Does a quote ever FILL?  Quoting into a book nobody crosses earns nothing.
      3. Do fills come in PAIRS?  This is the real test. MM only profits when BOTH
         legs fill: a matched YES+NO pair costs under $1 and pays exactly $1, so
         the outcome is irrelevant. A leg that fills alone is not market making at
         all -- it is an accidental directional bet. As of 2026-08-15 the bot had
         completed ONE matched pair in its entire life and was holding $6.39 of
         unpaired exposure.

    Returns a dict with a `verdict` key that maps directly onto an action.
    """
    from storage import db as _db   # path already bootstrapped at module import

    rows = _db.get_mm_daily_stats(days=days)
    ticks = sum(r["ticks"] for r in rows)
    quoted = sum(r["quoted"] for r in rows)
    fills = sum(r["fills"] for r in rows)

    reasons: dict[str, int] = {}
    for r in rows:
        try:
            for k, v in json.loads(r["reasons_json"] or "{}").items():
                reasons[k] = reasons.get(k, 0) + int(v)
        except (ValueError, TypeError):
            continue
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
    total_reasons = sum(reasons.values()) or 1

    pairing = _db.get_mm_pairing(is_paper=is_paper)
    naked = [p for p in pairing if p["unpaired"] > 0]
    paired_now = sum(p["paired"] for p in pairing)
    naked_dollars = round(sum(p["unpaired_dollars"] for p in naked), 2)

    dominant = top[0] if top else ("", 0)
    dominant_share = dominant[1] / total_reasons

    if ticks == 0:
        verdict = "NO DATA — market making has not ticked in this window."
        action = "Check ENABLE_MARKET_MAKING and that the bot is running."
    elif quoted == 0:
        verdict = (f"NOT QUOTING — {ticks} ticks, 0 quotes. Dominant reason: "
                   f"{dominant[0]} ({dominant_share*100:.0f}% of rejections).")
        action = ("If this persists and the reason is insufficient_volume, there are "
                  "no counterparties in MM's universe. Set ENABLE_MARKET_MAKING=false.")
    elif fills == 0:
        verdict = f"QUOTING BUT NOT FILLING — {quoted} quotes, 0 fills in {days}d."
        action = "Quotes may be priced too far inside. Worth investigating."
    elif paired_now == 0 and naked_dollars > 0:
        verdict = (f"FILLING BUT NEVER PAIRING — ${naked_dollars} naked across "
                   f"{len(naked)} ticker(s), 0 matched pairs.")
        action = ("These are accidental directional bets, not spread capture. "
                  "Turn off, or investigate why only one side fills.")
    else:
        verdict = f"WORKING — {paired_now:.0f} matched pair(s) open, ${naked_dollars} naked."
        action = "Keep running; tune from here."

    return {
        "window_days": days,
        "ticks": ticks,
        "quoted": quoted,
        "fills_recorded": fills,
        "top_rejection_reasons": [{"reason": k, "count": v} for k, v in top],
        "matched_pairs_open": paired_now,
        "naked_exposure_dollars": naked_dollars,
        "naked_positions": naked,
        "verdict": verdict,
        "action": action,
    }


def summary_report(is_paper: bool = False) -> dict:
    positions = load_positions(is_paper=is_paper)
    settled = [p for p in positions if p.get("pnl") is not None]
    open_n = sum(1 for p in positions if p.get("status") == "open")

    r, n = roi(positions)
    w, _ = win_rate(positions)
    sr, sn = sharpe_ratio(positions)
    dd, dd_n = max_drawdown()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "is_paper": is_paper,
        "n_settled": n,
        "n_open": open_n,
        "roi_pct": r,
        "win_rate_pct": w,
        "roi_ci": roi_confidence_interval(positions),
        "sharpe": {"value": sr, "n": sn, "note": "per-trade, not annualized; treat as noise below n~30"},
        "max_drawdown": {"value": dd, "n_snapshots": dd_n},
        "by_sport": breakdown_by(positions, "sport"),
        "by_bet_type": breakdown_by(positions, "bet_type"),
        "by_strategy": breakdown_by(positions, "strategy"),
        "by_fill_type": breakdown_by(positions, "fill_type"),
        "by_close_reason": breakdown_by([p for p in positions if p.get("close_reason")], "close_reason"),
        "edge_calibration": edge_calibration(positions),
        "weekly_performance": weekly_performance_series(positions),
        "calibration_drift": weekly_edge_calibration_series(positions),
        "fees_and_fills": fee_and_fill_stats(positions),
        "scanned_candidates": scanned_candidates_summary(),
        "counterfactual": counterfactual_backtest(
            get_qualifying_candidates_with_outcomes(is_paper=is_paper), positions),
        "market_making": mm_health(is_paper=is_paper),
        "ambiguous_matches": ambiguous_match_report(),
        "sample_size_warning": (
            f"n_settled={n} — most statistics above are low-confidence below ~n=30. "
            "Treat single-digit-n breakdowns as anecdotes, not findings."
        ),
    }


# ── Threshold check (free, zero-LLM) ────────────────────────────────────────────

def check_thresholds() -> Path | None:
    """Compare today's summary against the last reviewed snapshot; write a
    TRIGGER_<date>.md file and return its path iff something crossed a real
    threshold. Returns None (no file written) otherwise. Always updates
    last_snapshot.json's 'checked_at' so repeated runs are idempotent about the
    review baseline itself (the baseline only advances when a human/agent actually
    reviews it — see README)."""
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    current = summary_report(is_paper=False)
    _write_mm_status(current.get("market_making") or {})
    _write_ambiguous_status(current.get("ambiguous_matches") or {})

    previous = None
    if SNAPSHOT_PATH.exists():
        try:
            previous = json.loads(SNAPSHOT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            previous = None

    reasons = []
    if previous is not None:
        new_settled = (current["n_settled"] or 0) - (previous.get("n_settled") or 0)
        if new_settled >= MIN_NEW_SETTLED_TRADES:
            reasons.append(f"{new_settled} new settled trades since last review "
                            f"(threshold: {MIN_NEW_SETTLED_TRADES})")
    dd = current["max_drawdown"]["value"]
    if dd is not None and dd >= DRAWDOWN_ALERT_FRACTION:
        reasons.append(f"drawdown {dd*100:.1f}% >= alert threshold {DRAWDOWN_ALERT_FRACTION*100:.0f}%")

    if not reasons:
        return None

    trigger_path = FINDINGS_DIR / f"TRIGGER_{date.today().isoformat()}.md"
    trigger_path.write_text(
        "---\n"
        f"date: {date.today().isoformat()}\n"
        "triggered_by: threshold\n"
        "---\n\n"
        "## Trigger reasons\n\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\n\n## Snapshot at trigger time\n\n```json\n"
        + json.dumps(current, indent=2)
        + "\n```\n"
    )
    return trigger_path


def ambiguous_match_report() -> dict:
    """
    Markets the bot refused to bet because it could not tell which team they cover.

    Each of these is a LOST OPPORTUNITY the matcher should eventually handle, not a
    solved problem — the refusal only prevents the bad outcome (position #930 bought
    the opposite team), it doesn't recover the good one. Surfaced in the daily check
    so they get fixed rather than silently accumulating.
    """
    from storage.db import get_ambiguous_matches

    try:
        rows = [dict(r) for r in get_ambiguous_matches()]
    except Exception:  # table may predate the migration on an older DB copy
        return {}

    if not rows:
        return {"open_count": 0, "total_occurrences": 0, "cases": []}

    rows.sort(key=lambda r: -(r.get("occurrences") or 0))
    return {
        "open_count": len(rows),
        "total_occurrences": sum(r.get("occurrences") or 0 for r in rows),
        "by_context": Counter(r.get("context") for r in rows),
        "cases": [
            {
                "id": r["id"],
                "kalshi_name": r["kalshi_name"],
                "matchup": f"{r['away_team']} @ {r['home_team']}",
                "ticker": r["kalshi_ticker"],
                "context": r["context"],
                "sport": r.get("sport"),
                "scores": [r.get("home_score"), r.get("away_score")],
                "occurrences": r.get("occurrences"),
                "first_seen": r.get("first_seen"),
                "last_seen": r.get("last_seen"),
            }
            for r in rows[:25]
        ],
    }


MM_STATUS_PATH = FINDINGS_DIR / "MM_STATUS.md"
AMBIGUOUS_STATUS_PATH = FINDINGS_DIR / "AMBIGUOUS_MATCHES.md"


def _write_ambiguous_status(a: dict) -> None:
    """Overwrite findings/AMBIGUOUS_MATCHES.md with the open backlog. Rewritten in
    full each run, same convention as MM_STATUS.md: the file is the CURRENT list of
    things to fix, not a log to scroll."""
    if not a:
        return
    lines = [
        "# Ambiguous team matches (bets we refused)",
        "",
        f"_Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC_",
        "",
    ]
    if not a.get("open_count"):
        lines += [
            "**None outstanding.** No market has been skipped for an unresolvable "
            "team name.",
            "",
            "This is the good state, but it is not proof the matcher is correct — it "
            "only means nothing has TIED. A wrong-but-unambiguous match would not "
            "appear here.",
        ]
        AMBIGUOUS_STATUS_PATH.write_text("\n".join(lines))
        return

    lines += [
        f"**{a['open_count']} unresolved**, hit {a['total_occurrences']} times total.",
        "",
        "Each row is a market we could have priced but refused, because the Kalshi "
        "team name matched both clubs equally (or neither). Fixing one usually means "
        "teaching the matcher an alias — e.g. `WS -> White Sox`.",
        "",
        "| Kalshi name | matchup | scores (H/A) | hits | last seen | ticker |",
        "|---|---|---|---:|---|---|",
    ]
    for c in a.get("cases", []):
        h, aw = c.get("scores") or [None, None]
        sc = f"{h:g}/{aw:g}" if h is not None and aw is not None else "—"
        lines.append(
            f"| `{c['kalshi_name']}` | {c['matchup']} | {sc} | {c['occurrences']} | "
            f"{(c.get('last_seen') or '')[:16]} | `{c['ticker']}` |"
        )
    lines += [
        "",
        "Mark one handled with `db.resolve_ambiguous_match(<id>)` once the matcher "
        "resolves it.",
    ]
    AMBIGUOUS_STATUS_PATH.write_text("\n".join(lines))


def _write_mm_status(h: dict) -> None:
    """Overwrite findings/MM_STATUS.md with a plain-English answer to 'is market
    making worth keeping'. Rewritten in full every run (not appended) so the file
    is always the CURRENT answer rather than a log to scroll through -- the daily
    check exists to be read in ten seconds."""
    if not h:
        return
    reasons = h.get("top_rejection_reasons") or []
    total = sum(r["count"] for r in reasons) or 1
    naked = h.get("naked_positions") or []

    lines = [
        f"# Market making status — {date.today().isoformat()}",
        "",
        f"## {h.get('verdict', 'unknown')}",
        "",
        f"**What to do:** {h.get('action', '')}",
        "",
        f"Window: last {h.get('window_days')} days.",
        "",
        "| question | answer |",
        "|---|---|",
        f"| 1. Did it ever quote? | **{h.get('quoted', 0)}** quotes across "
        f"{h.get('ticks', 0)} ticks |",
        f"| 2. Did any quote fill? | **{h.get('fills_recorded', 0)}** fills |",
        f"| 3. Did fills PAIR? | **{h.get('matched_pairs_open', 0)}** matched pairs open, "
        f"**${h.get('naked_exposure_dollars', 0)}** naked |",
        "",
    ]
    if reasons:
        lines += ["## Why it didn't quote", "",
                  "| reason | count | share |", "|---|---|---|"]
        lines += [f"| {r['reason']} | {r['count']} | {r['count']/total*100:.0f}% |"
                  for r in reasons]
        lines.append("")
    if naked:
        lines += [
            "## Naked (unpaired) exposure",
            "",
            "A matched YES+NO pair costs under $1 and pays exactly $1, so the outcome",
            "doesn't matter. A leg that fills alone is an accidental directional bet.",
            "",
            "| ticker | unpaired | side | $ |", "|---|---|---|---|",
        ]
        lines += [f"| {p['ticker']} | {p['unpaired']:.0f} | {p['naked_side'].upper()} "
                  f"| ${p['unpaired_dollars']:.2f} |" for p in naked]
        lines.append("")
    MM_STATUS_PATH.write_text("\n".join(lines))


def _save_snapshot(report: dict) -> None:
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-thresholds", action="store_true",
                         help="Free, zero-LLM daily check — writes a TRIGGER_*.md "
                              "file if a real threshold is crossed, else does nothing.")
    parser.add_argument("--save-snapshot", action="store_true",
                         help="After printing, save this run's report as the new "
                              "baseline for --check-thresholds (call this after a "
                              "review actually happens, not on every check).")
    args = parser.parse_args()

    if args.check_thresholds:
        trigger = check_thresholds()
        if trigger:
            print(f"THRESHOLD CROSSED: {trigger}")
        else:
            print("No threshold crossed — nothing to do.")
        return

    report = summary_report(is_paper=False)
    print(json.dumps(report, indent=2))
    if args.save_snapshot:
        _save_snapshot(report)


if __name__ == "__main__":
    main()
