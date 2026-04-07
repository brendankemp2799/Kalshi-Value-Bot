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
from flask import Flask, jsonify, render_template_string, abort
import storage.db as db
from core.odds_converter import american_to_prob, remove_vig
from execution.auto_settle import auto_settle_positions

app = Flask(__name__)
IS_PAPER = False   # set by CLI arg at startup


# ── Data helpers (same logic as dashboard.py) ────────────────────────────────

def _bet_type_label(raw: str | None) -> str:
    return {
        "h2h":    "Moneyline",
        "totals": "Over/Under",
        "spread": "ATS",
        "btts":   "BTTS",
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
        "soccer_uefa_champs_league": "UCL",
    }.get(key, key.upper())


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_PT)
        h = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{dt.strftime('%b')} {dt.day}  {h}:{dt.strftime('%M')} {ampm} PT"
    except ValueError:
        return iso[:16]


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
    wins = losses = settled = open_count = 0
    by_sport: dict[str, dict] = defaultdict(
        lambda: {"staked": 0.0, "pnl": 0.0, "wins": 0, "losses": 0, "open": 0}
    )

    for p in positions:
        sport = _short_sport(p["sport"])
        stake = p["stake"]
        total_staked += stake
        by_sport[sport]["staked"] += stake

        if p["status"] == "open":
            open_count += 1
            by_sport[sport]["open"] += 1
        else:
            pnl = p["pnl"]
            if pnl is not None:
                total_pnl += pnl
                by_sport[sport]["pnl"] += pnl
                settled += 1
                if pnl >= 0:
                    wins += 1
                    by_sport[sport]["wins"] += 1
                else:
                    losses += 1
                    by_sport[sport]["losses"] += 1

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
    for p in positions:
        if p["status"] != "open":
            continue
        stake = p["stake"]
        price = p["market_price"]
        pot_win = round(stake * (1.0 - price) / price, 2) if price > 0 else 0.0
        edge = p["edge"]
        spread = p["kalshi_spread"]
        # Determine opponent (the team that isn't the one we bet on)
        if p["team_name"] == p["home_team"]:
            opponent = p["away_team"]
        elif p["team_name"] == p["away_team"]:
            opponent = p["home_team"]
        else:
            # Draw bet — show both teams
            opponent = f"{p['home_team']} vs {p['away_team']}"
        bet_type = p["bet_type"] if "bet_type" in p.keys() else "h2h"
        threshold = p["threshold"] if "threshold" in p.keys() else None
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
        })

    # ── Settled positions ────────────────────────────────────────────────────
    settled_rows = []
    for p in positions:
        if p["status"] != "closed":
            continue
        pnl_v = p["pnl"]
        bet_type_s = p["bet_type"] if "bet_type" in p.keys() else "h2h"
        settled_rows.append({
            "id": p["id"],
            "team": p["team_name"],
            "sport": _short_sport(p["sport"]),
            "bet_type": _bet_type_label(bet_type_s),
            "stake": round(p["stake"], 2),
            "price_pct": round(p["market_price"] * 100, 0),
            "pnl": round(pnl_v, 2) if pnl_v is not None else None,
            "won": pnl_v is not None and pnl_v >= 0,
            "settled": _fmt_dt(p["settled_at"]),
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
            "total_bets": len(positions),
        },
        "bankroll_chart": {"labels": bk_labels, "bankroll": bk_values, "at_risk": bk_at_risk},
        "pnl_chart": {"labels": pnl_labels, "cumulative": pnl_cumulative},
        "sport_rows": sport_rows,
        "open_rows": open_rows,
        "settled_rows": settled_rows[:30],
        "opp_rows": opp_rows,
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
    market_key_map = {"h2h": "h2h", "totals": "totals", "spread": "spreads", "btts": "btts"}
    market_key = market_key_map.get(bet_type, "h2h")

    # Derive outcome_name from team_name + bet_type
    if bet_type == "totals":
        outcome_name = "Over" if team_name.lower().startswith("over") else "Under"
    elif bet_type == "btts":
        outcome_name = "Yes"
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

            # For totals/spreads, filter by point
            if threshold is not None:
                target = next(
                    (o for o in outcomes
                     if o.get("name") == outcome_name
                     and o.get("point") is not None
                     and abs(float(o["point"]) - threshold) <= 0.25),
                    None,
                )
            else:
                target = next((o for o in outcomes if o.get("name") == outcome_name), None)

            if target is None:
                continue

            raw_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = remove_vig(raw_probs)
            idx = outcomes.index(target)
            rows.append({
                "book": display_name,
                "url": url,
                "odds": target["price"],
                "raw_prob": round(raw_probs[idx] * 100, 1),
                "devigged_prob": round(no_vig[idx] * 100, 1),
            })

    rows.sort(key=lambda r: r["devigged_prob"], reverse=True)
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    return jsonify(build_data())


@app.route("/position/<int:position_id>")
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
        "edge": round(p["edge"] * 100, 1) if p["edge"] is not None else None,
        "status": p["status"],
        "pnl": round(p["pnl"], 2) if p["pnl"] is not None else None,
        "breakdown": breakdown,
        "consensus": round(consensus, 1) if consensus is not None else None,
        "book_count": len(breakdown),
        "has_data": len(breakdown) > 0,
    }
    return render_template_string(DETAIL_TEMPLATE, p=data)


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


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
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }
  header { display: flex; align-items: center; gap: 16px; padding: 16px 24px; border-bottom: 1px solid var(--border); }
  header a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header a:hover { color: var(--text); }
  header h1 { font-size: 17px; font-weight: 600; }
  main { padding: 24px; max-width: 900px; margin: 0 auto; }
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px; overflow: hidden; }
  .section-header { padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .section-header h2 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0; }
  .cell { padding: 14px 18px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
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
      <div class="cell"><div class="cell-label">Stake</div><div class="cell-value">${{ "%.2f"|format(p.stake) }}</div></div>
      <div class="cell"><div class="cell-label">Entry Price</div><div class="cell-value">{{ p.price_pct }}¢</div></div>
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
    <table>
      <thead><tr>
        <th>Sportsbook</th>
        <th>Odds</th>
        <th>Raw Implied %</th>
        <th>De-vigged %</th>
        <th></th>
      </tr></thead>
      <tbody>
        {% for r in p.breakdown %}
        <tr>
          <td>
            {% if r.url %}
            <a href="{{ r.url }}" target="_blank" rel="noopener" style="color:var(--text);text-decoration:none">
              <strong>{{ r.book }}</strong>
              <span style="font-size:10px;color:var(--blue);margin-left:4px">↗</span>
            </a>
            {% else %}
            <strong>{{ r.book }}</strong>
            {% endif %}
          </td>
          <td style="font-family:monospace">{{ '+' if r.odds > 0 else '' }}{{ r.odds }}</td>
          <td style="color:var(--muted)">{{ r.raw_prob }}%</td>
          <td><strong>{{ r.devigged_prob }}%</strong></td>
          <td>
            <div class="bar-wrap">
              <div class="bar-bg"><div class="bar-fill" style="width:{{ [r.devigged_prob, 100]|min }}%"></div></div>
            </div>
          </td>
        </tr>
        {% endfor %}
        {% if p.consensus %}
        <tr class="consensus-row">
          <td>Consensus (avg of {{ p.book_count }} books)</td>
          <td>—</td>
          <td>—</td>
          <td><strong style="color:var(--green)">{{ p.consensus }}%</strong></td>
          <td></td>
        </tr>
        {% endif %}
      </tbody>
    </table>
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
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; border-bottom: 1px solid var(--border); }
  header h1 { font-size: 18px; font-weight: 600; letter-spacing: 0.5px; }
  .mode-badge { font-size: 11px; font-weight: 700; padding: 3px 10px; border-radius: 12px; letter-spacing: 1px; }
  .mode-live { background: #052e16; color: var(--green); border: 1px solid var(--green); }
  .mode-paper { background: #1c1407; color: var(--yellow); border: 1px solid var(--yellow); }
  .refresh-info { font-size: 12px; color: var(--muted); }

  main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }

  /* Summary cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }
  .card-value { font-size: 24px; font-weight: 700; }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neutral { color: var(--text); }

  /* Charts */
  .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  @media (max-width: 700px) { .charts { grid-template-columns: 1fr; } }
  .chart-box { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .chart-box h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 14px; }
  .chart-box canvas { max-height: 220px; }

  /* Tables */
  .section { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 16px; overflow: hidden; }
  .section-header { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .section-header h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted); }
  .section-header .count { font-size: 12px; color: var(--muted); }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .empty-state { padding: 32px; text-align: center; color: var(--muted); font-size: 13px; }
  .tag { display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; letter-spacing: 0.5px; }
  .tag-win { background: #052e16; color: var(--green); }
  .tag-loss { background: #2d0a0a; color: var(--red); }
  .tag-open { background: #1c1a07; color: var(--yellow); }
  .tag-submitted { background: #051b2c; color: var(--blue); }
  .tag-paper { background: #1c1407; color: var(--yellow); }
  .tag-yes { background: #14103a; color: var(--purple); }
</style>
</head>
<body>

<header>
  <h1>Kalshi Arbitrage Bot</h1>
  <div style="display:flex;align-items:center;gap:16px;">
    <span id="mode-badge" class="mode-badge">—</span>
    <span class="refresh-info" id="last-updated">Loading…</span>
  </div>
</header>

<main>
  <!-- Summary cards -->
  <div class="cards" id="cards"></div>

  <!-- Charts -->
  <div class="charts">
    <div class="chart-box">
      <h2>Bankroll Over Time</h2>
      <canvas id="bankrollChart"></canvas>
    </div>
    <div class="chart-box">
      <h2>Cumulative P&amp;L</h2>
      <canvas id="pnlChart"></canvas>
    </div>
  </div>

  <!-- Sport breakdown -->
  <div class="section">
    <div class="section-header"><h2>Performance by Sport</h2></div>
    <div class="table-wrap"><table id="sport-table"></table></div>
  </div>

  <!-- Open positions -->
  <div class="section">
    <div class="section-header">
      <h2>Open Positions</h2>
      <span class="count" id="open-count"></span>
    </div>
    <div class="table-wrap"><table id="open-table"></table></div>
  </div>

  <!-- Settled positions -->
  <div class="section">
    <div class="section-header">
      <h2>Settled Positions</h2>
      <span class="count" id="settled-count"></span>
    </div>
    <div class="table-wrap"><table id="settled-table"></table></div>
  </div>

  <!-- Recent detections -->
  <div class="section">
    <div class="section-header"><h2>Recent Value Detections</h2></div>
    <div class="table-wrap"><table id="opp-table"></table></div>
  </div>
</main>

<script>
let bankrollChart, pnlChart;

function pnlClass(v) {
  if (v === null || v === undefined) return 'neutral';
  return v >= 0 ? 'pos' : 'neg';
}
function pnlStr(v) {
  if (v === null || v === undefined) return '—';
  const s = (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
  return `<span class="${pnlClass(v)}">${s}</span>`;
}
function roiStr(v) {
  if (v === null || v === undefined) return '—';
  const s = (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
  return `<span class="${v >= 0 ? 'pos' : 'neg'}">${s}</span>`;
}
function emptyRow(cols, msg) {
  return `<tr><td colspan="${cols}" class="empty-state">${msg}</td></tr>`;
}

function renderCards(s, mode) {
  const badge = document.getElementById('mode-badge');
  badge.textContent = mode;
  badge.className = 'mode-badge ' + (mode === 'PAPER' ? 'mode-paper' : 'mode-live');

  const pnlVal = s.total_pnl !== null
    ? `<span class="${pnlClass(s.total_pnl)}">${s.total_pnl >= 0 ? '+' : ''}$${Math.abs(s.total_pnl).toFixed(2)}</span>`
    : '<span class="neutral">—</span>';

  const wrVal = s.win_rate !== null
    ? `<span class="${s.win_rate >= 50 ? 'pos' : 'neg'}">${s.win_rate.toFixed(1)}%</span>`
    : '<span class="neutral">—</span>';

  const roiVal = s.roi !== null
    ? `<span class="${pnlClass(s.roi)}">${s.roi >= 0 ? '+' : ''}${s.roi.toFixed(1)}%</span>`
    : '<span class="neutral">—</span>';

  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="card-label">Total P&L</div><div class="card-value">${pnlVal}</div><div class="card-sub">${s.settled} settled bets</div></div>
    <div class="card"><div class="card-label">Win Rate</div><div class="card-value">${wrVal}</div><div class="card-sub">${s.wins}W / ${s.losses}L</div></div>
    <div class="card"><div class="card-label">ROI</div><div class="card-value">${roiVal}</div><div class="card-sub">on $${s.total_staked.toFixed(2)} staked</div></div>
    <div class="card"><div class="card-label">Open Positions</div><div class="card-value"><span class="neutral">${s.open_count}</span></div><div class="card-sub">${s.total_bets} total bets</div></div>
  `;
}

function renderCharts(bankrollData, pnlData) {
  const chartDefaults = {
    responsive: true,
    maintainAspectRatio: true,
    plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
    scales: {
      x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#1e2130' } },
      y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e2130' } },
    },
  };

  // Bankroll chart
  if (bankrollChart) bankrollChart.destroy();
  bankrollChart = new Chart(document.getElementById('bankrollChart'), {
    type: 'line',
    data: {
      labels: bankrollData.labels,
      datasets: [
        { label: 'Bankroll', data: bankrollData.bankroll, borderColor: '#22c55e', backgroundColor: 'rgba(34,197,94,0.08)', tension: 0.3, pointRadius: 3, fill: true },
        { label: 'At Risk', data: bankrollData.at_risk, borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.05)', tension: 0.3, pointRadius: 3, borderDash: [4, 4] },
      ],
    },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins } },
  });

  // P&L chart
  if (pnlChart) pnlChart.destroy();
  const hasData = pnlData.cumulative.length > 0;
  pnlChart = new Chart(document.getElementById('pnlChart'), {
    type: 'line',
    data: {
      labels: hasData ? pnlData.labels : ['No data'],
      datasets: [{
        label: 'Cumulative P&L',
        data: hasData ? pnlData.cumulative : [0],
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59,130,246,0.08)',
        tension: 0.3,
        pointRadius: 3,
        fill: true,
      }],
    },
    options: {
      ...chartDefaults,
      plugins: {
        ...chartDefaults.plugins,
        annotation: {},
      },
      scales: {
        ...chartDefaults.scales,
        y: {
          ...chartDefaults.scales.y,
          ticks: { ...chartDefaults.scales.y.ticks, callback: v => '$' + v.toFixed(0) },
        },
      },
    },
  });
}

function renderSportTable(rows) {
  const t = document.getElementById('sport-table');
  if (!rows.length) { t.innerHTML = emptyRow(8, 'No positions logged yet.'); return; }
  t.innerHTML = `<thead><tr>
    <th>Sport</th><th>Bets</th><th>Won</th><th>Lost</th><th>Open</th>
    <th>Staked</th><th>P&L</th><th>ROI</th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
    <td><strong>${r.sport}</strong></td>
    <td>${r.total}</td>
    <td class="pos">${r.wins}</td>
    <td class="neg">${r.losses}</td>
    <td>${r.open}</td>
    <td>$${r.staked.toFixed(2)}</td>
    <td>${pnlStr(r.pnl)}</td>
    <td>${roiStr(r.roi)}</td>
  </tr>`).join('') + '</tbody>';
}

function renderOpenTable(rows) {
  const t = document.getElementById('open-table');
  document.getElementById('open-count').textContent = rows.length ? rows.length + ' positions' : '';
  if (!rows.length) { t.innerHTML = emptyRow(7, 'No open positions.'); return; }
  t.innerHTML = `<thead><tr>
    <th>#</th><th>Bet On</th><th>Opponent</th><th>Sport</th><th>Type</th><th>Game Time</th>
    <th>Stake</th><th>Entry Price</th><th>Edge</th><th>Books</th><th>Spread</th>
    <th>Potential Win</th><th>Status</th>
  </tr></thead><tbody>` + rows.map(r => {
    const statusClass = r.exec_status === 'paper' ? 'tag-paper' : r.exec_status === 'submitted' ? 'tag-submitted' : 'tag-open';
    const edgeStr = r.edge != null ? `<span class="pos"><strong>${r.edge.toFixed(1)}%</strong></span>` : '<span style="color:var(--muted)">—</span>';
    const booksStr = r.books != null ? r.books : '<span style="color:var(--muted)">—</span>';
    const spreadStr = r.spread != null ? `${r.spread.toFixed(1)}¢` : '<span style="color:var(--muted)">—</span>';
    const gameTime = r.game_time && r.game_time !== '—' ? r.game_time : '<span style="color:var(--muted)">—</span>';
    const typeStr = r.bet_type && r.bet_type !== 'Moneyline' ? `<span style="color:var(--blue)">${r.bet_type}</span>` : `<span style="color:var(--muted)">Moneyline</span>`;
    return `<tr>
      <td><a href="/position/${r.id}" style="color:var(--blue);text-decoration:none">#${r.id}</a></td>
      <td><strong>${r.team}</strong></td>
      <td style="color:var(--muted)">${r.opponent}</td>
      <td>${r.sport}</td>
      <td>${typeStr}</td>
      <td>${gameTime}</td>
      <td>$${r.stake.toFixed(2)}</td>
      <td>${r.price_pct}¢</td>
      <td>${edgeStr}</td>
      <td>${booksStr}</td>
      <td>${spreadStr}</td>
      <td class="pos">+$${r.potential_win.toFixed(2)}</td>
      <td><span class="tag ${statusClass}">${r.exec_status}</span></td>
    </tr>`;
  }).join('') + '</tbody>';
}

function renderSettledTable(rows) {
  const t = document.getElementById('settled-table');
  document.getElementById('settled-count').textContent = rows.length ? rows.length + ' bets' : '';
  if (!rows.length) {
    t.innerHTML = emptyRow(7, 'No settled bets yet. Record outcomes with: python dashboard.py --settle ID --won (or --lost)');
    return;
  }
  t.innerHTML = `<thead><tr>
    <th>#</th><th>Team</th><th>Sport</th><th>Type</th><th>Stake</th>
    <th>Entry Price</th><th>P&L</th><th>Result</th><th>Settled</th>
  </tr></thead><tbody>` + rows.map(r => {
    const typeStr = r.bet_type && r.bet_type !== 'Moneyline' ? `<span style="color:var(--blue)">${r.bet_type}</span>` : `<span style="color:var(--muted)">Moneyline</span>`;
    return `<tr>
    <td><a href="/position/${r.id}" style="color:var(--blue);text-decoration:none">#${r.id}</a></td>
    <td><strong>${r.team}</strong></td>
    <td>${r.sport}</td>
    <td>${typeStr}</td>
    <td>$${r.stake.toFixed(2)}</td>
    <td>${r.price_pct}¢</td>
    <td>${pnlStr(r.pnl)}</td>
    <td><span class="tag ${r.won ? 'tag-win' : 'tag-loss'}">${r.won ? 'WIN' : 'LOSS'}</span></td>
    <td style="color:var(--muted)">${r.settled}</td>
  </tr>`;
  }).join('') + '</tbody>';
}

function renderOppTable(rows) {
  const t = document.getElementById('opp-table');
  if (!rows.length) { t.innerHTML = emptyRow(7, 'No value opportunities detected yet.'); return; }
  t.innerHTML = `<thead><tr>
    <th>Team</th><th>Sport</th><th>Consensus</th><th>Kalshi Price</th>
    <th>Edge</th><th>Alerted</th><th>Detected</th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
    <td><strong>${r.team}</strong></td>
    <td>${r.sport}</td>
    <td>${r.consensus.toFixed(1)}%</td>
    <td>${r.price.toFixed(1)}%</td>
    <td class="pos"><strong>${r.edge.toFixed(1)}%</strong></td>
    <td>${r.alerted ? '<span class="tag tag-win">Yes</span>' : '<span style="color:var(--muted)">No</span>'}</td>
    <td style="color:var(--muted)">${r.detected}</td>
  </tr>`).join('') + '</tbody>';
}

async function refresh() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();
    renderCards(d.summary, d.mode);
    renderCharts(d.bankroll_chart, d.pnl_chart);
    renderSportTable(d.sport_rows);
    renderOpenTable(d.open_rows);
    renderSettledTable(d.settled_rows);
    renderOppTable(d.opp_rows);
    document.getElementById('last-updated').textContent =
      'Updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('last-updated').textContent = 'Error fetching data';
  }
}

refresh();
setInterval(refresh, 60000);  // auto-refresh every 60s
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


if __name__ == "__main__":
    main()
