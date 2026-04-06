"""
Arbitrage Betting Bot — P&L Dashboard

Usage:
    python dashboard.py                     # Live-mode stats
    python dashboard.py --paper             # Paper-mode stats
    python dashboard.py --all               # Both modes side by side
    python dashboard.py --settle 5 --won    # Mark position 5 as won
    python dashboard.py --settle 5 --lost   # Mark position 5 as lost

Run from the arbitrage_betting_bot/ directory (or any directory — it resolves
the DB path relative to this file).
"""
from __future__ import annotations

import argparse
import sys
import os
from collections import defaultdict
from datetime import datetime

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(__file__))

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich import box

import storage.db as db

console = Console()

# ── Helpers ───────────────────────────────────────────────────────────────────

_BARS = " ▁▂▃▄▅▆▇█"


def _sparkline(values: list[float], width: int = 20) -> str:
    """Return a unicode sparkline for a sequence of values."""
    if not values:
        return "—"
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    chars = [_BARS[round((v - lo) / span * (len(_BARS) - 1))] for v in values]
    return "".join(chars[-width:])


def _pnl_str(pnl: float | None, *, color: bool = True) -> str:
    if pnl is None:
        return "—"
    sign = "+" if pnl >= 0 else ""
    s = f"{sign}${pnl:,.2f}"
    if not color:
        return s
    return f"[green]{s}[/green]" if pnl >= 0 else f"[red]{s}[/red]"


def _roi_str(roi: float | None) -> str:
    if roi is None:
        return "—"
    sign = "+" if roi >= 0 else ""
    s = f"{sign}{roi:.1f}%"
    return f"[green]{s}[/green]" if roi >= 0 else f"[red]{s}[/red]"


def _short_sport(sport_key: str) -> str:
    mapping = {
        "americanfootball_nfl": "NFL",
        "americanfootball_ncaaf": "NCAAF",
        "basketball_nba": "NBA",
        "basketball_ncaab": "NCAAB",
        "baseball_mlb": "MLB",
        "icehockey_nhl": "NHL",
        "soccer_usa_mls": "MLS",
        "soccer_epl": "EPL",
        "soccer_uefa_champs_league": "UCL",
    }
    return mapping.get(sport_key, sport_key.upper())


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%b %d  %H:%M")
    except ValueError:
        return iso[:16]


# ── Stats computation ─────────────────────────────────────────────────────────

def _compute_stats(positions: list) -> dict:
    """Aggregate P&L stats from a list of position rows."""
    total_staked = 0.0
    total_pnl = 0.0
    wins = 0
    losses = 0
    settled = 0
    open_count = 0
    by_sport: dict[str, dict] = defaultdict(
        lambda: {"staked": 0.0, "pnl": 0.0, "wins": 0, "losses": 0, "open": 0, "edges": []}
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

    roi = (total_pnl / total_staked * 100) if total_staked > 0 and settled > 0 else None
    win_rate = (wins / settled * 100) if settled > 0 else None

    return {
        "total_staked": total_staked,
        "total_pnl": total_pnl if settled > 0 else None,
        "roi": roi,
        "wins": wins,
        "losses": losses,
        "settled": settled,
        "open_count": open_count,
        "total_bets": len(positions),
        "win_rate": win_rate,
        "by_sport": dict(by_sport),
    }


# ── Dashboard panels ──────────────────────────────────────────────────────────

def _summary_cards(stats: dict, mode_label: str) -> Columns:
    def card(title: str, body: str) -> Panel:
        return Panel(Text.from_markup(body, justify="center"), title=title, expand=True)

    pnl_val = _pnl_str(stats["total_pnl"])
    roi_val = _roi_str(stats["roi"])

    settled = stats["settled"]
    wins = stats["wins"]
    losses = stats["losses"]
    win_rate = stats["win_rate"]
    wr_str = f"{wins}/{settled}  ({win_rate:.1f}%)" if win_rate is not None else "No settled bets"

    open_str = str(stats["open_count"])
    staked_str = f"${stats['total_staked']:,.2f}"

    return Columns(
        [
            card(f"P&L  [{mode_label}]", pnl_val),
            card("Win Rate", wr_str),
            card("ROI", roi_val),
            card("Open Positions", open_str),
            card("Total Staked", staked_str),
        ],
        equal=True,
        expand=True,
    )


def _bankroll_panel(history: list) -> Panel:
    if not history:
        return Panel("[dim]No bankroll snapshots yet. Run the bot to generate data.[/dim]",
                     title="Bankroll History")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Date", style="dim", width=12)
    table.add_column("Bankroll", justify="right")
    table.add_column("At Risk", justify="right")
    table.add_column("Chart (last 30 days)", min_width=32)

    bankrolls = [row["bankroll"] for row in history]
    sparkline = _sparkline(bankrolls)

    for i, row in enumerate(history[-15:]):  # show last 15 rows in table
        is_last = i == len(history[-15:]) - 1
        chart_col = f"[cyan]{sparkline}[/cyan]  ${bankrolls[-1]:,.2f}" if is_last else ""
        table.add_row(
            row["log_date"],
            f"${row['bankroll']:,.2f}",
            f"${row['total_at_risk']:,.2f}",
            chart_col,
        )

    return Panel(table, title="Bankroll History")


def _sport_breakdown_panel(by_sport: dict) -> Panel:
    if not by_sport:
        return Panel("[dim]No positions logged yet.[/dim]", title="Performance by Sport")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Sport", style="bold")
    table.add_column("Bets", justify="right")
    table.add_column("Won", justify="right")
    table.add_column("Lost", justify="right")
    table.add_column("Open", justify="right")
    table.add_column("Staked", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("ROI", justify="right")

    for sport, s in sorted(by_sport.items()):
        staked = s["staked"]
        pnl = s["pnl"]
        wins = s["wins"]
        losses = s["losses"]
        open_c = s["open"]
        settled = wins + losses
        total = settled + open_c
        roi = (pnl / staked * 100) if staked > 0 and settled > 0 else None
        table.add_row(
            sport,
            str(total),
            f"[green]{wins}[/green]" if wins else str(wins),
            f"[red]{losses}[/red]" if losses else str(losses),
            str(open_c),
            f"${staked:,.2f}",
            _pnl_str(pnl if settled > 0 else None),
            _roi_str(roi),
        )

    return Panel(table, title="Performance by Sport")


def _open_positions_panel(positions: list) -> Panel:
    open_pos = [p for p in positions if p["status"] == "open"]
    if not open_pos:
        return Panel("[dim]No open positions.[/dim]", title="Open Positions")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("ID", justify="right", style="dim", width=5)
    table.add_column("Team", min_width=20)
    table.add_column("Sport", width=7)
    table.add_column("Stake", justify="right")
    table.add_column("Entry Price", justify="right")
    table.add_column("Potential Win", justify="right")
    table.add_column("Exec Status", width=12)
    table.add_column("Entered", width=14)

    for p in open_pos[:20]:  # cap at 20 rows
        stake = p["stake"]
        price = p["market_price"]
        potential_win = stake * (1.0 - price) / price if price > 0 else 0.0
        exec_s = p["execution_status"] or "—"
        color = "yellow" if exec_s == "paper" else "green" if exec_s == "submitted" else "red"
        table.add_row(
            str(p["id"]),
            p["team_name"],
            _short_sport(p["sport"]),
            f"${stake:.2f}",
            f"{price * 100:.0f}¢",
            f"[green]+${potential_win:.2f}[/green]",
            f"[{color}]{exec_s}[/{color}]",
            _fmt_dt(p["entered_at"]),
        )

    title = f"Open Positions ({len(open_pos)} total)"
    if len(open_pos) > 20:
        title += "  [dim](showing 20 of {len(open_pos)})[/dim]"
    return Panel(table, title=title)


def _closed_positions_panel(positions: list) -> Panel:
    closed = [p for p in positions if p["status"] == "closed"]
    if not closed:
        return Panel(
            "[dim]No settled bets yet.\n\n"
            "Use  [bold]python dashboard.py --settle ID --won[/bold]  "
            "or  [bold]--lost[/bold]  once results are in.[/dim]",
            title="Settled Positions",
        )

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("ID", justify="right", style="dim", width=5)
    table.add_column("Team", min_width=20)
    table.add_column("Sport", width=7)
    table.add_column("Stake", justify="right")
    table.add_column("Entry Price", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Result", width=6)
    table.add_column("Settled", width=14)

    for p in closed[:20]:
        pnl = p["pnl"]
        result = "[green]WIN[/green]" if (pnl is not None and pnl >= 0) else "[red]LOSS[/red]"
        table.add_row(
            str(p["id"]),
            p["team_name"],
            _short_sport(p["sport"]),
            f"${p['stake']:.2f}",
            f"{p['market_price'] * 100:.0f}¢",
            _pnl_str(pnl),
            result,
            _fmt_dt(p["settled_at"]),
        )

    title = f"Settled Positions ({len(closed)} total)"
    return Panel(table, title=title)


def _recent_opps_panel(opps: list) -> Panel:
    if not opps:
        return Panel("[dim]No opportunities detected yet.[/dim]", title="Recent Detections")

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Team", min_width=20)
    table.add_column("Sport", width=7)
    table.add_column("Consensus", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Edge", justify="right")
    table.add_column("Alerted", width=7)
    table.add_column("Detected", width=14)

    for o in opps[:15]:
        edge_pct = f"{o['edge'] * 100:.1f}%"
        alerted = "[green]Yes[/green]" if o["alerted"] else "[dim]No[/dim]"
        table.add_row(
            o["team_name"],
            _short_sport(o["sport"]),
            f"{o['consensus_prob'] * 100:.1f}%",
            f"{o['market_price'] * 100:.1f}%",
            f"[bold green]{edge_pct}[/bold green]",
            alerted,
            _fmt_dt(o["detected_at"]),
        )

    return Panel(table, title=f"Recent Value Detections (last {min(len(opps), 15)})")


# ── Render ────────────────────────────────────────────────────────────────────

def render_dashboard(is_paper: bool) -> None:
    # Create tables and apply any pending migrations
    db.init_db()

    mode_label = "PAPER" if is_paper else "LIVE"
    positions = db.get_all_positions(is_paper=is_paper)
    bankroll_history = db.get_bankroll_history()
    recent_opps = db.get_top_opportunities(limit=50)

    stats = _compute_stats(positions)

    console.print()
    console.print(Rule(f"[bold white]KALSHI ARBITRAGE BOT — {mode_label} DASHBOARD[/bold white]"))
    console.print()

    console.print(_summary_cards(stats, mode_label))
    console.print()
    console.print(_bankroll_panel(bankroll_history))
    console.print()
    console.print(_sport_breakdown_panel(stats["by_sport"]))
    console.print()
    console.print(_open_positions_panel(positions))
    console.print()
    console.print(_closed_positions_panel(positions))
    console.print()
    console.print(_recent_opps_panel(recent_opps))
    console.print()

    if stats["open_count"] > 0:
        console.print(
            "[dim]Tip: once a result is known, record it with "
            "[/dim][bold]python dashboard.py --settle ID --won[/bold]"
            "[dim] (or [/dim][bold]--lost[/bold][dim]).[/dim]"
        )
        console.print()


# ── Settle CLI ────────────────────────────────────────────────────────────────

def do_settle(position_id: int, won: bool) -> None:
    db.init_db()
    result = "won" if won else "lost"
    result_label = "[green]WON[/green]" if won else "[red]LOST[/red]"
    try:
        pnl = db.settle_position(position_id, result)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    sign = "+" if pnl >= 0 else ""
    console.print(
        f"Position [bold]#{position_id}[/bold] marked as {result_label}  "
        f"→  P&L: [bold]{sign}${pnl:.2f}[/bold]"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Arbitrage Bot — P&L Dashboard")
    parser.add_argument("--paper", action="store_true", help="Show paper-mode stats")
    parser.add_argument("--settle", type=int, metavar="ID", help="Position ID to settle")
    result_group = parser.add_mutually_exclusive_group()
    result_group.add_argument("--won", action="store_true", help="Mark settled position as won")
    result_group.add_argument("--lost", action="store_true", help="Mark settled position as lost")
    args = parser.parse_args()

    if args.settle is not None:
        if not args.won and not args.lost:
            console.print("[bold red]Error:[/bold red] --settle requires --won or --lost")
            sys.exit(1)
        do_settle(args.settle, won=args.won)
    else:
        render_dashboard(is_paper=args.paper)


if __name__ == "__main__":
    main()
