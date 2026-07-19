"""
Sends value alerts to the terminal and (in live mode) via Pushover push notification.
"""
from __future__ import annotations

import logging
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from core.value_detector import ValueOpportunity
from core.kelly_calculator import BetSizing
import config

logger = logging.getLogger(__name__)


def _pushover(title: str, message: str) -> None:
    """Send a Pushover push notification. Silently no-ops if keys not configured."""
    if not (config.PUSHOVER_USER_KEY and config.PUSHOVER_APP_TOKEN):
        return
    try:
        payload = {
            "token": config.PUSHOVER_APP_TOKEN,
            "user": config.PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
        }
        if config.DASHBOARD_URL:
            payload["url"] = config.DASHBOARD_URL
            payload["url_title"] = "Open Dashboard"
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data=payload,
            timeout=5,
        )
    except Exception:
        pass  # Never block the main loop for a notification failure


def send_alert(
    opp: ValueOpportunity,
    sizing: BetSizing,
    dry_run: bool = False,
    paper: bool = False,
) -> None:
    """Log a one-line value alert and push to phone in live mode."""
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
    if not dry_run and not paper:
        _pushover(
            title=f"Bet queued: {opp.team_name}",
            message=(
                f"${sizing.recommended_dollars:.0f} @ Kalshi {opp.market_price * 100:.0f}¢  |  "
                f"edge {opp.edge * 100:.1f}%  |  consensus {opp.consensus_prob * 100:.0f}%"
            ),
        )
