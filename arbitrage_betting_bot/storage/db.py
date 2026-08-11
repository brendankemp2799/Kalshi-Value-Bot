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
import sys
import os
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config

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
            CREATE TABLE IF NOT EXISTS scan_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id         TEXT NOT NULL,
                scanned_at      TEXT NOT NULL,
                sport           TEXT NOT NULL,
                home_team       TEXT NOT NULL,
                away_team       TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                bet_type        TEXT NOT NULL DEFAULT 'h2h',
                threshold       REAL,
                kalshi_ticker   TEXT,
                kalshi_spread   REAL,
                kalshi_volume   REAL,
                kalshi_price    REAL,
                limit_price     REAL,
                consensus_prob  REAL,
                bookmaker_count INTEGER,
                consensus_std   REAL,
                edge            REAL,
                status          TEXT NOT NULL,
                reason          TEXT,
                maker_only      INTEGER NOT NULL DEFAULT 0,
                commence_time   TEXT,
                bookmakers_json TEXT
            );
        """)
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

            CREATE TABLE IF NOT EXISTS api_credits (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at     TEXT NOT NULL,
                used_total      INTEGER,
                remaining       INTEGER,
                used_this_scan  INTEGER
            );

            -- Long-lived (never pruned, unlike scan_log) record of every scanned
            -- candidate that had both a Kalshi price and a sportsbook consensus --
            -- i.e. enough to later score individual books' predictive accuracy
            -- against the real outcome, for ALL scanned candidates rather than just
            -- the ones that became bets. See research/experiments/2026-08-11-
            -- book-weight-validation.md for why this exists.
            CREATE TABLE IF NOT EXISTS book_probability_log (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at              TEXT NOT NULL,
                sport                   TEXT NOT NULL,
                bet_type                TEXT NOT NULL,
                team_name               TEXT NOT NULL,
                threshold               REAL,
                kalshi_ticker           TEXT NOT NULL,
                kalshi_side             TEXT,
                consensus_prob          REAL,
                bookmaker_count         INTEGER,
                bookmakers_json         TEXT,
                commence_time           TEXT,
                actual_outcome          REAL,
                outcome_check_attempts  INTEGER NOT NULL DEFAULT 0
            );
        """)
    _migrate()
    logger.info("Database initialized at %s", DB_PATH)


def _migrate() -> None:
    """Add columns introduced after the initial schema (safe to run multiple times)."""
    with get_connection() as conn:
        # scan_log migrations
        scan_existing = {row[1] for row in conn.execute("PRAGMA table_info(scan_log)").fetchall()}
        for col, ddl in [
            ("bookmakers_json", "ALTER TABLE scan_log ADD COLUMN bookmakers_json TEXT"),
            ("limit_price",     "ALTER TABLE scan_log ADD COLUMN limit_price REAL"),
            ("maker_only",      "ALTER TABLE scan_log ADD COLUMN maker_only INTEGER NOT NULL DEFAULT 0"),
        ]:
            if col not in scan_existing:
                conn.execute(ddl)
                logger.debug("Migration: added scan_log.%s", col)

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
            ("bookmakers_json",  "ALTER TABLE positions ADD COLUMN bookmakers_json TEXT"),
            ("failure_reason",   "ALTER TABLE positions ADD COLUMN failure_reason TEXT"),
            ("fill_type",        "ALTER TABLE positions ADD COLUMN fill_type TEXT NOT NULL DEFAULT 'taker'"),
            ("kalshi_close_price",     "ALTER TABLE positions ADD COLUMN kalshi_close_price REAL"),
            ("consensus_close_prob",   "ALTER TABLE positions ADD COLUMN consensus_close_prob REAL"),
            ("closing_line_attempts", "ALTER TABLE positions ADD COLUMN closing_line_attempts INTEGER NOT NULL DEFAULT 0"),
            ("peak_price",   "ALTER TABLE positions ADD COLUMN peak_price REAL"),
            ("close_reason", "ALTER TABLE positions ADD COLUMN close_reason TEXT"),
            ("entry_fee_paid", "ALTER TABLE positions ADD COLUMN entry_fee_paid REAL NOT NULL DEFAULT 0.0"),
            ("order_verified_at", "ALTER TABLE positions ADD COLUMN order_verified_at TEXT"),
            ("strategy", "ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'value_edge'"),
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
    failure_reason: str | None = None,
    fill_type: str = "taker",
    entry_fee_paid: float = 0.0,
    strategy: str = "value_edge",
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions
                (entered_at, sport, home_team, away_team, team_name,
                 platform, stake, market_price, status, is_paper,
                 order_id, execution_status, market_ticker, side,
                 edge, bookmaker_count, consensus_std, kalshi_spread, commence_time,
                 bet_type, threshold, bookmakers_json, failure_reason, fill_type,
                 entry_fee_paid, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                sport, home_team, away_team, team_name, platform, stake, market_price,
                "failed" if execution_status == "failed" else "open",
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
                failure_reason,
                fill_type,
                entry_fee_paid,
                strategy,
            ),
        )
        return cur.lastrowid


def get_position(position_id: int) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()


def get_daily_stake_total(is_paper: bool = False) -> float:
    """Sum of stakes placed in new positions entered today (UTC). Used for daily capital risk gate."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(stake), 0.0) FROM positions "
            "WHERE entered_at LIKE ? AND is_paper = ? AND execution_status != 'failed'",
            (f"{today}%", 1 if is_paper else 0),
        ).fetchone()
        return float(row[0]) if row else 0.0


def count_open_positions(is_paper: bool = False) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE status = 'open' AND is_paper = ? AND execution_status != 'failed'",
            (1 if is_paper else 0,),
        ).fetchone()
        return row[0] if row else 0


def get_open_positions(is_paper: bool = False) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions "
            "WHERE status = 'open' AND is_paper = ? AND execution_status != 'failed'",
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
      won:  pnl = stake * (1 - entry_price) / entry_price - entry_fee_paid
      lost: pnl = -stake - entry_fee_paid
      void: pnl = 0.0  (market cancelled, stake returned)

    entry_fee_paid is the actual dollar fee Kalshi charged at fill time (read directly
    from the order's own record — see execution/kalshi_executor.py::_actual_fee_dollars),
    not a formula estimate. It's owed whether the position wins or loses, which the
    old formula-based version of this function got wrong for losses (no fee was ever
    subtracted there).

    Returns the realised P&L in dollars.
    """
    if result not in ("won", "lost", "void"):
        raise ValueError(f"result must be 'won', 'lost', or 'void', got: {result!r}")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT stake, market_price, entry_fee_paid FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Position {position_id} not found")
        stake: float = row["stake"]
        price: float = row["market_price"]
        entry_fee_paid: float = row["entry_fee_paid"] or 0.0
        if result == "won":
            gross_profit = stake * (1.0 - price) / price
            pnl = gross_profit - entry_fee_paid
        elif result == "lost":
            pnl = -stake - entry_fee_paid
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


# ── Risk Management (trailing stop) ─────────────────────────────────────────────

def set_peak_price(position_id: int, peak_price: float) -> None:
    """Record a new high-water mark for the trailing stop."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE positions SET peak_price = ? WHERE id = ?",
            (peak_price, position_id),
        )


def close_position_early(
    position_id: int,
    exit_price: float,
    reason: str = "trailing_stop",
    exit_fee: float = 0.0,
) -> float:
    """
    Close a position via our own trade (not a natural Kalshi settlement) — e.g. a
    trailing-stop trigger. Independent of settle_position() by design: touching that
    function's tested win/lost/void math isn't worth the risk for this new path.

    pnl = contracts * (exit_price - entry_price) - entry_fee_paid - exit_fee, same
    underlying formula as a natural settlement (won = exit_price 1.0, lost = exit_price
    0.0, void = exit_price == entry_price), just computed for an arbitrary exit price.
    Both the fee paid to enter and the fee paid to exit (each read from Kalshi's own
    order records, not estimated) are real costs regardless of how the trade turns out.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT stake, market_price, entry_fee_paid FROM positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Position {position_id} not found")
        stake: float = row["stake"]
        entry_price: float = row["market_price"]
        entry_fee_paid: float = row["entry_fee_paid"] or 0.0
        contracts = stake / entry_price
        pnl = contracts * (exit_price - entry_price) - entry_fee_paid - exit_fee
        conn.execute(
            """
            UPDATE positions
            SET status = 'closed', pnl = ?, settled_at = ?, close_reason = ?
            WHERE id = ?
            """,
            (pnl, datetime.utcnow().isoformat(), reason, position_id),
        )
        return pnl


# ── Order ID Verification (ghost-position audit) ────────────────────────────────

def get_unverified_real_positions() -> list[sqlite3.Row]:
    """
    Real (live, actually-submitted) positions whose order_id hasn't yet been checked
    against Kalshi's own order history. Once checked, a position is never re-checked —
    see execution/reconciliation.py::audit_order_ids() for why, and for what happens
    when one turns out not to be real (a "ghost" — see the incident this was built for,
    2026-07-23: a position whose order_id was actually a client-side UUID rather than
    Kalshi's real assigned order_id, double-counting a real trade with the wrong stake).
    """
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE is_paper = 0 AND execution_status = 'submitted' "
            "AND order_verified_at IS NULL"
        ).fetchall()


def mark_order_verified(position_id: int) -> None:
    """Record that a position's order_id has been checked against Kalshi's real order
    history — regardless of the result, so a flagged ghost isn't re-alerted every scan."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE positions SET order_verified_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), position_id),
        )


# ── Closing Line Value ──────────────────────────────────────────────────────────

def get_positions_pending_closing_lines(max_attempts: int = 3) -> list[sqlite3.Row]:
    """Closed positions still missing closing-line data, under the retry cap."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM positions
            WHERE status = 'closed'
              AND consensus_close_prob IS NULL
              AND closing_line_attempts < ?
            """,
            (max_attempts,),
        ).fetchall()


def set_closing_lines(
    position_id: int,
    kalshi_close_price: float | None,
    consensus_close_prob: float | None,
) -> None:
    """Record closing-line values for a settled position and bump the attempt count."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE positions
            SET kalshi_close_price = ?, consensus_close_prob = ?,
                closing_line_attempts = closing_line_attempts + 1
            WHERE id = ?
            """,
            (kalshi_close_price, consensus_close_prob, position_id),
        )


def log_book_probabilities(entries: list[dict]) -> None:
    """
    Persist a long-lived copy of every scanned candidate that had both a Kalshi
    price and a sportsbook consensus (kalshi_side is only set once a side has
    been resolved -- see core/value_detector.py::_log()). Unlike scan_log (wiped
    every scan), these rows accumulate indefinitely so book accuracy can
    eventually be tested against a real, less selection-biased sample than just
    the candidates that became bets.
    """
    rows = [e for e in entries
            if e.get("consensus_prob") is not None and e.get("kalshi_side") is not None]
    if not rows:
        return
    with get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO book_probability_log
                (scanned_at, sport, bet_type, team_name, threshold, kalshi_ticker,
                 kalshi_side, consensus_prob, bookmaker_count, bookmakers_json, commence_time)
            VALUES
                (:scanned_at, :sport, :bet_type, :team_name, :threshold, :kalshi_ticker,
                 :kalshi_side, :consensus_prob, :bookmaker_count, :bookmakers_json, :commence_time)
            """,
            rows,
        )


def get_pending_book_probability_outcomes(max_attempts: int = 5) -> list[sqlite3.Row]:
    """Logged candidates whose real outcome hasn't been resolved yet, under the retry cap."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM book_probability_log
            WHERE actual_outcome IS NULL
              AND outcome_check_attempts < ?
            """,
            (max_attempts,),
        ).fetchall()


def set_book_probability_outcome(log_id: int, actual_outcome: float | None) -> None:
    """Record the real (Kalshi-resolved) outcome for a logged candidate and bump
    the attempt count -- same retry-capped shape as set_closing_lines above."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE book_probability_log
            SET actual_outcome = ?, outcome_check_attempts = outcome_check_attempts + 1
            WHERE id = ?
            """,
            (actual_outcome, log_id),
        )


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


def log_scan_results(scan_id: str, entries: list[dict]) -> None:
    """Write all candidates from one scan to scan_log, replacing any prior scan."""
    if not entries:
        return
    with get_connection() as conn:
        # Keep only the current scan — delete everything older
        conn.execute("DELETE FROM scan_log WHERE scan_id != ?", (scan_id,))
        conn.executemany(
            """
            INSERT INTO scan_log
                (scan_id, scanned_at, sport, home_team, away_team, team_name,
                 bet_type, threshold, kalshi_ticker, kalshi_spread, kalshi_volume,
                 kalshi_price, limit_price, consensus_prob, bookmaker_count, consensus_std,
                 edge, status, reason, maker_only, commence_time, bookmakers_json)
            VALUES
                (:scan_id, :scanned_at, :sport, :home_team, :away_team, :team_name,
                 :bet_type, :threshold, :kalshi_ticker, :kalshi_spread, :kalshi_volume,
                 :kalshi_price, :limit_price, :consensus_prob, :bookmaker_count, :consensus_std,
                 :edge, :status, :reason, :maker_only, :commence_time, :bookmakers_json)
            """,
            [{**e, "scan_id": scan_id, "bookmakers_json": e.get("bookmakers_json")} for e in entries],
        )


def get_last_scan() -> list[sqlite3.Row]:
    """Return all entries from the most recent scan, ordered by edge desc."""
    with get_connection() as conn:
        # Ensure table exists (dashboard may query before bot has ever run)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id         TEXT NOT NULL,
                scanned_at      TEXT NOT NULL,
                sport           TEXT NOT NULL,
                home_team       TEXT NOT NULL,
                away_team       TEXT NOT NULL,
                team_name       TEXT NOT NULL,
                bet_type        TEXT NOT NULL DEFAULT 'h2h',
                threshold       REAL,
                kalshi_ticker   TEXT,
                kalshi_spread   REAL,
                kalshi_volume   REAL,
                kalshi_price    REAL,
                consensus_prob  REAL,
                bookmaker_count INTEGER,
                consensus_std   REAL,
                edge            REAL,
                status          TEXT NOT NULL,
                reason          TEXT,
                commence_time   TEXT,
                bookmakers_json TEXT
            )
        """)
        # Migration: add bookmakers_json if missing from existing scan_log
        existing_scan = {row[1] for row in conn.execute("PRAGMA table_info(scan_log)").fetchall()}
        if "bookmakers_json" not in existing_scan:
            conn.execute("ALTER TABLE scan_log ADD COLUMN bookmakers_json TEXT")
        row = conn.execute(
            "SELECT scan_id FROM scan_log ORDER BY scanned_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        return conn.execute(
            """
            SELECT * FROM scan_log WHERE scan_id = ?
            ORDER BY
                CASE status
                    WHEN 'value'   THEN 0
                    WHEN 'blocked' THEN 1
                    WHEN 'no_edge' THEN 2
                    ELSE 3
                END,
                CASE WHEN edge IS NULL THEN 1 ELSE 0 END,
                edge DESC
            """,
            (row["scan_id"],),
        ).fetchall()


def get_scan_entry(entry_id: int) -> sqlite3.Row | None:
    """Return a single scan_log row by id."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM scan_log WHERE id = ?", (entry_id,)
        ).fetchone()


def get_top_opportunities(limit: int = 50) -> list[sqlite3.Row]:
    """Most recent detected opportunities, newest first."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM opportunities ORDER BY detected_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ── API Credits ───────────────────────────────────────────────────────────────

# Module-level state: track used_total at scan start to compute per-scan delta
_scan_start_used: int | None = None


def mark_scan_start() -> None:
    """Call at the beginning of each scan to capture the baseline credit count."""
    global _scan_start_used
    row = get_api_credits()
    _scan_start_used = row["used_total"] if row and row["used_total"] is not None else None


def update_api_credits(used: int | None, remaining: int | None) -> None:
    """Upsert the latest credit snapshot (called after every Odds API request)."""
    global _scan_start_used
    used_this_scan = None
    if used is not None and _scan_start_used is not None:
        used_this_scan = used - _scan_start_used
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_credits (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at    TEXT NOT NULL,
                used_total     INTEGER,
                remaining      INTEGER,
                used_this_scan INTEGER
            )
        """)
        conn.execute(
            """
            INSERT INTO api_credits (recorded_at, used_total, remaining, used_this_scan)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.utcnow().isoformat(), used, remaining, used_this_scan),
        )


def update_bot_heartbeat() -> None:
    """Update the bot's last-active timestamp. Called on every scan cycle, even skipped ones."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_heartbeat (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                last_seen  TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO bot_heartbeat (id, last_seen) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen
        """, (datetime.utcnow().isoformat(),))


def get_bot_heartbeat() -> str | None:
    """Return the last-active timestamp string, or None if never set."""
    with get_connection() as conn:
        try:
            row = conn.execute("SELECT last_seen FROM bot_heartbeat WHERE id = 1").fetchone()
            return row["last_seen"] if row else None
        except Exception:
            return None


def get_api_credits() -> sqlite3.Row | None:
    """Return the most recent credit snapshot."""
    with get_connection() as conn:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_credits (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at    TEXT NOT NULL,
                    used_total     INTEGER,
                    remaining      INTEGER,
                    used_this_scan INTEGER
                )
            """)
            return conn.execute(
                "SELECT * FROM api_credits ORDER BY recorded_at DESC LIMIT 1"
            ).fetchone()
        except Exception:
            return None
