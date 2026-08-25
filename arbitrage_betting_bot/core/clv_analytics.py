"""CLV (closing-line value) and TTE (time-to-event) analytics for settled positions.

Added 2026-08-25 as part of a dashboard revamp: P&L is now tracked externally
(Pikkit, synced directly to the Kalshi account), so this dashboard's job is the
thing Pikkit does NOT do well -- CLV broken down over time and by sport/league/bet
type, plus whether time-to-event at bet placement correlates with outcome.

Pure functions over plain dicts (position rows) -- no DB or Flask coupling, so the
math can be unit tested directly. See storage/db.py::get_positions_for_clv_analytics
for where the rows come from and dashboard_server.py's /clv page for the display.

TWO CLV METRICS, deliberately kept separate:
  kalshi_clv     = kalshi_close_price - market_price
                   Did KALSHI'S OWN price move toward our side after we entered?
                   Positive means we paid less than the market later settled on --
                   the classic CLV signal, historically correlated with long-run
                   edge independent of any single bet's outcome.
  consensus_clv  = consensus_close_prob - consensus_prob
                   Did the SHARP SPORTSBOOK consensus move toward our side after we
                   bet? This validates the read itself, separately from execution --
                   a real risk since Kalshi is a much thinner market than Pinnacle.
Both are already side-adjusted at the source (market_price, kalshi_close_price,
consensus_prob, consensus_close_prob are all "for the side we actually bought" --
see execution/auto_settle.py::_fetch_kalshi_closing_price's docstring), so no sign
flipping is needed here for NO-side positions.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

TTE_BUCKETS: list[tuple[float, float]] = [
    (0, 1), (1, 3), (3, 6), (6, 12), (12, 24), (24, 48), (48, float("inf")),
]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _pct(numerator_pred, values: list) -> float | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(100.0 * sum(1 for v in values if numerator_pred(v)) / len(values), 1)


def pearson_correlation(xs: list[float | None], ys: list[float | None]) -> float | None:
    """Pearson r over paired (x, y), dropping any pair where either side is None.
    Used both for TTE-vs-CLV (continuous) and TTE-vs-won (0/1, i.e. point-biserial,
    which is just Pearson r on a binary variable). None below 3 usable pairs or zero
    variance in either axis -- a correlation coefficient on 1-2 points is noise, not
    signal."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    varx = sum((x - mx) ** 2 for x in (p[0] for p in pairs))
    vary = sum((y - my) ** 2 for y in (p[1] for p in pairs))
    if varx == 0 or vary == 0:
        return None
    return round(cov / (varx ** 0.5 * vary ** 0.5), 4)


def compute_row(pos: dict) -> dict:
    """Derive tte_hours, kalshi_clv, consensus_clv, and won for one closed position.
    Any of these may be None if the inputs needed to compute them are missing
    (e.g. closing lines never got captured, or this is an older position from
    before consensus_prob was recorded at entry)."""
    entered = _parse_dt(pos.get("entered_at"))
    commence = _parse_dt(pos.get("commence_time"))
    tte_hours = None
    if entered and commence:
        tte_hours = round((commence - entered).total_seconds() / 3600.0, 2)

    kalshi_clv = None
    if pos.get("kalshi_close_price") is not None and pos.get("market_price") is not None:
        kalshi_clv = round(pos["kalshi_close_price"] - pos["market_price"], 4)

    consensus_clv = None
    if pos.get("consensus_close_prob") is not None and pos.get("consensus_prob") is not None:
        consensus_clv = round(pos["consensus_close_prob"] - pos["consensus_prob"], 4)

    pnl = pos.get("pnl")
    won = None if pnl is None else pnl > 0

    return {**pos, "tte_hours": tte_hours, "kalshi_clv": kalshi_clv,
            "consensus_clv": consensus_clv, "won": won}


def compute_rows(positions: list[dict]) -> list[dict]:
    return [compute_row(p) for p in positions]


def overall_summary(rows: list[dict]) -> dict:
    kalshi_clvs = [r["kalshi_clv"] for r in rows]
    consensus_clvs = [r["consensus_clv"] for r in rows]
    wins = [r["won"] for r in rows]
    won_numeric = [None if r["won"] is None else (1.0 if r["won"] else 0.0) for r in rows]
    tte = [r["tte_hours"] for r in rows]
    return {
        "n": len(rows),
        "n_with_kalshi_clv": sum(1 for v in kalshi_clvs if v is not None),
        "n_with_consensus_clv": sum(1 for v in consensus_clvs if v is not None),
        "n_with_outcome": sum(1 for v in wins if v is not None),
        "mean_kalshi_clv": _mean(kalshi_clvs),
        "mean_consensus_clv": _mean(consensus_clvs),
        "pct_positive_kalshi_clv": _pct(lambda v: v > 0, kalshi_clvs),
        "pct_positive_consensus_clv": _pct(lambda v: v > 0, consensus_clvs),
        "win_rate": _pct(lambda v: v is True, wins),
        "tte_vs_kalshi_clv_corr": pearson_correlation(tte, kalshi_clvs),
        "tte_vs_consensus_clv_corr": pearson_correlation(tte, consensus_clvs),
        "tte_vs_win_corr": pearson_correlation(tte, won_numeric),
    }


def _group_stats(rows: list[dict]) -> dict:
    return {
        "n": len(rows),
        "win_rate": _pct(lambda v: v is True, [r["won"] for r in rows]),
        "mean_kalshi_clv": _mean([r["kalshi_clv"] for r in rows]),
        "mean_consensus_clv": _mean([r["consensus_clv"] for r in rows]),
    }


def group_by_field(rows: list[dict], field: str) -> list[dict]:
    """Aggregate CLV/win-rate by a flat field (sport, bet_type, ...), largest
    group first."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r.get(field) or "unknown"].append(r)
    out = [{"key": k, **_group_stats(g)} for k, g in groups.items()]
    return sorted(out, key=lambda x: -x["n"])


def bucket_by_tte(rows: list[dict], buckets: list[tuple[float, float]] = TTE_BUCKETS) -> list[dict]:
    """CLV/win-rate broken out by how long before game time the bet was placed --
    the correlation the dashboard exists to surface. Buckets in HOURS before
    commence_time; a bet placed AFTER commence_time (tte_hours < 0, e.g. an
    in-play/live line) is excluded from every bucket rather than silently landing
    in the first one."""
    out = []
    for lo, hi in buckets:
        bucket_rows = [r for r in rows if r["tte_hours"] is not None and lo <= r["tte_hours"] < hi]
        label = f"{lo:g}–{hi:g}h" if hi != float("inf") else f"{lo:g}h+"
        out.append({"range": label, **_group_stats(bucket_rows)})
    return out


def weekly_clv_series(rows: list[dict]) -> list[dict]:
    """Mean kalshi_clv per ISO week (Monday start), oldest first -- the CLV-over-
    time trend line. Only rows with a resolved kalshi_clv contribute."""
    buckets: dict[str, list[float]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        entered = _parse_dt(r.get("entered_at"))
        if entered is None:
            continue
        week_start = (entered - timedelta(days=entered.weekday())).date().isoformat()
        counts[week_start] += 1
        if r["kalshi_clv"] is not None:
            buckets[week_start].append(r["kalshi_clv"])
    weeks = sorted(set(counts) | set(buckets))
    return [
        {"week": w, "n": counts.get(w, 0),
         "mean_kalshi_clv": _mean(buckets.get(w, []))}
        for w in weeks
    ]
