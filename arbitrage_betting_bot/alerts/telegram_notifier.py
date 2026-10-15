"""
Telegram notification for newly flagged retail-sportsbook value bets.

Replaces alerts/alert_manager.py::send_alert() (a logging-only stub) for the
retail path -- the whole point of this restructure is that the bot can no
longer execute a trade, so getting the recommendation in front of the user
FAST, on their phone, is the entire automation surface that's left.

One-time setup (do this once, outside any code):
  1. Message @BotFather on Telegram -> /newbot -> copy the token it gives you
     into TELEGRAM_BOT_TOKEN (.env).
  2. Send the new bot any message (so it has a chat to reply into).
  3. GET https://api.telegram.org/bot<token>/getUpdates and read
     result[0].message.chat.id into TELEGRAM_CHAT_ID (.env).
"""
from __future__ import annotations

import logging

import requests

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org"


def send_bet_notification(flagged_bet: dict) -> str | None:
    """POST sendMessage with two inline URL buttons: the sportsbook deep link
    (or best available fallback -- see core/deeplink_builder.py) and a link to
    this bet's dashboard detail page. Returns Telegram's message_id on success
    (for storage/db.py::mark_bet_notified), or None on failure.

    Never raises -- same best-effort discipline as every other external call
    in this codebase (e.g. dashboard_server.py::build_data()'s try/except
    around settlement polling). A missed notification is bad; a crashed scan
    because Telegram was briefly down is worse.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- skipping notification "
            "for %s (see alerts/telegram_notifier.py's one-time setup steps)",
            flagged_bet.get("outcome_label"),
        )
        return None

    book_label = flagged_bet["recommended_book"].replace("_", " ").title()
    odds = flagged_bet["recommended_odds_american"]
    text = (
        f"<b>Value bet flagged</b>\n"
        f"{flagged_bet['home_team']} vs {flagged_bet['away_team']}\n"
        f"{flagged_bet['outcome_label']}  @ {book_label}  {odds:+d}\n"
        f"edge {flagged_bet['edge']:+.1%}  "
        f"(fair {flagged_bet['consensus_prob']:.1%} vs implied {flagged_bet['recommended_prob']:.1%})\n"
        f"Recommended stake: ${flagged_bet['recommended_stake']:.2f}"
    )

    buttons = []
    if flagged_bet.get("deep_link_url"):
        buttons.append({"text": f"Open in {book_label}", "url": flagged_bet["deep_link_url"]})
    if flagged_bet.get("dashboard_detail_url"):
        buttons.append({"text": "View on Dashboard", "url": flagged_bet["dashboard_detail_url"]})

    base_payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    # Telegram rejects the WHOLE message if ANY one inline button's URL fails
    # its validation (confirmed live 2026-10-13: a "localhost" dashboard URL
    # 400'd and silently lost an otherwise-good notification, deep link
    # included). Degrade one button at a time rather than losing the message
    # entirely over one bad link.
    for keep in range(len(buttons), -1, -1):
        payload = dict(base_payload)
        if keep:
            payload["reply_markup"] = {"inline_keyboard": [buttons[:keep]]}
        message_id = _try_send(payload)
        if message_id is not None:
            if keep < len(buttons):
                logger.warning(
                    "Telegram notification for %s sent with %d/%d button(s) -- "
                    "the rest failed URL validation",
                    flagged_bet.get("outcome_label"), keep, len(buttons),
                )
            return message_id

    logger.error("Telegram notification failed entirely for %s (even with no buttons)",
                 flagged_bet.get("outcome_label"))
    return None


def send_batch_notification(book_key: str, bets: list[dict], batch_id: str) -> str | None:
    """ONE message for every bet flagged at one book in one scan, with a single
    button that loads all of them into that book's bet slip at once.

    The button points at the dashboard's /s/<batch_id> route rather than at a
    sportsbook directly -- that route redirects into the iOS Shortcut, which
    opens each deep link in turn. Telegram will not accept a shortcuts:// URL
    in a button (confirmed live 2026-10-14), so the https hop is required.

    Falls back to a link-free message if the dashboard URL is unusable, rather
    than losing the alert -- see send_bet_notification()'s button degradation.
    """
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured -- skipping batch alert for %s", book_key)
        return None
    if not bets:
        return None

    book_label = book_key.replace("_", " ").title()
    lines = [f"<b>{len(bets)} value bet{'' if len(bets) == 1 else 's'} — {book_label}</b>"]
    total_stake = 0.0
    for b in bets:
        total_stake += b["recommended_stake"] or 0.0
        lines.append(
            f"\n• {b['outcome_label']}  {b['recommended_odds_american']:+d}\n"
            f"   {b['home_team']} vs {b['away_team']}\n"
            f"   edge {b['edge']:+.1%} · stake ${b['recommended_stake']:.2f}"
        )
    lines.append(f"\nTotal staked: ${total_stake:.2f}")
    text = "\n".join(lines)

    batch_url = f"{config.DASHBOARD_BASE_URL}/s/{batch_id}"
    buttons = [
        {"text": f"Add all {len(bets)} to {book_label} slip", "url": batch_url},
        {"text": "View on Dashboard", "url": f"{config.DASHBOARD_BASE_URL}/value-bets"},
    ]
    base = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

    for keep in range(len(buttons), -1, -1):
        payload = dict(base)
        if keep:
            payload["reply_markup"] = {"inline_keyboard": [[b] for b in buttons[:keep]]}
        message_id = _try_send(payload)
        if message_id is not None:
            if keep < len(buttons):
                logger.warning("Batch alert for %s sent with %d/%d button(s)",
                               book_key, keep, len(buttons))
            return message_id

    logger.error("Batch alert failed entirely for %s", book_key)
    return None


def _try_send(payload: dict) -> str | None:
    """One sendMessage attempt. Returns the message_id on success, None on any
    failure (bad request, network error, etc) -- never raises."""
    try:
        resp = requests.post(
            f"{_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=10,
        )
        if not resp.ok:
            logger.debug("Telegram sendMessage %d: %s", resp.status_code, resp.text)
            return None
        message_id = resp.json().get("result", {}).get("message_id")
        return str(message_id) if message_id is not None else None
    except requests.RequestException as e:
        logger.debug("Telegram sendMessage request error: %s", e)
        return None
