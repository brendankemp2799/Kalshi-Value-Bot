"""
Sends value alerts to the terminal (rich formatted table).
"""
from __future__ import annotations

import logging
try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except ImportError:
    import pytz
    _PT = pytz.timezone("America/Los_Angeles")

from rich.console import Console
from rich.table import Table
from rich import box

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.value_detector import ValueOpportunity
from core.kelly_calculator import BetSizing

logger = logging.getLogger(__name__)
console = Console()


def _format_prob(p: float) -> str:
    return f"{p * 100:.1f}%"


def _format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def send_alert(
    opp: ValueOpportunity,
    sizing: BetSizing,
    dry_run: bool = False,
    paper: bool = False,
) -> None:
    """Print a rich terminal alert."""
    event = opp.matched_event.odds_event
    home = event.home_team
    away = event.away_team
    sport = event.sport_key.replace("_", " ").upper()
    game_dt = event.commence_time.astimezone(_PT)
    h = game_dt.hour % 12 or 12
    ampm = "AM" if game_dt.hour < 12 else "PM"
    commence = f"{game_dt.strftime('%b')} {game_dt.day}  {h}:{game_dt.strftime('%M')} {ampm} PT"

    tag = "[DRY RUN] " if dry_run else "[PAPER] " if paper else ""
    console.print()
    console.rule(f"[bold green]{tag}VALUE ALERT — {sport}[/bold green]")

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column("Field", style="bold cyan", min_width=20)
    table.add_column("Value", style="white")

    table.add_row("Event", f"{home} vs {away}")
    table.add_row("Start Time", commence)
    table.add_row("Bet On", f"[bold yellow]{opp.team_name}[/bold yellow] to win")
    table.add_row("Platform", "[bold magenta]Kalshi[/bold magenta]")
    table.add_row("Sportsbook Consensus", _format_prob(opp.consensus_prob))
    table.add_row(
        "Market Price",
        f"[bold green]{_format_prob(opp.market_price)}[/bold green]  "
        f"({_format_american(opp.market_odds_american)})",
    )
    table.add_row("Edge", f"[bold green]{opp.edge_pct}[/bold green]")
    table.add_row("─" * 20, "─" * 30)
    table.add_row("Recommended Bet", f"[bold white]${sizing.recommended_dollars:.2f}[/bold white]")
    table.add_row(
        "Kelly Fraction",
        f"{sizing.full_kelly_fraction * 100:.1f}% full → "
        f"{sizing.fractional_kelly * 100:.1f}% fractional ({config.KELLY_FRACTION:.0%} Kelly)",
    )
    table.add_row("Bankroll", f"${sizing.bankroll:,.2f}")
    table.add_row("─" * 20, "─" * 30)
    table.add_row("Market Link", f"[link={opp.market_url}]{opp.market_url}[/link]")

    console.print(table)
    console.print()


def print_no_value_found() -> None:
    console.print("[dim]No value opportunities found this scan.[/dim]")
