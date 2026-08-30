"""
Arbitrage Betting Bot — Web Dashboard

Usage:
    python dashboard_server.py              # Live at http://localhost:5000
    python dashboard_server.py --paper      # Paper-mode stats
    python dashboard_server.py --port 8080  # Custom port

Access from your phone:
    Find your Mac's local IP:  ipconfig getifaddr en0
    Then open:  http://<mac-ip>:5000  on any device on the same WiFi.

Settle a position (from the terminal, not this server):
    python dashboard.py --settle 5 --won
    python dashboard.py --settle 5 --lost
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    import pytz
    _PT = pytz.timezone("America/Los_Angeles")
from functools import wraps

from flask import Flask, jsonify, render_template_string, abort, request, Response
import storage.db as db
from core.odds_converter import american_to_prob, _devig, _norm_team, _names_match
import core.clv_analytics as clv
from execution.auto_settle import auto_settle_positions
from data.kalshi_client import KalshiClient
import config
import re as _re

app = Flask(__name__)
IS_PAPER = False   # set by CLI arg at startup


def _requires_auth(f):
    """HTTP Basic Auth guard. Skipped if DASHBOARD_PASSWORD is not set."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not config.DASHBOARD_PASSWORD:
            return f(*args, **kwargs)
        auth = request.authorization
        password_ok = auth and auth.password == config.DASHBOARD_PASSWORD
        username_ok = (not config.DASHBOARD_USERNAME) or (auth and auth.username == config.DASHBOARD_USERNAME)
        if not (password_ok and username_ok):
            return Response(
                "Unauthorized", 401,
                {"WWW-Authenticate": 'Basic realm="Arbitrage Dashboard"'},
            )
        return f(*args, **kwargs)
    return decorated


# ── Data helpers (same logic as dashboard.py) ────────────────────────────────

_KALSHI_SLUG: dict[str, str] = {
    "KXNFLGAME":   "nfl-game",    "KXNCAAFGAME": "ncaaf-game",
    "KXNBAGAME":   "nba-game",    "KXNCAABGAME": "ncaab-game",
    "KXMLBGAME":   "mlb-game",    "KXNHLGAME":   "nhl-game",
    "KXMLSGAME":   "mls-game",    "KXEPLGAME":   "epl-game",
    "KXUCLGAME":   "uefa-champions-league-game",
    "KXNBATOTAL":  "nba-total",   "KXMLBTOTAL":  "mlb-total",
    "KXNHLTOTAL":  "nhl-total",   "KXEPLTOTAL":  "epl-total",
    "KXUCLTOTAL":  "ucl-total",   "KXMLSTOTAL":  "mls-total",
    "KXNBASPREAD": "nba-spread",  "KXMLBSPREAD": "mlb-spread",
    "KXNHLSPREAD": "nhl-spread",  "KXMLSSPREAD": "mls-spread",
    "KXEPLSPREAD": "epl-spread",  "KXUCLSPREAD": "ucl-spread",
}


def _kalshi_market_url(ticker: str) -> str:
    """Build a Kalshi market URL from a ticker like KXMLBTOTAL-26APR09CWSKC-9."""
    if not ticker:
        return ""
    # event_ticker = everything except the last segment (the threshold/team suffix)
    parts = ticker.split("-")
    event = "-".join(parts[:2]).lower() if len(parts) >= 2 else ticker.lower()
    series = parts[0].upper()
    slug = _KALSHI_SLUG.get(series, series.lower())
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event}"


def _col(row, name: str, default=None):
    """Read a column that may not exist yet.

    sqlite3.Row raises IndexError (not KeyError) for an unknown column, and the
    dashboard can run against a database whose migrations have not been applied —
    it is a separate process from the bot, and storage/db.py already carries
    defensive re-creates for exactly this first-run case. Newly added columns must
    therefore degrade to `default` rather than 500 the whole page.
    """
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _bet_type_label(raw: str | None) -> str:
    return {
        "h2h":    "Moneyline",
        "totals": "Over/Under",
        "spread": "ATS",
    }.get((raw or "h2h").lower(), (raw or "h2h").upper())


def _short_sport(key: str) -> str:
    return {
        "americanfootball_nfl": "NFL",
        "americanfootball_ncaaf": "NCAAF",
        "basketball_nba": "NBA",
        "basketball_ncaab": "NCAAB",
        "baseball_mlb": "MLB",
        "icehockey_nhl": "NHL",
        "soccer_usa_mls": "MLS",
        "soccer_epl": "EPL",
        "soccer_spain_la_liga": "La Liga",
        "soccer_italy_serie_a": "Serie A",
        "soccer_france_ligue_one": "Ligue 1",
        "soccer_uefa_champs_league": "UCL",
    }.get(key, key.upper())


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)  # all DB timestamps are naive UTC
        dt = dt.astimezone(_PT)
        h = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{dt.strftime('%b')} {dt.day}  {h}:{dt.strftime('%M')} {ampm} PT"
    except ValueError:
        return iso[:16]


MIN_CALIBRATION_SAMPLE = 20  # below this, win-rate/Brier numbers are noise, not signal


def _calibration_bucket_stats(rows: list[dict]) -> dict:
    """
    Aggregate a set of settled (non-void) positions into calibration stats:
    win rate vs. average predicted probability, and Brier score.
    predicted_prob (0-1) = market_price + edge, i.e. the consensus probability
    that qualified the bet in the first place.
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": None,
                "avg_predicted": None, "diff": None, "brier": None,
                "sample_ok": False}
    wins = sum(1 for r in rows if r["won"])
    win_rate = round(wins / n * 100, 1)
    avg_predicted = round(sum(r["predicted"] for r in rows) / n * 100, 1)
    brier = round(sum((r["predicted"] - (1.0 if r["won"] else 0.0)) ** 2 for r in rows) / n, 4)
    return {
        "n": n, "wins": wins, "losses": n - wins,
        "win_rate": win_rate, "avg_predicted": avg_predicted,
        "diff": round(win_rate - avg_predicted, 1),
        "brier": brier, "sample_ok": n >= MIN_CALIBRATION_SAMPLE,
    }


def _book_count_bucket(count: int | None) -> str:
    if count is None:
        return "?"
    if count <= 2:
        return "2"
    if count == 3:
        return "3"
    if count <= 5:
        return "4-5"
    return "6+"


def _calibration_data(positions: list) -> dict:
    """
    Predicted-vs-actual calibration check on placed bets only (v1 scope — no
    outcome data exists for opportunities that were filtered out and never bet).
    Void settlements are excluded: a market cancellation isn't a probability
    outcome, so it carries no calibration signal either way.
    """
    settled: list[dict] = []
    voids = 0
    for p in positions:
        if p["status"] != "closed" or p["pnl"] is None:
            continue
        if p["pnl"] == 0.0:
            voids += 1
            continue
        if p["edge"] is None:
            continue
        settled.append({
            "won": p["pnl"] > 0,
            "predicted": p["market_price"] + p["edge"],
            "bet_type": p["bet_type"] if "bet_type" in p.keys() else "h2h",
            "bookmaker_count": p["bookmaker_count"] if "bookmaker_count" in p.keys() else None,
        })

    overall = _calibration_bucket_stats(settled)
    overall["voids"] = voids

    by_bet_type: dict[str, list[dict]] = defaultdict(list)
    for r in settled:
        by_bet_type[_bet_type_label(r["bet_type"])].append(r)
    bet_type_rows = [
        {"label": label, **_calibration_bucket_stats(rows)}
        for label, rows in sorted(by_bet_type.items())
    ]

    by_books: dict[str, list[dict]] = defaultdict(list)
    for r in settled:
        by_books[_book_count_bucket(r["bookmaker_count"])].append(r)
    _book_order = {"2": 0, "3": 1, "4-5": 2, "6+": 3, "?": 4}
    book_rows = [
        {"label": label, **_calibration_bucket_stats(rows)}
        for label, rows in sorted(by_books.items(), key=lambda kv: _book_order.get(kv[0], 9))
    ]

    return {
        "overall": overall,
        "by_bet_type": bet_type_rows,
        "by_book_count": book_rows,
        "min_sample": MIN_CALIBRATION_SAMPLE,
    }


def build_data() -> dict:
    db.init_db()
    # Settle any resolved markets before reading data
    try:
        auto_settle_positions(is_paper=IS_PAPER)
    except Exception:
        pass  # never crash the dashboard if Kalshi is unreachable
    positions = db.get_all_positions(is_paper=IS_PAPER)
    bankroll_history = db.get_bankroll_history()
    recent_opps = db.get_top_opportunities(limit=50)

    # ── Summary stats ────────────────────────────────────────────────────────
    total_staked = 0.0
    total_pnl = 0.0
    wins = losses = settled = open_count = failed_count = 0
    by_sport: dict[str, dict] = defaultdict(
        lambda: {"staked": 0.0, "pnl": 0.0, "wins": 0, "losses": 0, "open": 0}
    )
    by_strategy: dict[str, dict] = defaultdict(
        lambda: {"staked": 0.0, "pnl": 0.0, "wins": 0, "losses": 0, "open": 0}
    )

    for p in positions:
        if p["execution_status"] == "failed":
            failed_count += 1
            continue  # failed orders don't count toward stats
        sport = _short_sport(p["sport"])
        strategy = p["strategy"] if "strategy" in p.keys() else "value_edge"
        stake = p["stake"]
        total_staked += stake
        by_sport[sport]["staked"] += stake
        by_strategy[strategy]["staked"] += stake

        if p["status"] == "open":
            open_count += 1
            by_sport[sport]["open"] += 1
            by_strategy[strategy]["open"] += 1
        else:
            pnl = p["pnl"]
            if pnl is not None:
                total_pnl += pnl
                by_sport[sport]["pnl"] += pnl
                by_strategy[strategy]["pnl"] += pnl
                settled += 1
                if pnl >= 0:
                    wins += 1
                    by_sport[sport]["wins"] += 1
                    by_strategy[strategy]["wins"] += 1
                else:
                    losses += 1
                    by_sport[sport]["losses"] += 1
                    by_strategy[strategy]["losses"] += 1

    roi = round(total_pnl / total_staked * 100, 2) if total_staked > 0 and settled > 0 else None
    win_rate = round(wins / settled * 100, 1) if settled > 0 else None

    # ── Bankroll chart data ──────────────────────────────────────────────────
    bk_labels = [r["log_date"] for r in bankroll_history]
    bk_values = [round(r["bankroll"], 2) for r in bankroll_history]
    bk_at_risk = [round(r["total_at_risk"], 2) for r in bankroll_history]

    # ── Sport breakdown ──────────────────────────────────────────────────────
    sport_rows = []
    for sport, s in sorted(by_sport.items()):
        staked = s["staked"]
        pnl = s["pnl"]
        w, l, o = s["wins"], s["losses"], s["open"]
        total = w + l + o
        settled_s = w + l
        roi_s = round(pnl / staked * 100, 1) if staked > 0 and settled_s > 0 else None
        sport_rows.append({
            "sport": sport,
            "total": total,
            "wins": w,
            "losses": l,
            "open": o,
            "staked": round(staked, 2),
            "pnl": round(pnl, 2) if settled_s > 0 else None,
            "roi": roi_s,
        })

    # ── Strategy breakdown (value_edge vs market_making) ──────────────────────
    strategy_rows = []
    for strategy, s in sorted(by_strategy.items()):
        staked = s["staked"]
        pnl = s["pnl"]
        w, l, o = s["wins"], s["losses"], s["open"]
        settled_s = w + l
        roi_s = round(pnl / staked * 100, 1) if staked > 0 and settled_s > 0 else None
        strategy_rows.append({
            "strategy": strategy,
            "total": w + l + o,
            "wins": w,
            "losses": l,
            "open": o,
            "staked": round(staked, 2),
            "pnl": round(pnl, 2) if settled_s > 0 else None,
            "roi": roi_s,
        })

    # ── P&L chart data (cumulative, closed positions only) ───────────────────
    closed = sorted(
        [p for p in positions if p["status"] == "closed" and p["pnl"] is not None],
        key=lambda p: p["settled_at"] or "",
    )
    pnl_labels, pnl_cumulative = [], []
    running = 0.0
    for p in closed:
        running += p["pnl"]
        pnl_labels.append(_fmt_dt(p["settled_at"]))
        pnl_cumulative.append(round(running, 2))

    # ── Open positions ───────────────────────────────────────────────────────
    open_rows = []
    failed_rows = []
    for p in positions:
        if p["status"] != "open":
            continue
        if p["team_name"] == p["home_team"]:
            opponent = p["away_team"]
        elif p["team_name"] == p["away_team"]:
            opponent = p["home_team"]
        else:
            opponent = f"{p['home_team']} vs {p['away_team']}"
        bet_type = p["bet_type"] if "bet_type" in p.keys() else "h2h"
        threshold = p["threshold"] if "threshold" in p.keys() else None
        if p["execution_status"] == "failed":
            reason = None
            if "failure_reason" in p.keys():
                reason = p["failure_reason"]
            failed_rows.append({
                "id": p["id"],
                "team": p["team_name"],
                "opponent": opponent,
                "sport": _short_sport(p["sport"]),
                "bet_type": _bet_type_label(bet_type),
                "threshold": threshold,
                "game_time": _fmt_dt(p["commence_time"]),
                "stake": round(p["stake"], 2),
                "price_pct": round(p["market_price"] * 100, 0),
                "edge": round(p["edge"] * 100, 1) if p["edge"] is not None else None,
                "entered": _fmt_dt(p["entered_at"]),
                "reason": reason or "Unknown error",
            })
            continue
        stake = p["stake"]
        price = p["market_price"]
        pot_win = round(stake * (1.0 - price) / price, 2) if price > 0 else 0.0
        edge = p["edge"]
        spread = p["kalshi_spread"]
        open_rows.append({
            "id": p["id"],
            "team": p["team_name"],
            "opponent": opponent,
            "sport": _short_sport(p["sport"]),
            "bet_type": _bet_type_label(bet_type),
            "threshold": threshold,
            "game_time": _fmt_dt(p["commence_time"]),
            "stake": round(stake, 2),
            "price_pct": round(price * 100, 0),
            "potential_win": pot_win,
            "edge": round(edge * 100, 1) if edge is not None else None,
            "books": p["bookmaker_count"],
            "spread": round(spread * 100, 1) if spread is not None else None,
            "exec_status": p["execution_status"] or "—",
            "entered": _fmt_dt(p["entered_at"]),
            "strategy": p["strategy"] if "strategy" in p.keys() else "value_edge",
        })

    # ── Settled positions ────────────────────────────────────────────────────
    settled_rows = []
    for p in positions:
        if p["status"] != "closed":
            continue
        pnl_v = p["pnl"]
        bet_type_s = p["bet_type"] if "bet_type" in p.keys() else "h2h"
        edge_v = p["edge"] if "edge" in p.keys() else None
        consensus_pct = round((p["market_price"] + edge_v) * 100, 1) if edge_v is not None else None
        edge_pct = round(edge_v * 100, 1) if edge_v is not None else None
        kalshi_close_v = p["kalshi_close_price"] if "kalshi_close_price" in p.keys() else None
        consensus_close_v = p["consensus_close_prob"] if "consensus_close_prob" in p.keys() else None
        settled_rows.append({
            "id": p["id"],
            "team": p["team_name"],
            "sport": _short_sport(p["sport"]),
            "bet_type": _bet_type_label(bet_type_s),
            "stake": round(p["stake"], 2),
            "price_pct": round(p["market_price"] * 100, 0),
            "consensus_pct": consensus_pct,
            "edge_pct": edge_pct,
            "kalshi_close_pct": round(kalshi_close_v * 100, 1) if kalshi_close_v is not None else None,
            "consensus_close_pct": round(consensus_close_v * 100, 1) if consensus_close_v is not None else None,
            "pnl": round(pnl_v, 2) if pnl_v is not None else None,
            "won": pnl_v is not None and pnl_v >= 0,
            "settled": _fmt_dt(p["settled_at"]),
            "strategy": p["strategy"] if "strategy" in p.keys() else "value_edge",
        })

    # ── Recent detections ────────────────────────────────────────────────────
    opp_rows = []
    for o in recent_opps[:20]:
        opp_rows.append({
            "team": o["team_name"],
            "sport": _short_sport(o["sport"]),
            "consensus": round(o["consensus_prob"] * 100, 1),
            "price": round(o["market_price"] * 100, 1),
            "edge": round(o["edge"] * 100, 1),
            "alerted": bool(o["alerted"]),
            "detected": _fmt_dt(o["detected_at"]),
        })

    # ── API credits ──────────────────────────────────────────────────────────
    credits_row = db.get_api_credits()
    api_credits = None
    if credits_row:
        api_credits = {
            "used_this_scan": credits_row["used_this_scan"],
            "used_total":     credits_row["used_total"],
            "remaining":      credits_row["remaining"],
            "recorded_at":    _fmt_dt(credits_row["recorded_at"]),
        }

    # Compute total deposited from Kalshi's own API data (balance + open costs + fees - settled pnl)
    try:
        total_deposited = KalshiClient().fetch_total_deposited()
    except Exception:
        total_deposited = None

    calibration = _calibration_data(positions)

    return {
        "mode": "PAPER" if IS_PAPER else "LIVE",
        "summary": {
            "total_pnl": round(total_pnl, 2) if settled > 0 else None,
            "total_staked": round(total_staked, 2),
            "roi": roi,
            "win_rate": win_rate,
            "wins": wins,
            "losses": losses,
            "settled": settled,
            "open_count": open_count,
            "failed_count": failed_count,
            "total_bets": len(positions),
            "kalshi_balance": total_deposited,
        },
        "api_credits": api_credits,
        "bankroll_chart": {"labels": bk_labels, "bankroll": bk_values, "at_risk": bk_at_risk},
        "pnl_chart": {"labels": pnl_labels, "cumulative": pnl_cumulative},
        "sport_rows": sport_rows,
        "strategy_rows": strategy_rows,
        "open_rows": open_rows,
        "failed_rows": failed_rows,
        "settled_rows": settled_rows[:30],
        "opp_rows": opp_rows,
        "calibration": calibration,
    }


# ── Per-book consensus breakdown ──────────────────────────────────────────────

# Odds API key → (display name, sport_slug_map)
# sport_slug_map: Odds API sport key → sport-specific URL path
# Falls back to base URL if sport not in map.
_BOOK_INFO: dict[str, tuple[str, str, dict[str, str]]] = {
    # key: (display_name, base_url, {sport_key: sport_path})
    "draftkings": ("DraftKings", "https://sportsbook.draftkings.com", {
        "baseball_mlb":              "/leagues/baseball/mlb",
        "basketball_nba":            "/leagues/basketball/nba",
        "icehockey_nhl":             "/leagues/hockey/nhl",
        "americanfootball_nfl":      "/leagues/football/nfl",
        "soccer_usa_mls":            "/leagues/soccer/mls",
        "soccer_epl":                "/leagues/soccer/english-premier-league",
        "soccer_uefa_champs_league": "/leagues/soccer/uefa-champions-league",
    }),
    "fanduel": ("FanDuel", "https://sportsbook.fanduel.com", {
        "baseball_mlb":              "/baseball/mlb",
        "basketball_nba":            "/basketball/nba",
        "icehockey_nhl":             "/hockey/nhl",
        "americanfootball_nfl":      "/football/nfl",
        "soccer_usa_mls":            "/soccer/mls",
        "soccer_epl":                "/soccer/epl",
        "soccer_uefa_champs_league": "/soccer/champions-league",
    }),
    "betmgm":         ("BetMGM",       "https://sports.betmgm.com",                    {}),
    "caesars":        ("Caesars",      "https://www.caesars.com/sportsbook-and-casino", {}),
    "williamhill_us": ("Caesars (WH)", "https://www.caesars.com/sportsbook-and-casino", {}),
    "betrivers":      ("BetRivers",    "https://www.betrivers.com",                     {}),
    "pointsbet":      ("PointsBet",    "https://www.pointsbet.com",                     {}),
    "unibet_us":      ("Unibet",       "https://www.unibet.com/betting",                {}),
    "barstool":       ("Barstool",     "https://www.barstoolsports.com/bets",           {}),
    "mybookieag":     ("MyBookie",     "https://mybookie.ag",                           {}),
    "bovada":         ("Bovada",       "https://www.bovada.lv/sports",                  {}),
    "betonlineag":    ("BetOnline",    "https://www.betonline.ag/sportsbook",           {}),
    "lowvig":         ("LowVig",       "https://www.lowvig.ag",                         {}),
    "pinnacle":       ("Pinnacle",     "https://www.pinnacle.com/en/baseball/matchups", {}),
    "superbook":      ("SuperBook",    "https://superbook.com",                         {}),
    "wynnbet":        ("WynnBET",      "https://www.wynnbet.com",                       {}),
    "betfair":        ("Betfair",      "https://www.betfair.com",                       {}),
    "sport888":       ("888sport",     "https://www.888sport.com",                      {}),
    "betus":          ("BetUS",        "https://www.betus.com.pa",                      {}),
    "betway":         ("Betway",       "https://betway.com",                            {}),
}


def _book_url(book_key: str, sport_key: str) -> tuple[str, str]:
    """Return (display_name, url) for a book + sport combination."""
    if book_key not in _BOOK_INFO:
        return book_key, ""
    name, base, sport_map = _BOOK_INFO[book_key]
    path = sport_map.get(sport_key, "")
    return name, base + path


def _book_breakdown(bookmakers_json: str, team_name: str, bet_type: str, threshold: float | None, sport_key: str = "") -> list[dict]:
    """
    Return per-book de-vigged probability for the outcome we bet on.
    Each entry: {book, url, odds, raw_prob, devigged_prob}
    """
    market_key_map = {"h2h": "h2h", "totals": "totals", "spread": "spreads"}
    market_key = market_key_map.get(bet_type, "h2h")

    # Derive outcome_name from team_name + bet_type
    if bet_type == "totals":
        outcome_name = "Over" if team_name.lower().startswith("over") else "Under"
    elif bet_type == "spread":
        # Strip the spread value suffix (e.g. "Washington Nationals -1.5" → "Washington Nationals")
        import re
        outcome_name = re.sub(r"\s*[+-]\d+\.?\d*\s*$", "", team_name).strip()
    else:
        outcome_name = team_name  # H2H: team name

    try:
        bookmakers = json.loads(bookmakers_json)
    except (json.JSONDecodeError, TypeError):
        return []

    rows = []
    for book in bookmakers:
        book_key = book.get("key", "")
        display_name, url = _book_url(book_key, sport_key)
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            outcomes = market.get("outcomes", [])

            # For spreads/totals: fuzzy name match + exact point match.
            # For H2H (threshold is None): exact normalized match.
            if threshold is not None:
                target = next(
                    (o for o in outcomes
                     if _names_match(o.get("name", ""), outcome_name)
                     and o.get("point") is not None
                     and abs(float(o["point"]) - threshold) <= 0.01),
                    None,
                )
            else:
                norm_outcome = _norm_team(outcome_name)
                target = next(
                    (o for o in outcomes
                     if _norm_team(o.get("name", "")) == norm_outcome),
                    None,
                )

            if target is None:
                continue

            raw_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = _devig(raw_probs)
            idx = outcomes.index(target)
            pt = target.get("point")
            line = f"{outcome_name} {pt}" if pt is not None else outcome_name
            rows.append({
                "book": display_name,
                "url": url,
                "line": line,
                "odds": target["price"],
                "raw_prob": round(raw_probs[idx] * 100, 1),
                "devigged_prob": round(no_vig[idx] * 100, 1),
            })

    rows.sort(key=lambda r: r["devigged_prob"], reverse=True)
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/data")
@_requires_auth
def api_data():
    return jsonify(build_data())


@app.route("/position/<int:position_id>")
@_requires_auth
def position_detail(position_id: int):
    p = db.get_position(position_id)
    if not p:
        abort(404)

    bet_type = p["bet_type"] if "bet_type" in p.keys() else "h2h"
    threshold = p["threshold"] if "threshold" in p.keys() else None
    bj = p["bookmakers_json"] if "bookmakers_json" in p.keys() else None

    breakdown = _book_breakdown(bj, p["team_name"], bet_type or "h2h", threshold, sport_key=p["sport"]) if bj else []
    consensus = sum(r["devigged_prob"] for r in breakdown) / len(breakdown) if breakdown else None

    data = {
        "id": p["id"],
        "team": p["team_name"],
        "home": p["home_team"],
        "away": p["away_team"],
        "sport": _short_sport(p["sport"]),
        "bet_type": _bet_type_label(bet_type),
        "threshold": threshold,
        "game_time": _fmt_dt(p["commence_time"]),
        "entered": _fmt_dt(p["entered_at"]),
        "stake": round(p["stake"], 2),
        "price_pct": round(p["market_price"] * 100, 1),
        # Contracts are the unit actually traded; stake is just contracts x price.
        # Showing it makes the integer-rounding step visible instead of looking like
        # an arbitrary dollar figure.
        "contracts": (round(p["stake"] / p["market_price"])
                      if p["market_price"] else None),
        # The single largest driver of bet size: a maker_only bet is sized at the
        # fee-free mid (fee_rate=0) and can come out ~2x larger than a HIGHER-edge
        # taker bet. Without this, two bets at the same price with different sizes
        # look inexplicable. None for rows predating the column.
        "maker_only": (None if _col(p, "maker_only") is None
                       else bool(_col(p, "maker_only"))),
        "edge": round(p["edge"] * 100, 1) if p["edge"] is not None else None,
        "status": p["status"],
        "pnl": round(p["pnl"], 2) if p["pnl"] is not None else None,
        "breakdown": breakdown,
        "consensus": round(consensus, 1) if consensus is not None else None,
        "book_count": len(breakdown),
        "has_data": len(breakdown) > 0,
    }
    return render_template_string(DETAIL_TEMPLATE, p=data)


@app.route("/scan/detail/<int:entry_id>")
@_requires_auth
def scan_detail(entry_id: int):
    row = db.get_scan_entry(entry_id)
    if not row:
        abort(404)

    bet_type = row["bet_type"] or "h2h"
    threshold = row["threshold"]
    bj = row["bookmakers_json"] if "bookmakers_json" in row.keys() else None
    breakdown = _book_breakdown(bj, row["team_name"], bet_type, threshold, row["sport"]) if bj else []

    consensus = round(row["consensus_prob"] * 100, 1) if row["consensus_prob"] is not None else None
    book_count = len(breakdown)

    ticker = row["kalshi_ticker"] or ""
    data = {
        "id":          row["id"],
        "team":        row["team_name"],
        "sport":       _short_sport(row["sport"]),
        "home":        row["home_team"],
        "away":        row["away_team"],
        "bet_type":    _bet_type_label(bet_type),
        "game_time":   _fmt_dt(row["commence_time"]),
        "price_pct":   round(row["kalshi_price"] * 100, 1) if row["kalshi_price"] is not None else "—",
        "edge":        round(row["edge"] * 100, 1) if row["edge"] is not None else None,
        "status":      row["status"],
        "reason":      row["reason"] or "",
        "consensus":   consensus,
        "book_count":  book_count,
        "has_data":    len(breakdown) > 0,
        "breakdown":   breakdown,
        "scanned_at":  _fmt_dt(row["scanned_at"]),
        "kalshi_ticker": ticker,
        "kalshi_url":  _kalshi_market_url(ticker),
    }
    return render_template_string(SCAN_DETAIL_TEMPLATE, p=data)


@app.route("/scan")
@_requires_auth
def scan_results():
    rows = db.get_last_scan()
    scanned_at = _fmt_dt(rows[0]["scanned_at"]) if rows else "No scan data yet"
    last_active = _fmt_dt(db.get_bot_heartbeat()) if db.get_bot_heartbeat() else None

    entries = []
    for r in rows:
        bet_type = r["bet_type"] or "h2h"
        threshold = r["threshold"]
        matchup = f"{r['home_team']} vs {r['away_team']}"
        entries.append({
            "id":          r["id"],
            "sport":       _short_sport(r["sport"]),
            "matchup":     matchup,
            "team":        r["team_name"],
            "bet_type":    _bet_type_label(bet_type),
            "threshold":   threshold,
            "ticker":      r["kalshi_ticker"] or "",
            "price":       round(r["kalshi_price"] * 100, 1) if r["kalshi_price"] is not None else None,
            "limit_price": round(r["limit_price"] * 100, 1) if r["limit_price"] is not None else None,
            "consensus":   round(r["consensus_prob"] * 100, 1) if r["consensus_prob"] is not None else None,
            "edge":        round(r["edge"] * 100, 1) if r["edge"] is not None else None,
            "books":       r["bookmaker_count"],
            "spread":      round(r["kalshi_spread"] * 100, 1) if r["kalshi_spread"] is not None else None,
            "volume":      int(r["kalshi_volume"]) if r["kalshi_volume"] is not None else None,
            "status":      r["status"],
            "maker_only":  bool(r["maker_only"]),
            "reason":      r["reason"] or "",
            "game_time":   _fmt_dt(r["commence_time"]),
        })

    return render_template_string(SCAN_TEMPLATE, entries=entries, scanned_at=scanned_at, last_active=last_active)


_MM_REASON_HELP = {
    "ok": "Quoted — passed every gate.",
    "paper_filled": "Paper mode: the live book already crossed our intended price.",
    "insufficient_volume": f"Market has traded under {config.MM_MIN_VOLUME:.0f} contracts. "
                           "78% of wide-spread Kalshi sports markets have never traded at all.",
    "consensus_outside_spread": "Sportsbook fair value sits outside Kalshi's bid/ask. "
                                "That's a directional signal, not a spread to capture.",
    "crossed_book": "No room to quote inside the book without crossing it.",
    "below_fee_floor": "Capture wouldn't cover the maker fee on both legs.",
    "too_few_books": f"Fewer than {config.MM_MIN_BOOKMAKERS} sportsbooks — fair value not confident enough to centre on.",
    "high_disagreement": f"Sportsbooks disagree by more than {config.MM_MAX_CONSENSUS_STD:.2f} std.",
    "outside_fair_value_band": "Deep favourite or longshot — outside MM_FAIR_VALUE_BAND.",
    "spread_too_narrow": "Spread is tight enough for the directional strategy to just cross it.",
    "spread_narrowed_to_tradeable": "Spread narrowed since the last scan — directional territory now.",
    "stale_consensus_drift": "Kalshi's price moved since our last sportsbook read; quoting paused.",
    "aggregate_exposure_cap": "Total MM exposure cap reached for this tick.",
    "duplicate_ticker": "Already evaluated this ticker earlier in the tick.",
    "no_consensus": "No sportsbook consensus available.",
    "clip_zero": "Computed clip size rounded to zero.",
}


@app.route("/mm")
@_requires_auth
def mm_decisions():
    """What the market maker did on its most recent tick, and why.

    The directional strategy has had /scan since the beginning; MM had no
    equivalent, and since every rejection path in execution/market_maker.py was
    logger.debug (off in production), a tick that evaluated ~60 candidates and
    quoted 1 left no trace anywhere. This is that page."""
    rows = db.get_last_mm_tick()
    decided_at = _fmt_dt(rows[0]["decided_at"]) if rows else "No MM tick recorded yet"

    entries, counts = [], {}
    for r in rows:
        reason = r["reason"] or ""
        counts[reason] = counts.get(reason, 0) + 1
        entries.append({
            "sport":      _short_sport(r["sport"] or ""),
            "team":       r["team_name"] or "",
            "bet_type":   _bet_type_label(r["bet_type"] or "h2h"),
            "ticker":     r["kalshi_ticker"] or "",
            "url":        _kalshi_market_url(r["kalshi_ticker"] or ""),
            "book":       (f"{r['kalshi_bid']*100:.0f} / {r['kalshi_ask']*100:.0f}"
                           if r["kalshi_bid"] is not None and r["kalshi_ask"] is not None else "—"),
            "spread":     round(r["kalshi_spread"] * 100, 1) if r["kalshi_spread"] is not None else None,
            "volume":     int(r["kalshi_volume"]) if r["kalshi_volume"] is not None else None,
            "consensus":  round(r["consensus_prob"] * 100, 1) if r["consensus_prob"] is not None else None,
            "books":      r["bookmaker_count"],
            "quote":      (f"{r['yes_quote']*100:.0f} / {(1-r['no_quote'])*100:.0f}"
                           if r["yes_quote"] is not None and r["no_quote"] is not None else "—"),
            "net":        round(r["net_per_pair"] * 100, 2) if r["net_per_pair"] is not None else None,
            "contracts":  r["contracts"],
            "action":     r["action"],
            "reason":     reason,
            "help":       _MM_REASON_HELP.get(reason, ""),
        })

    quoted = sum(1 for e in entries if e["action"] in ("placed", "kept"))
    summary = sorted(counts.items(), key=lambda kv: -kv[1])

    # Paired vs unpaired open MM fills. A matched pair is near-riskless; a leg
    # that filled alone is naked directional risk the bot never chose to take.
    try:
        pairing = db.get_mm_pairing(is_paper=IS_PAPER)
    except Exception:
        pairing = []
    naked = [p for p in pairing if p["unpaired"] > 0]
    naked_total = round(sum(p["unpaired_dollars"] for p in naked), 2)
    paired_total = sum(p["paired"] for p in pairing)

    return render_template_string(
        MM_TEMPLATE, entries=entries, decided_at=decided_at,
        total=len(entries), quoted=quoted, summary=summary,
        enabled=config.ENABLE_MARKET_MAKING,
        pairing=pairing, naked=naked, naked_total=naked_total,
        paired_total=paired_total,
    )


def _clv_context() -> dict:
    """CLV (closing-line value) and TTE (time-to-event) analytics context.

    Added 2026-08-25: P&L is now tracked externally via Pikkit (synced directly to
    the Kalshi account, more accurate than anything reconstructable here), so the
    homepage's job is no longer another P&L view -- it answers the two questions
    Pikkit doesn't: how CLV breaks down by sport/bet-type/time, and whether how far
    ahead of game time a bet was placed (TTE) correlates with CLV or win rate. See
    core/clv_analytics.py for the math. Shared by index() -- moved onto the
    homepage 2026-08-25, replacing the old P&L-focused cards/charts/tables there.
    """
    raw = db.get_positions_for_clv_analytics(is_paper=IS_PAPER)
    rows = clv.compute_rows(raw)

    summary = clv.overall_summary(rows)
    by_sport = [{**g, "key": _short_sport(g["key"])} for g in clv.group_by_field(rows, "sport")]
    by_bet_type = [{**g, "key": _bet_type_label(g["key"])} for g in clv.group_by_field(rows, "bet_type")]
    tte_buckets = clv.bucket_by_tte(rows)
    weekly = clv.weekly_clv_series(rows)

    # Recent rows table, newest first (rows arrive oldest-first from the DB layer
    # so weekly_clv_series doesn't need to re-sort).
    recent = []
    for r in reversed(rows[-200:]):
        recent.append({
            "sport":        _short_sport(r.get("sport") or ""),
            "bet_type":     _bet_type_label(r.get("bet_type")),
            "team_name":    r.get("team_name") or "",
            "url":          _kalshi_market_url(r.get("market_ticker") or ""),
            "entered_at":   _fmt_dt(r.get("entered_at")),
            "tte_hours":    r["tte_hours"],
            "kalshi_clv":   r["kalshi_clv"],
            "consensus_clv": r["consensus_clv"],
            "ev_pct":       r["ev_pct"],
            "won":          r["won"],
            "pnl":          r.get("pnl"),
        })

    scatter = [{"x": r["tte_hours"], "y": r["kalshi_clv"] * 100} for r in rows
               if r["tte_hours"] is not None and r["kalshi_clv"] is not None]

    return dict(
        summary=summary, by_sport=by_sport, by_bet_type=by_bet_type,
        tte_buckets=tte_buckets, weekly=weekly, recent=recent,
        scatter_json=json.dumps(scatter),
        weekly_json=json.dumps(weekly),
        min_sample=MIN_CALIBRATION_SAMPLE,
    )


@app.route("/clv")
@_requires_auth
def clv_page():
    """Moved onto the homepage 2026-08-25 -- kept as a redirect for old links/bookmarks."""
    from flask import redirect, url_for
    return redirect(url_for("index"))


@app.route("/dk-scaled")
@_requires_auth
def dk_scaled_shadow():
    """DK-scaled player-prop estimates: what they'd have bet, and how they've
    calibrated against real settlements so far.

    Shadow mode (config.DK_SCALED_SHADOW_MODE, on by default) means these estimates
    never place real capital -- see core/value_detector.py::_record_dk_shadow() and
    the 2026-08-24 review that asked for exactly this before trusting the feature:
    "run it in shadow mode first and empirically measure its calibration... the most
    important question is how prediction error changes with distance from the
    Pinnacle anchor." This page is that measurement.
    """
    summary = db.get_dk_scaled_shadow_summary()
    rows = db.get_dk_scaled_shadow_rows(limit=200)

    entries = []
    for r in rows:
        outcome = _col(r, "actual_outcome")
        if outcome is None:
            outcome_label = "Pending"
        elif outcome >= 0.5:
            outcome_label = "Yes"
        else:
            outcome_label = "No"
        error = None
        if outcome is not None and r["scaled_prob"] is not None:
            error = round(r["scaled_prob"] - outcome, 4)
        entries.append({
            "sport":        _short_sport(r["sport"] or ""),
            "participant":  r["participant"] or "",
            "market":       (r["market_key"] or "").replace("_", " "),
            "ticker":       r["kalshi_ticker"] or "",
            "url":          _kalshi_market_url(r["kalshi_ticker"] or ""),
            "side":         (r["kalshi_side"] or "").upper(),
            "target_point": r["target_point"],
            "anchor_point": r["anchor_point"],
            "distance":     r["distance"],
            "ratio":        round(r["scaling_ratio"], 3) if r["scaling_ratio"] is not None else None,
            "scaled_prob":  round(r["scaled_prob"] * 100, 1) if r["scaled_prob"] is not None else None,
            "kalshi_price": round(r["kalshi_price"] * 100, 1) if r["kalshi_price"] is not None else None,
            "edge":         round(r["edge"] * 100, 2) if r["edge"] is not None else None,
            "would_bet":    bool(r["would_bet"]),
            "status":       r["status"] or "",
            "outcome":      outcome_label,
            "error":        error,
            "scanned_at":   _fmt_dt(r["scanned_at"]),
        })

    # Scatter data for the distance-vs-error chart: only settled rows have an error.
    scatter = [{"x": e["distance"], "y": e["error"]} for e in entries if e["error"] is not None]

    return render_template_string(
        DK_SCALED_TEMPLATE,
        shadow_mode=config.DK_SCALED_SHADOW_MODE,
        enabled=getattr(config, "ENABLE_PROP_ALTERNATE_LINES", False),
        summary=summary,
        entries=entries,
        scatter_json=json.dumps(scatter),
        min_sample=MIN_CALIBRATION_SAMPLE,
    )


@app.route("/")
@_requires_auth
def index():
    return render_template_string(HTML_TEMPLATE, **_clv_context())


# ── Shared mobile CSS snippet (injected into each template) ───────────────────

_MOBILE_TABLE_CSS = """
  /* ── Mobile: stacked card layout for all tables ── */
  @media (max-width: 700px) {
    .table-wrap { overflow-x: unset; }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr { display: block; border: 1px solid var(--border); border-radius: 8px; margin: 0 0 8px; overflow: hidden; }
    tbody td { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               padding: 7px 12px; border-bottom: 1px solid rgba(42,45,58,0.6);
               white-space: normal; word-break: break-word; font-size: 13px; }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before { content: attr(data-label); font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; flex-shrink: 0; min-width: 76px; }
    tbody td[colspan] { justify-content: center; }
    tbody td[colspan]::before { display: none; }
  }
"""

# ── Scan results template ─────────────────────────────────────────────────────

SCAN_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Last Scan — Arb Bot</title>
<style>
  :root {
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
    --muted:#64748b;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;
    --yellow:#f59e0b;--orange:#f97316;--purple:#a855f7;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  html,body{overflow-x:hidden;}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
  header a{color:var(--muted);text-decoration:none;font-size:13px;white-space:nowrap;}
  header a:hover{color:var(--text);}
  header h1{font-size:16px;font-weight:600;}
  .meta{font-size:12px;color:var(--muted);margin-left:auto;white-space:nowrap;}
  main{padding:16px;max-width:1600px;margin:0 auto;}
  .toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;}
  .search-input{background:var(--surface);border:1px solid var(--border);color:var(--text);
    padding:6px 12px;border-radius:8px;font-size:13px;width:200px;outline:none;min-width:0;}
  .search-input:focus{border-color:var(--blue);}
  .search-input::placeholder{color:var(--muted);}
  .col-select{background:var(--surface);border:1px solid var(--border);color:var(--text);
    padding:6px 10px;border-radius:8px;font-size:12px;outline:none;cursor:pointer;}
  .filters{display:flex;gap:6px;flex-wrap:wrap;}
  .filter-btn{background:var(--surface);border:1px solid var(--border);color:var(--muted);
    padding:4px 10px;border-radius:20px;cursor:pointer;font-size:11px;font-weight:600;}
  .filter-btn.active{border-color:currentColor;}
  .filter-btn[data-status="all"].active{color:var(--text);}
  .filter-btn[data-status="value"].active{color:var(--green);}
  .filter-btn[data-status="no_edge"].active{color:var(--blue);}
  .filter-btn[data-status="spread_too_wide"].active{color:var(--yellow);}
  .filter-btn[data-status="few_books"].active{color:var(--yellow);}
  .filter-btn[data-status="no_consensus"].active{color:var(--yellow);}
  .filter-btn[data-status="no_threshold"].active{color:var(--yellow);}
  .filter-btn[data-status="blocked"].active{color:var(--orange);}
  .filter-btn[data-status="kelly_no_edge"].active{color:var(--purple);}
  .filter-btn[data-status="daily_cap"].active{color:var(--red);}
  .visible-count{font-size:12px;color:var(--muted);margin-left:auto;white-space:nowrap;}
  .section{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;}
  .table-wrap{overflow-x:auto;}
  table{width:100%;border-collapse:collapse;}
  th{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;
     padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap;}
  tr:last-child td{border-bottom:none;}
  tr:hover td{background:rgba(255,255,255,0.02);}
  .badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:0.4px;}
  .s-value      {color:var(--green);background:rgba(34,197,94,0.1);}
  .s-no_edge    {color:var(--blue);background:rgba(59,130,246,0.1);}
  .s-spread_too_wide,.s-spread_too_wide_take,.s-low_volume,.s-few_books,.s-no_consensus,.s-no_threshold
                {color:var(--yellow);background:rgba(245,158,11,0.1);}
  .s-blocked    {color:var(--orange);background:rgba(249,115,22,0.1);}
  .s-kelly_no_edge{color:var(--purple);background:rgba(168,85,247,0.1);}
  .s-daily_cap  {color:var(--red);background:rgba(239,68,68,0.1);}
  .pos{color:var(--green);} .neg{color:var(--red);} .muted{color:var(--muted);}
  .empty{padding:40px;text-align:center;color:var(--muted);}
  .count-badge{font-size:11px;color:var(--muted);margin-left:4px;}

  /* ── Mobile ── */
  @media (max-width: 700px) {
    header { padding: 10px 14px; gap: 8px; }
    header h1 { font-size: 14px; }
    .meta { font-size: 11px; margin-left: 0; width: 100%; }
    main { padding: 10px; }
    .search-input { width: 100%; }
    .toolbar { gap: 6px; }
    .table-wrap { overflow-x: unset; }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr { display: block; border: 1px solid var(--border); border-radius: 8px; margin: 0 0 8px; overflow: hidden; }
    tbody td { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               padding: 7px 12px; border-bottom: 1px solid rgba(42,45,58,0.6);
               white-space: normal; word-break: break-word; }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before { content: attr(data-label); font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; flex-shrink: 0; min-width: 72px; }
    tbody td[colspan] { justify-content: center; }
    tbody td[colspan]::before { display: none; }
  }
</style>
</head>
<body>
<header>
  <a href="/">← Dashboard</a>
  <h1>Last Scan Results</h1>
  <span class="meta">Last scan: {{ scanned_at }}{% if last_active %} · Bot active: {{ last_active }}{% endif %}</span>
</header>
<main>
  <div class="toolbar">
    <input class="search-input" id="search" type="text" placeholder="Search…">
    <select class="col-select" id="col-select">
      <option value="-1">All columns</option>
      <option value="0">Sport</option>
      <option value="1">Matchup</option>
      <option value="2">Bet</option>
      <option value="3">Type</option>
      <option value="4">Game Time</option>
      <option value="5">Ask Price</option>
      <option value="6">Limit Price</option>
      <option value="7">Consensus</option>
      <option value="8">Edge</option>
      <option value="9">Books</option>
      <option value="10">Spread</option>
      <option value="11">Order</option>
      <option value="12">Status</option>
      <option value="13">Reason</option>
    </select>
    <span class="visible-count" id="visible-count"></span>
  </div>
  <div class="filters" id="filters">
    <button class="filter-btn active" data-status="all">All <span class="count-badge" id="cnt-all"></span></button>
    <button class="filter-btn" data-status="value">Value <span class="count-badge" id="cnt-value"></span></button>
    <button class="filter-btn" data-status="no_edge">No Edge <span class="count-badge" id="cnt-no_edge"></span></button>
    <button class="filter-btn" data-status="spread_too_wide">Wide <span class="count-badge" id="cnt-spread_too_wide"></span></button>
    <button class="filter-btn" data-status="few_books">Few Books <span class="count-badge" id="cnt-few_books"></span></button>
    <button class="filter-btn" data-status="no_consensus">No Consensus <span class="count-badge" id="cnt-no_consensus"></span></button>
    <button class="filter-btn" data-status="no_threshold">No Threshold <span class="count-badge" id="cnt-no_threshold"></span></button>
    <button class="filter-btn" data-status="blocked">Blocked <span class="count-badge" id="cnt-blocked"></span></button>
    <button class="filter-btn" data-status="kelly_no_edge">Kelly ✗ <span class="count-badge" id="cnt-kelly_no_edge"></span></button>
    <button class="filter-btn" data-status="daily_cap">Daily Cap <span class="count-badge" id="cnt-daily_cap"></span></button>
  </div>

  <div class="section">
    <div class="table-wrap">
      <table id="scan-table">
        <thead><tr>
          <th>Sport</th><th>Matchup</th><th>Bet</th><th>Type</th>
          <th>Game Time</th><th>Ask Price</th><th>Limit Price</th><th>Consensus</th>
          <th>Edge</th><th>Books</th><th>Spread</th><th>Order</th><th>Status</th><th>Reason</th>
        </tr></thead>
        <tbody id="scan-body">
          {% if not entries %}
          <tr><td colspan="14" class="empty">No scan data yet — run the bot first.</td></tr>
          {% else %}
          {% for r in entries %}
          <tr data-status="{{ r.status }}">
            <td data-label="Sport">{{ r.sport }}</td>
            <td data-label="Matchup" style="color:var(--muted)">{{ r.matchup }}</td>
            <td data-label="Bet"><a href="/scan/detail/{{ r.id }}" style="color:var(--text);text-decoration:none"><strong>{{ r.team }}</strong> <span style="font-size:10px;color:var(--blue)">↗</span></a></td>
            <td data-label="Type"><span style="color:var(--blue)">{{ r.bet_type }}</span></td>
            <td data-label="Game Time" style="color:var(--muted)">{{ r.game_time }}</td>
            <td data-label="Ask Price">{% if r.price is not none %}{{ r.price }}¢{% else %}<span class="muted">—</span>{% endif %}</td>
            <td data-label="Limit Price">{% if r.limit_price is not none %}<span style="color:var(--blue)">{{ r.limit_price }}¢</span>{% else %}<span class="muted">—</span>{% endif %}</td>
            <td data-label="Consensus">{% if r.consensus is not none %}<strong>{{ r.consensus }}%</strong>{% else %}<span class="muted">—</span>{% endif %}</td>
            <td data-label="Edge">
              {% if r.edge is not none %}
                <span class="{{ 'pos' if r.edge >= 4 else 'muted' }}"><strong>{{ r.edge }}%</strong></span>
              {% else %}<span class="muted">—</span>{% endif %}
            </td>
            <td data-label="Books">{% if r.books is not none %}{{ r.books }}{% else %}<span class="muted">—</span>{% endif %}</td>
            <td data-label="Spread">{% if r.spread is not none %}{{ r.spread }}¢{% else %}<span class="muted">—</span>{% endif %}</td>
            <td data-label="Order">
              {% if r.status == 'value' %}
                {% if r.maker_only %}<span class="badge" style="background:rgba(234,179,8,0.15);color:#eab308;font-size:10px">LIMIT ONLY</span>
                {% else %}<span class="badge" style="background:rgba(59,130,246,0.15);color:var(--blue);font-size:10px">LIMIT→ASK</span>{% endif %}
              {% else %}<span class="muted">—</span>{% endif %}
            </td>
            <td data-label="Status"><span class="badge s-{{ r.status }}">{{ r.status.replace('_',' ').upper() }}</span></td>
            <td data-label="Reason" style="color:var(--muted);font-size:12px">{{ r.reason }}</td>
          </tr>
          {% endfor %}
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</main>
<script>
const rows = Array.from(document.querySelectorAll('#scan-body tr[data-status]'));

// Count by status
const counts = {};
rows.forEach(r => { const s = r.dataset.status; counts[s] = (counts[s]||0)+1; });
document.getElementById('cnt-all').textContent = rows.length;
Object.entries(counts).forEach(([s, n]) => {
  const el = document.getElementById('cnt-'+s);
  if (el) el.textContent = n;
});

let activeStatus = 'all';
let searchText = '';
let searchCol = -1;

function applyFilters() {
  let visible = 0;
  rows.forEach(r => {
    const statusOk = activeStatus === 'all' || r.dataset.status === activeStatus;
    let textOk = true;
    if (searchText) {
      const cells = Array.from(r.querySelectorAll('td'));
      if (searchCol >= 0) {
        textOk = cells[searchCol] ? cells[searchCol].textContent.toLowerCase().includes(searchText) : false;
      } else {
        textOk = cells.some(c => c.textContent.toLowerCase().includes(searchText));
      }
    }
    const show = statusOk && textOk;
    r.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('visible-count').textContent =
    visible === rows.length ? `${rows.length} rows` : `${visible} of ${rows.length} rows`;
}

// Status filter buttons
document.getElementById('filters').addEventListener('click', e => {
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  activeStatus = btn.dataset.status;
  applyFilters();
});

// Search input
document.getElementById('search').addEventListener('input', e => {
  searchText = e.target.value.toLowerCase().trim();
  applyFilters();
});

// Column selector
document.getElementById('col-select').addEventListener('change', e => {
  searchCol = parseInt(e.target.value);
  applyFilters();
});

applyFilters();
</script>
</body>
</html>
"""


# ── Scan detail template ─────────────────────────────────────────────────────

SCAN_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scan Entry #{{ p.id }} — Arb Bot</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #64748b; --green: #22c55e;
    --red: #ef4444; --blue: #3b82f6; --yellow: #f59e0b; --orange: #f97316; --purple: #a855f7;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { overflow-x: hidden; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
  header { display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  header a { color: var(--muted); text-decoration: none; font-size: 13px; white-space: nowrap; }
  header a:hover { color: var(--text); }
  header h1 { font-size: 16px; font-weight: 600; }
  main { padding: 20px; max-width: 900px; margin: 0 auto; }
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px; overflow: hidden; }
  .section-header { padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .section-header h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .section-header p { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0; }
  .cell { padding: 14px 16px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  .cell:last-child { border-right: none; }
  .cell-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 5px; }
  .cell-value { font-size: 16px; font-weight: 600; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  table { width: 100%; border-collapse: collapse; }
  th { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; padding: 10px 16px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 10px 16px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .bar-wrap { display: flex; align-items: center; gap: 8px; }
  .bar-bg { flex: 1; height: 6px; background: var(--border); border-radius: 3px; max-width: 120px; }
  .bar-fill { height: 6px; border-radius: 3px; background: var(--blue); }
  .consensus-row td { font-weight: 700; background: rgba(59,130,246,0.06); border-top: 2px solid var(--blue); }
  .no-data { padding: 32px; text-align: center; color: var(--muted); font-size: 13px; }
  .badge { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; }
  .s-value       { color: var(--green);  background: rgba(34,197,94,0.1); }
  .s-no_edge     { color: var(--blue);   background: rgba(59,130,246,0.1); }
  .s-blocked     { color: var(--orange); background: rgba(249,115,22,0.1); }
  .s-kelly_no_edge { color: var(--purple); background: rgba(168,85,247,0.1); }
  .s-daily_cap   { color: var(--red);    background: rgba(239,68,68,0.1); }
  .s-spread_too_wide,.s-spread_too_wide_take,.s-few_books,.s-no_consensus,.s-no_threshold,.s-low_volume
                 { color: var(--yellow); background: rgba(245,158,11,0.1); }

  /* ── Mobile ── */
  @media (max-width: 700px) {
    header { padding: 10px 14px; }
    header h1 { font-size: 14px; }
    main { padding: 12px; }
    .cell { padding: 10px 12px; }
    .cell-value { font-size: 14px; }
    .table-wrap { overflow-x: unset; }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr { display: block; border: 1px solid var(--border); border-radius: 8px; margin: 0 0 8px; overflow: hidden; }
    tbody td { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               padding: 7px 12px; border-bottom: 1px solid rgba(42,45,58,0.6);
               white-space: normal; word-break: break-word; }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before { content: attr(data-label); font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; flex-shrink: 0; min-width: 76px; }
    .bar-bg { max-width: 60px; }
    .consensus-row td { flex-wrap: wrap; }
  }
</style>
</head>
<body>
<header>
  <a href="/scan">← Last Scan</a>
  <h1>{{ p.team }} — {{ p.sport }}</h1>
  <span class="badge s-{{ p.status }}">{{ p.status.replace('_',' ').upper() }}</span>
</header>
<main>

  <div class="section">
    <div class="section-header"><h2>Bet Summary</h2></div>
    <div class="grid">
      <div class="cell"><div class="cell-label">Bet On</div><div class="cell-value">{{ p.team }}</div></div>
      <div class="cell"><div class="cell-label">Matchup</div><div class="cell-value" style="font-size:13px">{{ p.home }} vs {{ p.away }}</div></div>
      <div class="cell"><div class="cell-label">Type</div><div class="cell-value" style="color:var(--blue)">{{ p.bet_type }}</div></div>
      <div class="cell"><div class="cell-label">Game Time</div><div class="cell-value" style="font-size:13px">{{ p.game_time }}</div></div>
      <div class="cell"><div class="cell-label">Kalshi Price</div><div class="cell-value">{{ p.price_pct }}¢</div></div>
      {% if p.consensus is not none %}
      <div class="cell"><div class="cell-label">Consensus</div><div class="cell-value pos">{{ p.consensus }}%</div></div>
      {% endif %}
      {% if p.edge is not none %}
      <div class="cell"><div class="cell-label">Edge</div><div class="cell-value {{ 'pos' if p.edge >= 0 else 'neg' }}">{{ '+' if p.edge >= 0 else '' }}{{ p.edge }}%</div></div>
      {% endif %}
      <div class="cell"><div class="cell-label">Scanned At</div><div class="cell-value" style="font-size:13px">{{ p.scanned_at }}</div></div>
    </div>
    <div style="padding:12px 16px;border-top:1px solid var(--border);display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
      {% if p.kalshi_url %}
      <a href="{{ p.kalshi_url }}" target="_blank" rel="noopener"
         style="font-size:13px;color:var(--blue);text-decoration:none;">
        View on Kalshi ↗
        {% if p.kalshi_ticker %}<span style="font-size:11px;color:var(--muted);margin-left:6px">{{ p.kalshi_ticker }}</span>{% endif %}
      </a>
      {% endif %}
      {% if p.reason %}
      <span style="font-size:12px;color:var(--muted)"><strong>Reason not placed:</strong> {{ p.reason }}</span>
      {% endif %}
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      <h2>Sportsbook Consensus Breakdown{% if p.book_count %} — {{ p.book_count }} books{% endif %}</h2>
      <p>Odds captured at scan time. Click a book name to verify on their site.</p>
    </div>
    {% if p.has_data %}
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Sportsbook</th>
        <th>Line</th>
        <th>Odds</th>
        <th>Raw Implied %</th>
        <th>De-vigged %</th>
        <th></th>
      </tr></thead>
      <tbody>
        {% for r in p.breakdown %}
        <tr>
          <td data-label="Book">
            {% if r.url %}
            <a href="{{ r.url }}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">
              <strong>{{ r.book }}</strong>
              <span style="font-size:10px;color:var(--blue);margin-left:4px">↗</span>
            </a>
            {% else %}
            <strong>{{ r.book }}</strong>
            {% endif %}
          </td>
          <td data-label="Line" style="color:var(--blue)">{{ r.line }}</td>
          <td data-label="Odds" style="font-family:monospace">{{ '+' if r.odds > 0 else '' }}{{ r.odds }}</td>
          <td data-label="Raw %" style="color:var(--muted)">{{ r.raw_prob }}%</td>
          <td data-label="De-vigged %"><strong>{{ r.devigged_prob }}%</strong></td>
          <td>
            <div class="bar-wrap">
              <div class="bar-bg"><div class="bar-fill" style="width:{{ [r.devigged_prob, 100]|min }}%"></div></div>
            </div>
          </td>
        </tr>
        {% endfor %}
        {% if p.consensus %}
        <tr class="consensus-row">
          <td data-label="Book">Consensus (avg of {{ p.book_count }} books)</td>
          <td data-label="Line">—</td><td data-label="Odds">—</td>
          <td data-label="De-vigged %"><strong style="color:var(--green)">{{ p.consensus }}%</strong></td>
          <td></td>
        </tr>
        {% endif %}
      </tbody>
    </table>
    </div>
    {% else %}
    <div class="no-data">
      No sportsbook data for this entry.<br>
      <small>Only entries from scans after this feature was added will have breakdown data.</small>
    </div>
    {% endif %}
  </div>

</main>
</body>
</html>
"""


# ── Detail page template ──────────────────────────────────────────────────────

DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Position #{{ p.id }} — Arb Bot</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --muted: #64748b; --green: #22c55e;
    --red: #ef4444; --blue: #3b82f6; --yellow: #f59e0b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { overflow-x: hidden; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
  header { display: flex; align-items: center; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  header a { color: var(--muted); text-decoration: none; font-size: 13px; white-space: nowrap; }
  header a:hover { color: var(--text); }
  header h1 { font-size: 16px; font-weight: 600; }
  main { padding: 20px; max-width: 900px; margin: 0 auto; }
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px; overflow: hidden; }
  .section-header { padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .section-header h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 0; }
  .cell { padding: 14px 16px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
  .cell:last-child { border-right: none; }
  .cell-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 5px; }
  .cell-value { font-size: 16px; font-weight: 600; }
  .pos { color: var(--green); } .neg { color: var(--red); } .neutral { color: var(--text); }
  table { width: 100%; border-collapse: collapse; }
  th { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; padding: 10px 16px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 10px 16px; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .bar-wrap { display: flex; align-items: center; gap: 8px; }
  .bar-bg { flex: 1; height: 6px; background: var(--border); border-radius: 3px; max-width: 120px; }
  .bar-fill { height: 6px; border-radius: 3px; background: var(--blue); }
  .consensus-row td { font-weight: 700; background: rgba(59,130,246,0.06); border-top: 2px solid var(--blue); }
  .no-data { padding: 32px; text-align: center; color: var(--muted); font-size: 13px; }
  .tag { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; }
  .tag-open { background: #1c1a07; color: var(--yellow); }
  .tag-closed { background: #1a1d27; color: var(--muted); }

  /* ── Mobile ── */
  @media (max-width: 700px) {
    header { padding: 10px 14px; }
    header h1 { font-size: 14px; }
    main { padding: 12px; }
    .cell { padding: 10px 12px; }
    .cell-value { font-size: 14px; }
    .table-wrap { overflow-x: unset; }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr { display: block; border: 1px solid var(--border); border-radius: 8px; margin: 0 0 8px; overflow: hidden; }
    tbody td { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               padding: 7px 12px; border-bottom: 1px solid rgba(42,45,58,0.6);
               white-space: normal; word-break: break-word; }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before { content: attr(data-label); font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; flex-shrink: 0; min-width: 76px; }
    .bar-bg { max-width: 60px; }
  }
</style>
</head>
<body>
<header>
  <a href="/">← Dashboard</a>
  <h1>Position #{{ p.id }} — {{ p.team }} ({{ p.sport }})</h1>
  <span class="tag {{ 'tag-open' if p.status == 'open' else 'tag-closed' }}">{{ p.status.upper() }}</span>
</header>
<main>

  <!-- Summary -->
  <div class="section">
    <div class="section-header"><h2>Bet Summary</h2></div>
    <div class="grid">
      <div class="cell"><div class="cell-label">Bet On</div><div class="cell-value">{{ p.team }}</div></div>
      <div class="cell"><div class="cell-label">Matchup</div><div class="cell-value" style="font-size:13px">{{ p.home }} vs {{ p.away }}</div></div>
      <div class="cell"><div class="cell-label">Type</div><div class="cell-value" style="color:var(--blue)">{{ p.bet_type }}</div></div>
      <div class="cell"><div class="cell-label">Game Time</div><div class="cell-value" style="font-size:13px">{{ p.game_time }}</div></div>
      <div class="cell"><div class="cell-label">Placed At</div><div class="cell-value" style="font-size:13px">{{ p.entered }}</div></div>
      <div class="cell"><div class="cell-label">Stake</div><div class="cell-value">${{ "%.2f"|format(p.stake) }}</div></div>
      <div class="cell"><div class="cell-label">Entry Price</div><div class="cell-value">{{ p.price_pct }}¢</div></div>
      {% if p.contracts is not none %}
      <div class="cell"><div class="cell-label">Contracts</div><div class="cell-value">{{ p.contracts }}</div></div>
      {% endif %}
      {% if p.maker_only is not none %}
      <div class="cell"><div class="cell-label">Sized As</div><div class="cell-value" style="color:{{ 'var(--blue)' if p.maker_only else 'var(--muted)' }}">{{ 'maker-only (mid, 0% fee)' if p.maker_only else 'taker (ask + fee)' }}</div></div>
      {% endif %}
      {% if p.edge is not none %}
      <div class="cell"><div class="cell-label">Edge at Entry</div><div class="cell-value pos">+{{ p.edge }}%</div></div>
      {% endif %}
      {% if p.pnl is not none %}
      <div class="cell"><div class="cell-label">P&L</div><div class="cell-value {{ 'pos' if p.pnl >= 0 else 'neg' }}">{{ '+' if p.pnl >= 0 else '' }}${{ "%.2f"|format(p.pnl) }}</div></div>
      {% endif %}
    </div>
  </div>

  <!-- Per-book consensus breakdown -->
  <div class="section">
    <div class="section-header">
      <h2>Sportsbook Consensus Breakdown{% if p.book_count %} — {{ p.book_count }} books{% endif %}</h2>
      <p style="font-size:11px;color:var(--muted);margin-top:4px">Odds captured at bet entry time. Click a book name to verify on their site. Some books post lines 2–3 days in advance — if you can't find the line, check back closer to game time.</p>
    </div>
    {% if p.has_data %}
    <div class="table-wrap">
    <table>
      <thead><tr>
        <th>Sportsbook</th>
        <th>Line</th>
        <th>Odds</th>
        <th>Raw Implied %</th>
        <th>De-vigged %</th>
        <th></th>
      </tr></thead>
      <tbody>
        {% for r in p.breakdown %}
        <tr>
          <td data-label="Book">
            {% if r.url %}
            <a href="{{ r.url }}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">
              <strong>{{ r.book }}</strong>
              <span style="font-size:10px;color:var(--blue);margin-left:4px">↗</span>
            </a>
            {% else %}
            <strong>{{ r.book }}</strong>
            {% endif %}
          </td>
          <td data-label="Line" style="color:var(--blue)">{{ r.line }}</td>
          <td data-label="Odds" style="font-family:monospace">{{ '+' if r.odds > 0 else '' }}{{ r.odds }}</td>
          <td data-label="Raw %" style="color:var(--muted)">{{ r.raw_prob }}%</td>
          <td data-label="De-vigged %"><strong>{{ r.devigged_prob }}%</strong></td>
          <td>
            <div class="bar-wrap">
              <div class="bar-bg"><div class="bar-fill" style="width:{{ [r.devigged_prob, 100]|min }}%"></div></div>
            </div>
          </td>
        </tr>
        {% endfor %}
        {% if p.consensus %}
        <tr class="consensus-row">
          <td data-label="Book">Consensus (avg of {{ p.book_count }} books)</td>
          <td data-label="Line">—</td>
          <td data-label="Odds">—</td>
          <td data-label="De-vigged %"><strong style="color:var(--green)">{{ p.consensus }}%</strong></td>
          <td></td>
        </tr>
        {% endif %}
      </tbody>
    </table>
    </div>
    {% else %}
    <div class="no-data">
      No sportsbook data stored for this position.<br>
      <small>Positions logged before this feature was added won't have breakdown data.</small>
    </div>
    {% endif %}
  </div>

</main>
</body>
</html>
"""


# ── HTML + JS template ────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Arb Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #64748b;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --yellow: #f59e0b;
    --purple: #a855f7;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { overflow-x: hidden; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid var(--border); flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 17px; font-weight: 600; letter-spacing: 0.5px; }
  .header-right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .mode-badge { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 12px; letter-spacing: 1px; white-space: nowrap; }
  .mode-live { background: #052e16; color: var(--green); border: 1px solid var(--green); }
  .mode-paper { background: #1c1407; color: var(--yellow); border: 1px solid var(--yellow); }
  .refresh-info { font-size: 12px; color: var(--muted); white-space: nowrap; }

  main { padding: 16px 20px; max-width: 1400px; margin: 0 auto; }

  /* Summary cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .card-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; }
  .card-sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neutral { color: var(--text); }

  /* Charts */
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }
  @media (max-width: 700px) { .charts { grid-template-columns: 1fr; } }
  .chart-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .chart-box h2 { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 12px; }
  .chart-box canvas { max-height: 200px; }
  .chart-box.wide { grid-column: 1 / -1; }

  /* CLV & TTE (2026-08-25) */
  .section-title { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase;
                   letter-spacing: 0.6px; margin: 20px 0 10px; }
  .why { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
         padding: 12px 16px; margin-bottom: 16px; }
  .why p { font-size: 12px; color: var(--muted); }
  .num { font-variant-numeric: tabular-nums; }
  .tag-void { background: rgba(100,116,139,0.15); color: var(--muted); }

  /* Tables */
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 14px; overflow: hidden; }
  .section-header { padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; user-select: none; }
  .section-header h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .section-header .count { font-size: 12px; color: var(--muted); }
  .section-header .chevron { font-size: 11px; color: var(--muted); margin-left: 8px; flex-shrink: 0; transition: transform 0.2s; }
  .section.collapsed .table-wrap, .section.collapsed .cards { display: none; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .empty-state { padding: 28px; text-align: center; color: var(--muted); font-size: 13px; }
  .tag { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; letter-spacing: 0.5px; }
  .tag-win { background: #052e16; color: var(--green); }
  .tag-loss { background: #2d0a0a; color: var(--red); }
  .tag-open { background: #1c1a07; color: var(--yellow); }
  .tag-submitted { background: #051b2c; color: var(--blue); }
  .tag-paper { background: #1c1407; color: var(--yellow); }
  .tag-yes { background: #14103a; color: var(--purple); }

  /* ── Mobile: stacked card layout ── */
  @media (max-width: 700px) {
    body { font-size: 13px; }
    header { padding: 10px 14px; flex-direction: column; align-items: flex-start; gap: 6px; }
    header h1 { font-size: 15px; }
    .header-right { width: 100%; }
    .refresh-info { font-size: 11px; }
    main { padding: 10px 12px; }
    .cards { grid-template-columns: repeat(2, 1fr); gap: 8px; margin-bottom: 14px; }
    .card { padding: 10px 12px; }
    .card-value { font-size: 18px; }
    .card-sub { font-size: 10px; word-break: break-word; }
    .chart-box { padding: 10px; }
    .chart-box canvas { max-height: 150px; }
    .section-header { padding: 10px 12px; }
    .table-wrap { overflow-x: unset; }
    table, tbody { display: block; width: 100%; }
    thead { display: none; }
    tbody tr { display: block; border: 1px solid var(--border); border-radius: 8px; margin: 0 0 8px; overflow: hidden; }
    tbody td { display: flex; align-items: center; justify-content: space-between; gap: 8px;
               padding: 7px 12px; border-bottom: 1px solid rgba(42,45,58,0.6);
               white-space: normal; word-break: break-word; font-size: 13px; }
    tbody td:last-child { border-bottom: none; }
    tbody td[data-label]::before { content: attr(data-label); font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 600; flex-shrink: 0; min-width: 76px; }
    tbody td.no-label::before { display: none; }
    tbody td[colspan] { justify-content: center; }
    tbody td[colspan]::before { display: none; }
    .empty-state { white-space: normal; }
  }
</style>
</head>
<body>

<header>
  <h1>Kalshi Arb Bot</h1>
  <div class="header-right">
    <a href="/scan" style="font-size:12px;color:var(--blue);text-decoration:none;padding:4px 10px;border:1px solid var(--blue);border-radius:6px;white-space:nowrap;">Last Scan</a>
    <a href="/mm" style="font-size:12px;color:var(--blue);text-decoration:none;padding:4px 10px;border:1px solid var(--blue);border-radius:6px;white-space:nowrap;">Market Making</a>
    <a href="/dk-scaled" style="font-size:12px;color:var(--blue);text-decoration:none;padding:4px 10px;border:1px solid var(--blue);border-radius:6px;white-space:nowrap;">DK-Scaled Shadow</a>
    <span id="mode-badge" class="mode-badge">—</span>
    <span class="refresh-info" id="credits-badge">Credits: —</span>
    <span class="refresh-info" id="last-updated">Loading…</span>
  </div>
</header>

<main>
  <!-- CLV & TTE (2026-08-25) — replaces the old P&L cards/charts/tables. P&L is
       tracked externally via Pikkit now; this answers what Pikkit doesn't: CLV
       broken down by sport/bet-type/time, and whether time-to-event at bet
       placement correlates with CLV or win rate. See core/clv_analytics.py. -->
  <div class="why">
    <p>P&amp;L is tracked externally via Pikkit now, synced directly to the Kalshi
       account. This tracks what Pikkit only shows daily/weekly and doesn't break
       out by sport or bet type: <strong style="color:var(--text)">closing-line
       value</strong> over time, and whether <strong style="color:var(--text)">time-to-event</strong>
       at bet placement correlates with CLV or win rate.</p>
  </div>

  {% if summary.n < min_sample %}
  <div class="why">
    <p><strong style="color:var(--text)">Only {{ summary.n }} settled bet(s) so
       far</strong> (of {{ min_sample }} wanted for the numbers below to mean much).
       History was reset 2026-08-25 to start clean from current strategy code —
       this builds up as new bets settle.</p>
  </div>
  {% endif %}

  <!-- CLV summary cards -->
  <div class="cards">
    <div class="card"><div class="card-label">Settled Bets</div><div class="card-value">{{ summary.n }}</div></div>
    <div class="card"><div class="card-label">Win Rate</div>
      <div class="card-value">{% if summary.win_rate is not none %}<span class="{{ 'pos' if summary.win_rate >= 50 else 'neg' }}">{{ '%.1f'|format(summary.win_rate) }}%</span>{% else %}<span class="neutral">—</span>{% endif %}</div>
    </div>
    <div class="card"><div class="card-label">Mean Kalshi CLV</div>
      <div class="card-value">{% if summary.mean_kalshi_clv is not none %}<span class="{{ 'pos' if summary.mean_kalshi_clv > 0 else ('neg' if summary.mean_kalshi_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(summary.mean_kalshi_clv * 100) }}%</span>{% else %}<span class="neutral">—</span>{% endif %}</div>
      <div class="card-sub">Entry price vs. Kalshi's own closing price</div>
    </div>
    <div class="card"><div class="card-label">Mean Book CLV</div>
      <div class="card-value">{% if summary.mean_consensus_clv is not none %}<span class="{{ 'pos' if summary.mean_consensus_clv > 0 else ('neg' if summary.mean_consensus_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(summary.mean_consensus_clv * 100) }}%</span>{% else %}<span class="neutral">—</span>{% endif %}</div>
      <div class="card-sub">Entry consensus vs. sportsbook's closing consensus</div>
    </div>
    <div class="card"><div class="card-label">Positive CLV Rate</div>
      <div class="card-value">{{ '%.1f'|format(summary.pct_positive_kalshi_clv) + '%' if summary.pct_positive_kalshi_clv is not none else '—' }}</div>
      <div class="card-sub">of {{ summary.n_with_kalshi_clv }} with a captured closing line</div>
    </div>
    <div class="card"><div class="card-label">Mean EV%</div>
      <div class="card-value">{% if summary.mean_ev_pct is not none %}<span class="{{ 'pos' if summary.mean_ev_pct > 0 else ('neg' if summary.mean_ev_pct < 0 else 'neutral') }}">{{ '%+.1f'|format(summary.mean_ev_pct * 100) }}%</span>{% else %}<span class="neutral">—</span>{% endif %}</div>
      <div class="card-sub">Expected profit per dollar staked, AT ENTRY — a forecast, not an outcome</div>
    </div>
  </div>
  <div class="why">
    <p><strong style="color:var(--text)">EV%</strong> is <code>consensus_prob</code> vs.
       the price we actually paid, run through the same fee-adjusted Kelly formula used
       for sizing (<code>core/kelly_calculator.py::expected_value_pct</code>) — "for every
       dollar staked here, how much did the model expect to make, net of Kalshi's fee."
       It's computed at entry from what the model believed then, before the outcome was
       known, so it is NOT the same thing as realized P&amp;L or win rate above. The
       calibration check is comparing the two over enough settled bets: if mean EV%
       tracks realized ROI, <code>consensus_prob</code> is a trustworthy probability
       estimate; if realized ROI runs persistently below mean EV%, it's optimistic.</p>
  </div>

  <div class="section-title">Does time-to-event correlate with anything?</div>
  <div class="cards">
    <div class="card"><div class="card-label">TTE vs. Kalshi CLV</div>
      <div class="card-value">{{ '%+.3f'|format(summary.tte_vs_kalshi_clv_corr) if summary.tte_vs_kalshi_clv_corr is not none else '—' }}</div>
      <div class="card-sub">Pearson r. Positive = betting further ahead of game time
        tends toward better CLV; negative = betting closer to game time does.</div>
    </div>
    <div class="card"><div class="card-label">TTE vs. Win Rate</div>
      <div class="card-value">{{ '%+.3f'|format(summary.tte_vs_win_corr) if summary.tte_vs_win_corr is not none else '—' }}</div>
      <div class="card-sub">Same idea, against actual outcome instead of CLV.</div>
    </div>
    <div class="card"><div class="card-label">TTE vs. Book CLV</div>
      <div class="card-value">{{ '%+.3f'|format(summary.tte_vs_consensus_clv_corr) if summary.tte_vs_consensus_clv_corr is not none else '—' }}</div>
      <div class="card-sub">Whether the sharp line itself moves more with more lead time.</div>
    </div>
  </div>
  <div class="why">
    <p>r near 0 means no relationship; |r| above ~0.3 is worth a second look, but
       even then this is observational, not causal — treat it as a lead, not a
       verdict, until the sample is large.</p>
  </div>

  <!-- CLV charts -->
  <div class="charts">
    <div class="chart-box wide">
      <h2>Weekly Kalshi CLV Trend</h2>
      <canvas id="weeklyChart"></canvas>
    </div>
  </div>
  <div class="charts">
    <div class="chart-box">
      <h2>Mean Kalshi CLV by Sport</h2>
      <canvas id="sportChart"></canvas>
    </div>
    <div class="chart-box">
      <h2>Mean Kalshi CLV by Bet Type</h2>
      <canvas id="betTypeChart"></canvas>
    </div>
  </div>
  <div class="charts">
    <div class="chart-box">
      <h2>Win Rate by Time-to-Event</h2>
      <canvas id="tteWinChart"></canvas>
    </div>
    <div class="chart-box">
      <h2>Kalshi CLV vs. Time-to-Event</h2>
      <canvas id="scatterChart"></canvas>
    </div>
  </div>

  <!-- CLV by sport -->
  <div class="section">
    <div class="section-header"><h2>CLV — By Sport</h2></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Sport</th><th>Bets</th><th>Win Rate</th><th>Mean Kalshi CLV</th><th>Mean Book CLV</th><th>Mean EV%</th></tr></thead>
      <tbody>
        {% if not by_sport %}<tr><td colspan="6" class="empty-state">No settled bets yet.</td></tr>{% endif %}
        {% for g in by_sport %}
        <tr>
          <td data-label="Sport"><strong>{{ g.key }}</strong></td>
          <td data-label="Bets">{{ g.n }}</td>
          <td data-label="Win Rate">{{ '%.1f'|format(g.win_rate) + '%' if g.win_rate is not none else '—' }}</td>
          <td data-label="Kalshi CLV">{% if g.mean_kalshi_clv is not none %}<span class="{{ 'pos' if g.mean_kalshi_clv > 0 else ('neg' if g.mean_kalshi_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_kalshi_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Book CLV">{% if g.mean_consensus_clv is not none %}<span class="{{ 'pos' if g.mean_consensus_clv > 0 else ('neg' if g.mean_consensus_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_consensus_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Mean EV%">{% if g.mean_ev_pct is not none %}<span class="{{ 'pos' if g.mean_ev_pct > 0 else ('neg' if g.mean_ev_pct < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_ev_pct * 100) }}%</span>{% else %}—{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- CLV by bet type -->
  <div class="section">
    <div class="section-header"><h2>CLV — By Bet Type</h2></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Bet Type</th><th>Bets</th><th>Win Rate</th><th>Mean Kalshi CLV</th><th>Mean Book CLV</th><th>Mean EV%</th></tr></thead>
      <tbody>
        {% if not by_bet_type %}<tr><td colspan="6" class="empty-state">No settled bets yet.</td></tr>{% endif %}
        {% for g in by_bet_type %}
        <tr>
          <td data-label="Bet Type"><strong>{{ g.key }}</strong></td>
          <td data-label="Bets">{{ g.n }}</td>
          <td data-label="Win Rate">{{ '%.1f'|format(g.win_rate) + '%' if g.win_rate is not none else '—' }}</td>
          <td data-label="Kalshi CLV">{% if g.mean_kalshi_clv is not none %}<span class="{{ 'pos' if g.mean_kalshi_clv > 0 else ('neg' if g.mean_kalshi_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_kalshi_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Book CLV">{% if g.mean_consensus_clv is not none %}<span class="{{ 'pos' if g.mean_consensus_clv > 0 else ('neg' if g.mean_consensus_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_consensus_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Mean EV%">{% if g.mean_ev_pct is not none %}<span class="{{ 'pos' if g.mean_ev_pct > 0 else ('neg' if g.mean_ev_pct < 0 else 'neutral') }}">{{ '%+.1f'|format(g.mean_ev_pct * 100) }}%</span>{% else %}—{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- CLV by time-to-event -->
  <div class="section">
    <div class="section-header"><h2>CLV — By Time-to-Event</h2></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Hours Before Game</th><th>Bets</th><th>Win Rate</th><th>Mean Kalshi CLV</th><th>Mean Book CLV</th><th>Mean EV%</th></tr></thead>
      <tbody>
        {% for b in tte_buckets %}
        <tr>
          <td data-label="Hours Before"><strong>{{ b.range }}</strong></td>
          <td data-label="Bets">{{ b.n }}</td>
          <td data-label="Win Rate">{{ '%.1f'|format(b.win_rate) + '%' if b.win_rate is not none else '—' }}</td>
          <td data-label="Kalshi CLV">{% if b.mean_kalshi_clv is not none %}<span class="{{ 'pos' if b.mean_kalshi_clv > 0 else ('neg' if b.mean_kalshi_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(b.mean_kalshi_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Book CLV">{% if b.mean_consensus_clv is not none %}<span class="{{ 'pos' if b.mean_consensus_clv > 0 else ('neg' if b.mean_consensus_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(b.mean_consensus_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Mean EV%">{% if b.mean_ev_pct is not none %}<span class="{{ 'pos' if b.mean_ev_pct > 0 else ('neg' if b.mean_ev_pct < 0 else 'neutral') }}">{{ '%+.1f'|format(b.mean_ev_pct * 100) }}%</span>{% else %}—{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- Recent settled bets -->
  <div class="section">
    <div class="section-header">
      <h2>Recent Settled Bets</h2>
      <span class="count">{{ recent|length }} shown</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Sport</th><th>Type</th><th>Bet</th><th>TTE (h)</th>
        <th>Kalshi CLV</th><th>Book CLV</th><th>EV%</th><th>Outcome</th><th>Entered</th></tr></thead>
      <tbody>
        {% if not recent %}<tr><td colspan="9" class="empty-state">No settled bets yet.</td></tr>{% endif %}
        {% for r in recent %}
        <tr>
          <td data-label="Sport">{{ r.sport }}</td>
          <td data-label="Type">{{ r.bet_type }}</td>
          <td data-label="Bet"><a href="{{ r.url }}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none"><strong>{{ r.team_name }}</strong></a></td>
          <td data-label="TTE">{{ r.tte_hours if r.tte_hours is not none else '—' }}</td>
          <td data-label="Kalshi CLV">{% if r.kalshi_clv is not none %}<span class="{{ 'pos' if r.kalshi_clv > 0 else ('neg' if r.kalshi_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(r.kalshi_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Book CLV">{% if r.consensus_clv is not none %}<span class="{{ 'pos' if r.consensus_clv > 0 else ('neg' if r.consensus_clv < 0 else 'neutral') }}">{{ '%+.1f'|format(r.consensus_clv * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="EV%">{% if r.ev_pct is not none %}<span class="{{ 'pos' if r.ev_pct > 0 else ('neg' if r.ev_pct < 0 else 'neutral') }}">{{ '%+.1f'|format(r.ev_pct * 100) }}%</span>{% else %}—{% endif %}</td>
          <td data-label="Outcome">
            {% if r.won is none %}<span class="tag tag-void">Void</span>
            {% elif r.won %}<span class="tag tag-win">Won</span>
            {% else %}<span class="tag tag-loss">Lost</span>{% endif %}
          </td>
          <td data-label="Entered" style="color:var(--muted);font-size:12px">{{ r.entered_at }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table></div>
  </div>

  <!-- Calibration summary -->
  <div class="section">
    <div class="section-header">
      <h2>Calibration</h2>
      <span class="count" id="calibration-count"></span>
    </div>
    <div class="cards" id="calibration-cards"></div>
  </div>

  <!-- Calibration by bet type -->
  <div class="section">
    <div class="section-header"><h2>Calibration — By Bet Type</h2></div>
    <div class="table-wrap"><table id="calibration-bettype-table"></table></div>
  </div>

  <!-- Calibration by bookmaker count -->
  <div class="section">
    <div class="section-header"><h2>Calibration — By Bookmaker Count</h2></div>
    <div class="table-wrap"><table id="calibration-books-table"></table></div>
  </div>
</main>

<script>
function emptyRow(cols, msg) {
  return `<tr><td colspan="${cols}" class="empty-state">${msg}</td></tr>`;
}

function _calRowsHtml(rows, labelHeader) {
  if (!rows.length) return emptyRow(6, 'No settled bets yet.');
  return `<thead><tr>
    <th>${labelHeader}</th><th>N</th><th>Won</th><th>Lost</th><th>Win Rate</th><th>Avg Predicted</th><th>Diff</th>
  </tr></thead><tbody>` + rows.map(r => {
    if (!r.n) {
      return `<tr><td data-label="${labelHeader}"><strong>${r.label}</strong></td>
        <td colspan="6" style="color:var(--muted)">No settled bets</td></tr>`;
    }
    const diffClass = r.diff > 3 ? 'pos' : r.diff < -3 ? 'neg' : 'neutral';
    const sampleTag = r.sample_ok ? '' : ' <span style="color:var(--muted);font-size:11px">(low sample)</span>';
    return `<tr>
      <td data-label="${labelHeader}"><strong>${r.label}</strong></td>
      <td data-label="N">${r.n}${sampleTag}</td>
      <td data-label="Won" class="pos">${r.wins}</td>
      <td data-label="Lost" class="neg">${r.losses}</td>
      <td data-label="Win Rate">${r.win_rate.toFixed(1)}%</td>
      <td data-label="Avg Predicted">${r.avg_predicted.toFixed(1)}%</td>
      <td data-label="Diff" class="${diffClass}">${r.diff >= 0 ? '+' : ''}${r.diff.toFixed(1)}pt</td>
    </tr>`;
  }).join('') + '</tbody>';
}

function renderCalibration(cal) {
  const o = cal.overall;
  document.getElementById('calibration-count').textContent =
    o.n ? o.n + ' settled bet' + (o.n === 1 ? '' : 's') + (o.voids ? ' (' + o.voids + ' void)' : '') : '';

  const cardsEl = document.getElementById('calibration-cards');
  if (!o.n) {
    cardsEl.innerHTML = `<div class="card"><div class="card-label">Calibration</div>
      <div class="card-value neutral">—</div><div class="card-sub">No settled bets yet</div></div>`;
  } else {
    const diffClass = o.diff > 3 ? 'pos' : o.diff < -3 ? 'neg' : 'neutral';
    const sampleCard = o.sample_ok ? '' : `
      <div class="card"><div class="card-label">Sample Size</div><div class="card-value neutral">Low</div>
      <div class="card-sub">Need ${cal.min_sample}+ settled bets for signal</div></div>`;
    cardsEl.innerHTML = `
      <div class="card"><div class="card-label">Settled Bets</div><div class="card-value">${o.n}</div>
        <div class="card-sub">${o.wins}W / ${o.losses}L${o.voids ? ' · ' + o.voids + ' void' : ''}</div></div>
      <div class="card"><div class="card-label">Win Rate</div><div class="card-value">${o.win_rate.toFixed(1)}%</div>
        <div class="card-sub">vs ${o.avg_predicted.toFixed(1)}% predicted</div></div>
      <div class="card"><div class="card-label">Calibration Gap</div>
        <div class="card-value"><span class="${diffClass}">${o.diff >= 0 ? '+' : ''}${o.diff.toFixed(1)}pt</span></div>
        <div class="card-sub">actual − predicted win rate</div></div>
      <div class="card"><div class="card-label">Brier Score</div><div class="card-value">${o.brier.toFixed(4)}</div>
        <div class="card-sub">lower is better · 0 = perfect</div></div>
      ${sampleCard}
    `;
  }

  document.getElementById('calibration-bettype-table').innerHTML = _calRowsHtml(cal.by_bet_type, 'Bet Type');
  document.getElementById('calibration-books-table').innerHTML = _calRowsHtml(cal.by_book_count, 'Books');
}

async function refresh() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();
    const badge = document.getElementById('mode-badge');
    badge.textContent = d.mode;
    badge.className = 'mode-badge ' + (d.mode === 'PAPER' ? 'mode-paper' : 'mode-live');
    renderCalibration(d.calibration);
    renderCredits(d.api_credits);
    document.getElementById('last-updated').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('last-updated').textContent = 'Error fetching data';
  }
}

function renderCredits(c) {
  const el = document.getElementById('credits-badge');
  if (!c || c.remaining == null) { el.textContent = 'Credits: —'; return; }
  // Thresholds carried over from the old removed credits card: <100 remaining
  // is red (about to run dry), <500 is a caution amber, otherwise green.
  const remClass = c.remaining < 100 ? 'neg' : c.remaining < 500 ? 'neutral' : 'pos';
  const remaining = c.remaining.toLocaleString();
  const usedTotal = c.used_total != null ? c.used_total.toLocaleString() : '—';
  el.innerHTML = '<span class="' + remClass + '">' + remaining +
    '</span> credits left · ' + usedTotal + ' used this cycle';
}

refresh();
setInterval(refresh, 60000);  // auto-refresh every 60s -- calibration only; CLV/TTE
                               // below is rendered server-side once per page load.

// ── CLV & TTE charts (server-rendered data, static per page load) ─────────────
const clvWeekly = {{ weekly_json|safe }};
const clvScatterData = {{ scatter_json|safe }};
const clvBySport = {{ by_sport|tojson }};
const clvByBetType = {{ by_bet_type|tojson }};
const clvTteBuckets = {{ tte_buckets|tojson }};
(function () {
  const gridColor = 'rgba(255,255,255,0.06)';
  const textColor = '#64748b';
  const commonScales = (yLabel) => ({
    x: { grid: { color: gridColor }, ticks: { color: textColor } },
    y: { grid: { color: gridColor }, ticks: { color: textColor },
         title: yLabel ? { display: true, text: yLabel, color: textColor } : undefined },
  });

  new Chart(document.getElementById('weeklyChart'), {
    type: 'line',
    data: {
      labels: clvWeekly.map(w => w.week),
      datasets: [{
        label: 'Mean Kalshi CLV', data: clvWeekly.map(w => w.mean_kalshi_clv != null ? w.mean_kalshi_clv * 100 : null),
        borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.15)',
        tension: 0.25, spanGaps: true, fill: true,
      }],
    },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } }, scales: commonScales('CLV %') },
  });

  new Chart(document.getElementById('sportChart'), {
    type: 'bar',
    data: { labels: clvBySport.map(g => g.key),
      datasets: [{ label: 'Mean Kalshi CLV', data: clvBySport.map(g => (g.mean_kalshi_clv || 0) * 100),
        backgroundColor: clvBySport.map(g => (g.mean_kalshi_clv || 0) >= 0 ? '#22c55e' : '#ef4444') }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } }, scales: commonScales('CLV %') },
  });

  new Chart(document.getElementById('betTypeChart'), {
    type: 'bar',
    data: { labels: clvByBetType.map(g => g.key),
      datasets: [{ label: 'Mean Kalshi CLV', data: clvByBetType.map(g => (g.mean_kalshi_clv || 0) * 100),
        backgroundColor: clvByBetType.map(g => (g.mean_kalshi_clv || 0) >= 0 ? '#22c55e' : '#ef4444') }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } }, scales: commonScales('CLV %') },
  });

  new Chart(document.getElementById('tteWinChart'), {
    type: 'bar',
    data: { labels: clvTteBuckets.map(b => b.range),
      datasets: [{ label: 'Win rate %', data: clvTteBuckets.map(b => b.win_rate),
        backgroundColor: '#a855f7' }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: { x: commonScales().x,
                y: { grid: { color: gridColor }, ticks: { color: textColor },
                     title: { display: true, text: 'Win rate %', color: textColor },
                     min: 0, max: 100 } } },
  });

  new Chart(document.getElementById('scatterChart'), {
    type: 'scatter',
    data: { datasets: [{ label: 'Kalshi CLV', data: clvScatterData,
        backgroundColor: 'rgba(59,130,246,0.6)' }] },
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: 'Hours before game', color: textColor },
             grid: { color: gridColor }, ticks: { color: textColor } },
        y: { title: { display: true, text: 'Kalshi CLV %', color: textColor },
             grid: { color: gridColor }, ticks: { color: textColor } },
      } },
  });
})();

function initCollapsible() {
  // Mobile starts collapsed by default to save scroll space; desktop starts
  // expanded (unchanged prior behavior) but every section still gets a
  // click-to-collapse toggle.
  const startCollapsed = window.innerWidth <= 700;
  document.querySelectorAll('.section').forEach(section => {
    const header = section.querySelector('.section-header');
    if (!header) return;
    if (startCollapsed) section.classList.add('collapsed');
    const chevron = document.createElement('span');
    chevron.className = 'chevron';
    chevron.textContent = startCollapsed ? '▼' : '▲';
    header.appendChild(chevron);
    header.addEventListener('click', () => {
      const isNowCollapsed = section.classList.toggle('collapsed');
      chevron.textContent = isNowCollapsed ? '▼' : '▲';
    });
  });
}
initCollapsible();
</script>
</body>
</html>
"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global IS_PAPER
    parser = argparse.ArgumentParser(description="Arbitrage Bot Web Dashboard")
    parser.add_argument("--paper", action="store_true", help="Show paper-mode stats")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default 5000)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind (default 0.0.0.0 — accessible on local network)")
    args = parser.parse_args()

    IS_PAPER = args.paper
    mode = "PAPER" if IS_PAPER else "LIVE"

    # Print the local network IP so it's easy to type into a phone
    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "your-mac-ip"

    print(f"\n  Arb Bot Dashboard ({mode} mode)")
    print(f"  Local:   http://localhost:{args.port}")
    print(f"  Network: http://{local_ip}:{args.port}  ← open this on your phone")
    print(f"\n  To find your Mac's IP:  ipconfig getifaddr en0")
    print(f"  Press Ctrl+C to stop.\n")

    app.run(host=args.host, port=args.port, debug=False)




# Companion to SCAN_TEMPLATE for the market-making side. Deliberately a separate,
# self-contained page rather than a tab on /scan: the two strategies answer
# different questions ("is this mispriced enough to take?" vs "is this stable
# enough to quote inside?") and share almost no columns.


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global IS_PAPER
    parser = argparse.ArgumentParser(description="Arbitrage Bot Web Dashboard")
    parser.add_argument("--paper", action="store_true", help="Show paper-mode stats")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default 5000)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind (default 0.0.0.0 — accessible on local network)")
    args = parser.parse_args()

    IS_PAPER = args.paper
    mode = "PAPER" if IS_PAPER else "LIVE"

    # Print the local network IP so it's easy to type into a phone
    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "your-mac-ip"

    print(f"\n  Arb Bot Dashboard ({mode} mode)")
    print(f"  Local:   http://localhost:{args.port}")
    print(f"  Network: http://{local_ip}:{args.port}  ← open this on your phone")
    print(f"\n  To find your Mac's IP:  ipconfig getifaddr en0")
    print(f"  Press Ctrl+C to stop.\n")

    app.run(host=args.host, port=args.port, debug=False)




# Companion to SCAN_TEMPLATE for the market-making side. Deliberately a separate,
# self-contained page rather than a tab on /scan: the two strategies answer
# different questions ("is this mispriced enough to take?" vs "is this stable
# enough to quote inside?") and share almost no columns.
MM_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Market Making — Arb Bot</title>
<style>
  :root {
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
    --muted:#64748b;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;
    --yellow:#f59e0b;--orange:#f97316;--purple:#a855f7;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  html,body{overflow-x:hidden;}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
  header a{color:var(--muted);text-decoration:none;font-size:13px;white-space:nowrap;}
  header a:hover{color:var(--text);}
  header h1{font-size:16px;font-weight:600;}
  .meta{color:var(--muted);font-size:12px;}
  main{padding:20px;max-width:1500px;margin:0 auto;}
  .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 16px;min-width:120px;}
  .card .n{font-size:22px;font-weight:600;}
  .card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px;}
  .off{background:rgba(239,68,68,.12);border:1px solid var(--red);color:var(--red);
       border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:12px;}
  .why{background:var(--surface);border:1px solid var(--border);border-radius:8px;
       padding:12px 16px;margin-bottom:16px;}
  .why h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:8px;}
  .why ul{list-style:none;display:flex;flex-direction:column;gap:5px;}
  .why li{display:flex;gap:10px;align-items:baseline;font-size:12px;}
  .why .cnt{font-variant-numeric:tabular-nums;font-weight:600;min-width:32px;text-align:right;color:var(--text);}
  .why .txt{color:var(--muted);}
  table{width:100%;border-collapse:collapse;background:var(--surface);
        border:1px solid var(--border);border-radius:8px;overflow:hidden;}
  th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.5px;
     color:var(--muted);padding:9px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:9px 10px;border-bottom:1px solid var(--border);font-size:12px;white-space:nowrap;}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr:hover{background:rgba(255,255,255,.02);}
  .num{font-variant-numeric:tabular-nums;text-align:right;}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;
        font-weight:600;text-transform:uppercase;letter-spacing:.4px;}
  .a-placed{background:rgba(34,197,94,.15);color:var(--green);}
  .a-kept{background:rgba(59,130,246,.15);color:var(--blue);}
  .a-cancelled{background:rgba(249,115,22,.15);color:var(--orange);}
  .a-rejected{background:rgba(100,116,139,.15);color:var(--muted);}
  .reason{color:var(--muted);font-size:11px;}
  .reason strong{color:var(--text);font-weight:500;display:block;}
  .empty{text-align:center;color:var(--muted);padding:28px;}
  a.tick{color:var(--text);text-decoration:none;}
  a.tick:hover{color:var(--blue);}
  @media (max-width:820px){
    thead{display:none;}
    table,tbody,tr,td{display:block;width:100%;}
    tr{border-bottom:1px solid var(--border);padding:8px 0;}
    td{border:none;padding:4px 12px;display:flex;gap:10px;white-space:normal;}
    td::before{content:attr(data-label);font-size:10px;color:var(--muted);
               text-transform:uppercase;letter-spacing:.5px;font-weight:600;min-width:78px;}
    .num{text-align:left;}
  }
</style>
</head>
<body>
<header>
  <a href="/">← Dashboard</a>
  <a href="/clv">CLV &amp; TTE</a>
  <a href="/scan">Last Scan</a>
  <a href="/dk-scaled">DK-Scaled Shadow</a>
  <h1>Market Making</h1>
  <span class="meta">Last tick: {{ decided_at }}</span>
</header>
<main>
  {% if not enabled %}
  <div class="off">Market making is currently <strong>disabled</strong>
    (ENABLE_MARKET_MAKING=false). The rows below are from the last tick that ran.</div>
  {% endif %}

  <div class="cards">
    <div class="card"><div class="n">{{ total }}</div><div class="l">Candidates</div></div>
    <div class="card"><div class="n" style="color:var(--green)">{{ quoted }}</div><div class="l">Quoted</div></div>
    <div class="card"><div class="n" style="color:var(--muted)">{{ total - quoted }}</div><div class="l">Not quoted</div></div>
  </div>

  {% if pairing %}
  <div class="why">
    <h2>Fill pairing — is this actually market making?</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:8px;">
      A matched YES+NO pair costs under $1 and pays exactly $1, so the outcome does not
      matter. A leg that fills alone is a naked directional position, not spread capture.</p>
    <div class="cards" style="margin-bottom:0;">
      <div class="card"><div class="n" style="color:var(--green)">{{ '%.0f'|format(paired_total) }}</div>
        <div class="l">Matched pairs</div></div>
      <div class="card"><div class="n" style="color:{{ 'var(--red)' if naked_total > 0 else 'var(--muted)' }}">
        ${{ '%.2f'|format(naked_total) }}</div><div class="l">Naked exposure</div></div>
    </div>
    {% if naked %}
    <ul style="margin-top:10px;">
      {% for p in naked %}
      <li><span class="cnt">{{ '%.0f'|format(p.unpaired) }}</span><span class="txt">
        <strong style="color:var(--red)">{{ p.naked_side|upper }}</strong>
        unpaired on <strong style="color:var(--text)">{{ p.ticker }}</strong>
        — ${{ '%.2f'|format(p.unpaired_dollars) }} directional
        ({{ '%.0f'|format(p.yes_contracts) }} yes / {{ '%.0f'|format(p.no_contracts) }} no)</span></li>
      {% endfor %}
    </ul>
    {% endif %}
  </div>
  {% endif %}

  {% if summary %}
  <div class="why">
    <h2>Why</h2>
    <ul>
      {% for reason, n in summary %}
      <li><span class="cnt">{{ n }}</span><span class="txt"><strong style="color:var(--text)">{{ reason }}</strong></span></li>
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  <table>
    <thead>
      <tr>
        <th>Sport</th><th>Market</th><th>Type</th>
        <th class="num">Book</th><th class="num">Spread</th><th class="num">Volume</th>
        <th class="num">Consensus</th><th class="num">Books</th>
        <th class="num">Our quote</th><th class="num">Net/pair</th><th class="num">Size</th>
        <th>Action</th><th>Reason</th>
      </tr>
    </thead>
    <tbody>
      {% if not entries %}
      <tr><td colspan="13" class="empty">No MM tick recorded yet — enable market making and let one tick run.</td></tr>
      {% endif %}
      {% for e in entries %}
      <tr>
        <td data-label="Sport">{{ e.sport }}</td>
        <td data-label="Market"><a class="tick" href="{{ e.url }}" target="_blank" rel="noopener"><strong>{{ e.team }}</strong></a></td>
        <td data-label="Type">{{ e.bet_type }}</td>
        <td data-label="Book" class="num">{{ e.book }}</td>
        <td data-label="Spread" class="num">{% if e.spread is not none %}{{ e.spread }}¢{% else %}—{% endif %}</td>
        <td data-label="Volume" class="num">{% if e.volume is not none %}{{ '{:,}'.format(e.volume) }}{% else %}—{% endif %}</td>
        <td data-label="Consensus" class="num">{% if e.consensus is not none %}{{ e.consensus }}%{% else %}—{% endif %}</td>
        <td data-label="Books" class="num">{{ e.books if e.books is not none else '—' }}</td>
        <td data-label="Our quote" class="num">{{ e.quote }}</td>
        <td data-label="Net/pair" class="num" {% if e.net is not none and e.net > 0 %}style="color:var(--green)"{% endif %}>
          {% if e.net is not none %}{{ e.net }}¢{% else %}—{% endif %}</td>
        <td data-label="Size" class="num">{{ e.contracts if e.contracts is not none else '—' }}</td>
        <td data-label="Action"><span class="pill a-{{ e.action }}">{{ e.action }}</span></td>
        <td data-label="Reason" class="reason"><strong>{{ e.reason }}</strong>{{ e.help }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</main>
</body>
</html>
"""

# ── DK-scaled shadow-mode calibration template ────────────────────────────────

DK_SCALED_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DK-Scaled Shadow Mode — Arb Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e2e8f0;
    --muted:#64748b;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;
    --yellow:#f59e0b;--orange:#f97316;--purple:#a855f7;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  html,body{overflow-x:hidden;}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
  header a{color:var(--muted);text-decoration:none;font-size:13px;white-space:nowrap;}
  header a:hover{color:var(--text);}
  header h1{font-size:16px;font-weight:600;}
  .meta{color:var(--muted);font-size:12px;}
  main{padding:20px;max-width:1500px;margin:0 auto;}
  .cards{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 16px;min-width:120px;}
  .card .n{font-size:22px;font-weight:600;}
  .card .l{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px;}
  .off,.shadow{border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:12px;}
  .off{background:rgba(239,68,68,.12);border:1px solid var(--red);color:var(--red);}
  .shadow{background:rgba(59,130,246,.12);border:1px solid var(--blue);color:var(--blue);}
  .why{background:var(--surface);border:1px solid var(--border);border-radius:8px;
       padding:12px 16px;margin-bottom:16px;}
  .why h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:8px;}
  .why p{font-size:12px;color:var(--muted);margin-bottom:4px;}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;}
  @media (max-width:900px){.charts{grid-template-columns:1fr;}}
  .chart-box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;}
  .chart-box h2{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:10px;}
  .chart-box canvas{max-height:240px;}
  table{width:100%;border-collapse:collapse;background:var(--surface);
        border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:16px;}
  th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.5px;
     color:var(--muted);padding:9px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:9px 10px;border-bottom:1px solid var(--border);font-size:12px;white-space:nowrap;}
  tbody tr:last-child td{border-bottom:none;}
  tbody tr:hover{background:rgba(255,255,255,.02);}
  .num{font-variant-numeric:tabular-nums;text-align:right;}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;
        font-weight:600;text-transform:uppercase;letter-spacing:.4px;}
  .p-yes{background:rgba(34,197,94,.15);color:var(--green);}
  .p-no{background:rgba(239,68,68,.15);color:var(--red);}
  .p-pending{background:rgba(100,116,139,.15);color:var(--muted);}
  .p-would{background:rgba(168,85,247,.15);color:var(--purple);}
  .empty{text-align:center;color:var(--muted);padding:28px;}
  a.tick{color:var(--text);text-decoration:none;}
  a.tick:hover{color:var(--blue);}
  .pos{color:var(--green);} .neg{color:var(--red);}
  @media (max-width:820px){
    thead{display:none;}
    table,tbody,tr,td{display:block;width:100%;}
    tr{border-bottom:1px solid var(--border);padding:8px 0;}
    td{border:none;padding:4px 12px;display:flex;gap:10px;white-space:normal;}
    td::before{content:attr(data-label);font-size:10px;color:var(--muted);
               text-transform:uppercase;letter-spacing:.5px;font-weight:600;min-width:78px;}
    .num{text-align:left;}
  }
</style>
</head>
<body>
<header>
  <a href="/">← Dashboard</a>
  <a href="/clv">CLV &amp; TTE</a>
  <a href="/scan">Last Scan</a>
  <a href="/mm">Market Making</a>
  <h1>DK-Scaled Shadow Mode</h1>
</header>
<main>
  {% if not enabled %}
  <div class="off">DraftKings alternate-line data is currently <strong>not being fetched</strong>
    (ENABLE_PROP_ALTERNATE_LINES=false) — nothing new is being logged here.
    {% if summary.n %}The rows below are from before it was turned off.{% endif %}</div>
  {% elif shadow_mode %}
  <div class="shadow">Shadow mode is <strong>on</strong> (DK_SCALED_SHADOW_MODE=true):
    every estimate below is logged, but none of them place real capital, even the
    ones marked "would bet". Flip the switch only once calibration below looks
    trustworthy against a real sample of settled outcomes.</div>
  {% else %}
  <div class="off">Shadow mode is <strong>off</strong> (DK_SCALED_SHADOW_MODE=false) —
    estimates that clear the edge bar are now placed as REAL bets.</div>
  {% endif %}

  <div class="cards">
    <div class="card"><div class="n">{{ summary.n }}</div><div class="l">Opportunities</div>
      <div class="h">distinct rungs{% if summary.n_raw_rows and summary.n_raw_rows > summary.n %},
        deduped from {{ summary.n_raw_rows }} scan rows (each rung is re-evaluated
        every scan until first pitch){% endif %}</div></div>
    <div class="card"><div class="n">{{ summary.n_settled }}</div><div class="l">Settled</div>
      <div class="h">resolved against the real outcome — this is the sample that counts</div></div>
    <div class="card"><div class="n" style="color:var(--purple)">{{ summary.n_would_bet }}</div><div class="l">Would bet</div>
      <div class="h">cleared every gate; suppressed by shadow mode</div></div>
    <div class="card">
      <div class="n" style="{% if summary.brier is not none and summary.brier < 0.20 %}color:var(--green){% elif summary.brier is not none %}color:var(--red){% endif %}">
        {{ '%.4f'|format(summary.brier) if summary.brier is not none else '—' }}</div>
      <div class="l">Brier score</div>
    </div>
  </div>

  {% if summary.n_settled < min_sample %}
  <div class="why">
    <p><strong style="color:var(--text)">Not enough settled outcomes yet</strong>
       ({{ summary.n_settled }} of {{ min_sample }} needed) for the Brier score or
       calibration buckets below to mean much. Predictions accumulate as MLB games
       settle; check back once more have resolved.</p>
  </div>
  {% endif %}

  <div class="why">
    <h2>Calibration by distance from the Pinnacle anchor</h2>
    <p>The question the 2026-08-24 review said mattered most: does the estimate get
       worse the farther the target rung sits from the one point Pinnacle actually
       prices? Lower Brier and mean error near zero is good; either climbing with
       distance is the sign the flat ratio assumption is breaking down.</p>
  </div>

  <table>
    <thead>
      <tr><th>Rungs from anchor</th><th class="num">Samples</th>
          <th class="num">Brier score</th><th class="num">Mean error</th></tr>
    </thead>
    <tbody>
      {% if not summary.buckets or summary.n_settled == 0 %}
      <tr><td colspan="4" class="empty">No settled DK-scaled estimates yet.</td></tr>
      {% endif %}
      {% for b in summary.buckets %}
      <tr>
        <td data-label="Rungs">{{ b.range }}</td>
        <td data-label="Samples" class="num">{{ b.n }}</td>
        <td data-label="Brier" class="num">{{ '%.4f'|format(b.brier) if b.brier is not none else '—' }}</td>
        <td data-label="Mean error" class="num {% if b.mean_error is not none and b.mean_error > 0.03 %}neg{% elif b.mean_error is not none and b.mean_error < -0.03 %}neg{% endif %}">
          {{ '%+.4f'|format(b.mean_error) if b.mean_error is not none else '—' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <div class="charts">
    <div class="chart-box">
      <h2>Brier score by distance bucket</h2>
      <canvas id="bucketChart"></canvas>
    </div>
    <div class="chart-box">
      <h2>Error (predicted − actual) vs. distance</h2>
      <canvas id="scatterChart"></canvas>
    </div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Sport</th><th>Player</th><th>Side</th>
        <th class="num">Target</th><th class="num">Anchor</th><th class="num">Dist</th>
        <th class="num">Ratio</th><th class="num">Est. prob</th><th class="num">Kalshi</th>
        <th class="num">Edge</th><th>Would bet</th><th>Outcome</th><th>Logged</th>
      </tr>
    </thead>
    <tbody>
      {% if not entries %}
      <tr><td colspan="13" class="empty">No DK-scaled estimates logged yet — enable
        ENABLE_PROP_ALTERNATE_LINES and let a scan run.</td></tr>
      {% endif %}
      {% for e in entries %}
      <tr>
        <td data-label="Sport">{{ e.sport }}</td>
        <td data-label="Player"><a class="tick" href="{{ e.url }}" target="_blank" rel="noopener">
          <strong>{{ e.participant }}</strong></a> <span class="meta">{{ e.market }}</span></td>
        <td data-label="Side">{{ e.side }}</td>
        <td data-label="Target" class="num">{{ e.target_point }}</td>
        <td data-label="Anchor" class="num">{{ e.anchor_point }}</td>
        <td data-label="Dist" class="num">{{ e.distance }}</td>
        <td data-label="Ratio" class="num">{{ e.ratio if e.ratio is not none else '—' }}</td>
        <td data-label="Est. prob" class="num">{% if e.scaled_prob is not none %}{{ e.scaled_prob }}%{% else %}—{% endif %}</td>
        <td data-label="Kalshi" class="num">{% if e.kalshi_price is not none %}{{ e.kalshi_price }}%{% else %}—{% endif %}</td>
        <td data-label="Edge" class="num {% if e.edge is not none and e.edge > 0 %}pos{% endif %}">
          {% if e.edge is not none %}{{ e.edge }}%{% else %}—{% endif %}</td>
        <td data-label="Would bet">{% if e.would_bet %}<span class="pill p-would">Yes</span>{% else %}—{% endif %}</td>
        <td data-label="Outcome"><span class="pill p-{{ e.outcome|lower }}">{{ e.outcome }}</span></td>
        <td data-label="Logged" class="meta">{{ e.scanned_at }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</main>
<script>
  const buckets = {{ summary.buckets|tojson }};
  const scatterData = {{ scatter_json|safe }};
  const gridColor = 'rgba(255,255,255,0.06)';
  const textColor = '#64748b';

  new Chart(document.getElementById('bucketChart'), {
    type: 'bar',
    data: {
      labels: buckets.map(b => b.range),
      datasets: [{
        label: 'Brier score (lower is better)',
        data: buckets.map(b => b.brier),
        backgroundColor: '#3b82f6',
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: gridColor }, ticks: { color: textColor } },
        y: { grid: { color: gridColor }, ticks: { color: textColor }, beginAtZero: true },
      },
    },
  });

  new Chart(document.getElementById('scatterChart'), {
    type: 'scatter',
    data: {
      datasets: [{
        label: 'predicted − actual',
        data: scatterData,
        backgroundColor: 'rgba(168,85,247,0.6)',
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: 'Distance from anchor (rungs)', color: textColor },
             grid: { color: gridColor }, ticks: { color: textColor } },
        y: { title: { display: true, text: 'Error', color: textColor },
             grid: { color: gridColor }, ticks: { color: textColor } },
      },
    },
  });
</script>
</body>
</html>
"""



if __name__ == "__main__":
    main()
