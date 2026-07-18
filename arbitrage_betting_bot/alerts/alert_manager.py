"""
Sends value alerts to the terminal (rich formatted table).
"""
from __future__ import annotations

import logging

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.value_detector import ValueOpportunity
from core.kelly_calculator import BetSizing

logger = logging.getLogger(__name__)


def send_alert(
    opp: ValueOpportunity,
    sizing: BetSizing,
    dry_run: bool = False,
    paper: bool = False,
) -> None:
    """Log a one-line value alert. Full details are stored in the dashboard."""
    tag = "DRY RUN" if dry_run else "PAPER" if paper else "LIVE"
    logger.info(
        "[%s] VALUE — %s  |  edge %.1f%%  |  $%.2f  |  consensus %.1f%%  |  Kalshi %.1f%%",
        tag,
        opp.team_name,
        opp.edge * 100,
        sizing.recommended_dollars,
        opp.consensus_prob * 100,
        opp.market_price * 100,
    )


def print_no_value_found() -> None:
    console.print("[dim]No value opportunities found this scan.[/dim]")
