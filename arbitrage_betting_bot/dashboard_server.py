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

from flask import Flask, jsonify, render_template_string
import storage.db as db
from execution.auto_settle import auto_settle_positions

app = Flask(__name__)
IS_PAPER = False   # set by CLI arg at startup


# ── Data helpers (same logic as dashboard.py) ────────────────────────────────

def _short_sport(key: str) -> str:
    return {
        "americanfootball_nfl": "NFL",
        "americanfootball_ncaaf": "NCAAF",
        "basketball_nba": "NBA",
        "basketball_ncaab": "NCAAB",
        "baseball_mlb": "MLB",
        "icehockey_nhl": "NHL",
        "soccer_usa_mls": "MLS",
    }.get(key, key.upper())


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # convert UTC → local timezone
        h = dt.hour % 12 or 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{dt.strftime('%b')} {dt.day}  {h}:{dt.strftime('%M')} {ampm}"
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
        open_rows.append({
            "id": p["id"],
            "team": p["team_name"],
            "opponent": opponent,
            "sport": _short_sport(p["sport"]),
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
        settled_rows.append({
            "id": p["id"],
            "team": p["team_name"],
            "sport": _short_sport(p["sport"]),
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    return jsonify(build_data())


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


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
    <th>#</th><th>Bet On</th><th>Opponent</th><th>Sport</th><th>Game Time</th>
    <th>Stake</th><th>Entry Price</th><th>Edge</th><th>Books</th><th>Spread</th>
    <th>Potential Win</th><th>Status</th>
  </tr></thead><tbody>` + rows.map(r => {
    const statusClass = r.exec_status === 'paper' ? 'tag-paper' : r.exec_status === 'submitted' ? 'tag-submitted' : 'tag-open';
    const edgeStr = r.edge != null ? `<span class="pos"><strong>${r.edge.toFixed(1)}%</strong></span>` : '<span style="color:var(--muted)">—</span>';
    const booksStr = r.books != null ? r.books : '<span style="color:var(--muted)">—</span>';
    const spreadStr = r.spread != null ? `${r.spread.toFixed(1)}¢` : '<span style="color:var(--muted)">—</span>';
    const gameTime = r.game_time && r.game_time !== '—' ? r.game_time : '<span style="color:var(--muted)">—</span>';
    return `<tr>
      <td style="color:var(--muted)">${r.id}</td>
      <td><strong>${r.team}</strong></td>
      <td style="color:var(--muted)">${r.opponent}</td>
      <td>${r.sport}</td>
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
    <th>#</th><th>Team</th><th>Sport</th><th>Stake</th>
    <th>Entry Price</th><th>P&L</th><th>Result</th><th>Settled</th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
    <td style="color:var(--muted)">${r.id}</td>
    <td><strong>${r.team}</strong></td>
    <td>${r.sport}</td>
    <td>$${r.stake.toFixed(2)}</td>
    <td>${r.price_pct}¢</td>
    <td>${pnlStr(r.pnl)}</td>
    <td><span class="tag ${r.won ? 'tag-win' : 'tag-loss'}">${r.won ? 'WIN' : 'LOSS'}</span></td>
    <td style="color:var(--muted)">${r.settled}</td>
  </tr>`).join('') + '</tbody>';
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
