"""
SQLite database layer.

Tables:
  opportunities  — every value opportunity detected (logged even if filtered out)
  alerts         — opportunities that were actually surfaced to the user
  positions      — manually entered bets for correlation tracking
  bankroll_log   — daily bankroll snapshots
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "betting_bot.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist, then apply any pending migrations."""
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at     TEXT NOT NULL,
                sport           TEXT NOT NULL,
                home_team       TEXT NOT NULL,
                away_team       TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                platform        TEXT NOT NULL,
                consensus_prob  REAL NOT NULL,
                market_price    REAL NOT NULL,
                edge            REAL NOT NULL,
                market_url      TEXT NOT NULL,
                alerted         INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id  INTEGER REFERENCES opportunities(id),
                alerted_at      TEXT NOT NULL,
                recommended_bet REAL NOT NULL,
                bankroll_at_time REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                entered_at       TEXT NOT NULL,
                sport            TEXT NOT NULL,
                home_team        TEXT NOT NULL,
                away_team        TEXT NOT NULL,
                team_name        TEXT NOT NULL,
                platform         TEXT NOT NULL,
                stake            REAL NOT NULL,
                market_price     REAL NOT NULL,
                status           TEXT NOT NULL DEFAULT 'open',
                is_paper         INTEGER NOT NULL DEFAULT 0,
                order_id         TEXT NOT NULL DEFAULT '',
                execution_status TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS bankroll_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                log_date        TEXT NOT NULL UNIQUE,
                bankroll        REAL NOT NULL,
                total_at_risk   REAL NOT NULL
            );
        """)
    _migrate()
    logger.info("Database initialized at %s", DB_PATH)


def _migrate() -> None:
    """Add columns introduced after the initial schema (safe to run multiple times)."""
    with get_connection() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
        for col, ddl in [
            ("pnl",          "ALTER TABLE positions ADD COLUMN pnl REAL"),
            ("settled_at",   "ALTER TABLE positions ADD COLUMN settled_at TEXT"),
            ("market_ticker","ALTER TABLE positions ADD COLUMN market_ticker TEXT NOT NULL DEFAULT ''"),
            ("side",         "ALTER TABLE positions ADD COLUMN side TEXT NOT NULL DEFAULT ''"),
            ("edge",            "ALTER TABLE positions ADD COLUMN edge REAL"),
            ("bookmaker_count", "ALTER TABLE positions ADD COLUMN bookmaker_count INTEGER"),
            ("consensus_std",   "ALTER TABLE positions ADD COLUMN consensus_std REAL"),
            ("kalshi_spread",   "ALTER TABLE positions ADD COLUMN kalshi_spread REAL"),
            ("commence_time",   "ALTER TABLE positions ADD COLUMN commence_time TEXT"),
            ("bet_type",        "ALTER TABLE positions ADD COLUMN bet_type TEXT NOT NULL DEFAULT 'h2h'"),
            ("threshold",       "ALTER TABLE positions ADD COLUMN threshold REAL"),
            ("bookmakers_json", "ALTER TABLE positions ADD COLUMN bookmakers_json TEXT"),
        ]:
            if col not in existing:
                conn.execute(ddl)
                logger.debug("Migration: added positions.%s", col)


# ── Opportunities ─────────────────────────────────────────────────────────────

def log_opportunity(
    sport: str,
    home_team: str,
    away_team: str,
    team_name: str,
    platform: str,
    consensus_prob: float,
    market_price: float,
    edge: float,
    market_url: str,
    alerted: bool = False,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO opportunities
                (detected_at, sport, home_team, away_team, team_name, platform,
                 consensus_prob, market_price, edge, market_url, alerted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                sport, home_team, away_team, team_name, platform,
                consensus_prob, market_price, edge, market_url,
                1 if alerted else 0,
            ),
        )
        return cur.lastrowid


def log_alert(opportunity_id: int, recommended_bet: float, bankroll: float) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alerts (opportunity_id, alerted_at, recommended_bet, bankroll_at_time)
            VALUES (?, ?, ?, ?)
            """,
            (opportunity_id, datetime.utcnow().isoformat(), recommended_bet, bankroll),
        )
        conn.execute(
            "UPDATE opportunities SET alerted = 1 WHERE id = ?",
            (opportunity_id,),
        )


def count_alerts_today() -> int:
    today = date.today().isoformat()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM alerts WHERE alerted_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row["cnt"] if row else 0


# ── Positions ─────────────────────────────────────────────────────────────────

def add_position(
    sport: str,
    home_team: str,
    away_team: str,
    team_name: str,
    platform: str,
    stake: float,
    market_price: float,
    is_paper: bool = False,
    order_id: str = "",
    execution_status: str = "pending",
    market_ticker: str = "",
    side: str = "",
    edge: float | None = None,
    bookmaker_count: int | None = None,
    consensus_std: float | None = None,
    kalshi_spread: float | None = None,
    commence_time: str | None = None,
    bet_type: str = "h2h",
    threshold: float | None = None,
    bookmakers_json: str | None = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
                (entered_at, sport, home_team, away_team, team_name,
                 platform, stake, market_price, status, is_paper,
                 order_id, execution_status, market_ticker, side,
                 edge, bookmaker_count, consensus_std, kalshi_spread, commence_time,
                 bet_type, threshold, bookmakers_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                sport, home_team, away_team, team_name, platform, stake, market_price,
                1 if is_paper else 0,
                order_id,
                execution_status,
                market_ticker,
                side,
                edge,
                bookmaker_count,
                consensus_std,
                kalshi_spread,
                commence_time,
                bet_type,
                threshold,
                bookmakers_json,
            ),
        )
        return cur.lastrowid


def get_position(position_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()


def get_open_positions(is_paper: bool = False) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE status = 'open' AND is_paper = ?",
            (1 if is_paper else 0,),
        ).fetchall()


def close_position(position_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE positions SET status = 'closed' WHERE id = ?",
            (position_id,),
        )


# ── Bankroll ──────────────────────────────────────────────────────────────────

def snapshot_bankroll(bankroll: float, total_at_risk: float) -> None:
    today = date.today().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO bankroll_log (log_date, bankroll, total_at_risk)
            VALUES (?, ?, ?)
            ON CONFLICT(log_date) DO UPDATE SET bankroll=excluded.bankroll,
                                                total_at_risk=excluded.total_at_risk
            """,
            (today, bankroll, total_at_risk),
        )


# ── P&L Settlement ────────────────────────────────────────────────────────────

def settle_position(position_id: int, result: str) -> float:
    """
    Mark a position as closed with its outcome and compute realised P&L.

    result: "won" | "lost" | "void"
      won:  pnl = stake * (1 - entry_price) / entry_price
      lost: pnl = -stake
      void: pnl = 0.0  (market cancelled, stake returned)

    Returns the realised P&L in dollars.
    """
    if result not in ("won", "lost", "void"):
        raise ValueError(f"result must be 'won', 'lost', or 'void', got: {result!r}")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT stake, market_price FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Position {position_id} not found")
        stake: float = row["stake"]
        price: float = row["market_price"]
        if result == "won":
            pnl = stake * (1.0 - price) / price
        elif result == "lost":
            pnl = -stake
        else:  # void
            pnl = 0.0
        conn.execute(
            """
            UPDATE positions
            SET status = 'closed', pnl = ?, settled_at = ?
            WHERE id = ?
            """,
            (pnl, datetime.utcnow().isoformat(), position_id),
        )
        return pnl


# ── Dashboard Queries ─────────────────────────────────────────────────────────

def get_all_positions(is_paper: bool = False) -> list[sqlite3.Row]:
    """All positions (open and closed) for one mode, newest first."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE is_paper = ? ORDER BY entered_at DESC",
            (1 if is_paper else 0,),
        ).fetchall()


def get_bankroll_history() -> list[sqlite3.Row]:
    """All bankroll snapshots, oldest first."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM bankroll_log ORDER BY log_date ASC"
        ).fetchall()


def get_top_opportunities(limit: int = 50) -> list[sqlite3.Row]:
    """Most recent detected opportunities, newest first."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM opportunities ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
