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
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from storage.db import get_connection  # noqa: E402  (read-only use only)

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
        out.append({"edge_bucket": label, "n": n, "roi_pct": r, "win_rate_pct": w})
    return out


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
        "sharpe": {"value": sr, "n": sn, "note": "per-trade, not annualized; treat as noise below n~30"},
        "max_drawdown": {"value": dd, "n_snapshots": dd_n},
        "by_sport": breakdown_by(positions, "sport"),
        "by_bet_type": breakdown_by(positions, "bet_type"),
        "by_strategy": breakdown_by(positions, "strategy"),
        "by_fill_type": breakdown_by(positions, "fill_type"),
        "by_close_reason": breakdown_by([p for p in positions if p.get("close_reason")], "close_reason"),
        "edge_calibration": edge_calibration(positions),
        "fees_and_fills": fee_and_fill_stats(positions),
        "scanned_candidates": scanned_candidates_summary(),
        "market_making": mm_health(is_paper=is_paper),
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


MM_STATUS_PATH = FINDINGS_DIR / "MM_STATUS.md"


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
