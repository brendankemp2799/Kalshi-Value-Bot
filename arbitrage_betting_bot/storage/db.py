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
from datetime import date, datetime, timedelta, timezone
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

            -- Persists each sport's last-fetch time across process restarts so a
            -- redeploy doesn't force an immediate full re-fetch of every in-season
            -- sport regardless of how recently it was actually polled -- the
            -- in-memory last_fetched dict in main.py's tick loop used to reset to
            -- empty on every restart. See config.STARTUP_REFETCH_SKIP_WINDOW_SECONDS.
            CREATE TABLE IF NOT EXISTS sport_poll_state (
                sport            TEXT PRIMARY KEY,
                last_fetched_at  REAL NOT NULL
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

            -- Why the market maker did or didn't quote each candidate on the most
            -- recent MM tick. The directional strategy has written every candidate
            -- and its rejection reason to scan_log since the beginning; MM had no
            -- equivalent, and because every rejection path in
            -- execution/market_maker.py was logger.debug (off in production), a
            -- tick that evaluated ~60 candidates and quoted 1 produced no record
            -- of any kind. This is that record.
            --
            -- Holds ONE tick only, replaced wholesale each time (same pattern as
            -- scan_log): the MM tick runs every MM_INTERVAL_SECONDS=30s, so
            -- retaining history here would write ~170k rows/day to answer a
            -- question ("what is MM doing right now, and why") that is about the
            -- present. Fills are the durable record and they already go to
            -- `positions` with strategy='market_making'.
            CREATE TABLE IF NOT EXISTS mm_decision_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                tick_id         TEXT NOT NULL,
                decided_at      TEXT NOT NULL,
                sport           TEXT,
                kalshi_ticker   TEXT NOT NULL,
                team_name       TEXT,
                bet_type        TEXT,
                kalshi_bid      REAL,
                kalshi_ask      REAL,
                kalshi_spread   REAL,
                kalshi_volume   REAL,
                consensus_prob  REAL,
                bookmaker_count INTEGER,
                consensus_std   REAL,
                yes_quote       REAL,   -- price we'd buy YES at (NULL if not quoting)
                no_quote        REAL,   -- price we'd buy NO at
                net_per_pair    REAL,   -- expected profit per matched pair, after fees
                clip_dollars    REAL,
                contracts       INTEGER,
                action          TEXT NOT NULL,  -- placed | kept | rejected | cancelled
                reason          TEXT NOT NULL
            );

            -- Markets skipped because we could not tell WHICH TEAM they refer to.
            -- Added 2026-08-18 after position #930: "Chicago WS" scored identically
            -- against both Chicago Cubs and Chicago White Sox, the tie-break silently
            -- picked home, and the bot bought the opposite team (see
            -- core/value_detector.py::_sb_team_match). We now refuse those matches --
            -- but a refusal is a LOST OPPORTUNITY, not a fix, so each one is recorded
            -- here to be resolved later (usually by teaching the matcher an alias).
            --
            -- Deduplicated on (context, kalshi_ticker, kalshi_name) with a counter,
            -- because the same fixture is rescanned every cycle: a single ambiguous
            -- game would otherwise write ~100 identical rows a day. first_seen/
            -- last_seen/occurrences give the same information in one row.
            CREATE TABLE IF NOT EXISTS ambiguous_match_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                context       TEXT NOT NULL,   -- spread_covering_team | h2h_yes_team
                kalshi_ticker TEXT NOT NULL,
                kalshi_name   TEXT NOT NULL,   -- the name we could not resolve
                sport         TEXT,
                bet_type      TEXT,
                home_team     TEXT NOT NULL,
                away_team     TEXT NOT NULL,
                home_score    REAL,            -- what each side scored, so a fix can
                away_score    REAL,            -- be checked against the real numbers
                first_seen    TEXT NOT NULL,
                last_seen     TEXT NOT NULL,
                occurrences   INTEGER NOT NULL DEFAULT 1,
                resolved      INTEGER NOT NULL DEFAULT 0,  -- set once the matcher handles it
                UNIQUE(context, kalshi_ticker, kalshi_name)
            );

            -- One row PER DAY of market-making activity, incremented on every tick.
            -- mm_decision_log deliberately holds only the latest tick, which answers
            -- "what is MM doing right now" but cannot answer "has MM quoted at all
            -- this week" -- the question that actually decides whether the strategy
            -- is viable. A per-tick history would be ~2,880 rows/day for that; a
            -- daily rollup is one, and is all the weekly review needs.
            CREATE TABLE IF NOT EXISTS mm_daily_stats (
                day           TEXT PRIMARY KEY,   -- UTC date
                ticks         INTEGER NOT NULL DEFAULT 0,
                candidates    INTEGER NOT NULL DEFAULT 0,
                quoted        INTEGER NOT NULL DEFAULT 0,   -- candidate-quotes, not legs
                legs_placed   INTEGER NOT NULL DEFAULT 0,
                legs_kept     INTEGER NOT NULL DEFAULT 0,
                fills         INTEGER NOT NULL DEFAULT 0,
                reasons_json  TEXT                          -- {reason: count} for the day
            );

            -- Every DK-scaled player-prop estimate core/value_detector.py's
            -- scaled_alternate_diagnostics() fallback produced (2026-08-24), win or
            -- lose, bet or not -- the shadow-mode calibration record ChatGPT's review
            -- of the feature recommended before trusting it with capital: "run it in
            -- shadow mode first and empirically measure its calibration... the most
            -- important question is how prediction error changes with distance from
            -- the Pinnacle anchor." Written for BOTH sides of EVERY rung a scaled
            -- estimate priced (see _record_dk_shadow()), not just the ones that
            -- cleared the edge bar, so calibration is measured against the full
            -- evaluated population rather than a selection-biased subset.
            --
            -- Resolved the same way book_probability_log is (see
            -- execution/auto_settle.py::_backfill_dk_scaled_outcomes()): Kalshi's own
            -- market-resolution endpoint, no extra Odds API calls.
            CREATE TABLE IF NOT EXISTS dk_scaled_shadow_log (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id                TEXT,
                scanned_at             TEXT NOT NULL,
                sport                  TEXT NOT NULL,
                home_team              TEXT NOT NULL,
                away_team              TEXT NOT NULL,
                participant            TEXT NOT NULL,
                market_key             TEXT NOT NULL,
                kalshi_ticker          TEXT NOT NULL,
                kalshi_side            TEXT NOT NULL,   -- yes | no
                target_point           REAL,
                anchor_point           REAL,
                distance               REAL,            -- target_point - anchor_point
                anchor_fair_prob       REAL,            -- Pinnacle's de-vigged prob at its own point
                anchor_raw_prob        REAL,            -- alt book's raw (vigged) prob at that SAME point
                target_raw_prob        REAL,            -- alt book's raw prob at the target point
                scaling_ratio          REAL,            -- anchor_fair_prob / anchor_raw_prob
                scaled_prob            REAL,            -- our estimate at the target point
                kalshi_price           REAL,
                edge                   REAL,
                would_bet              INTEGER NOT NULL DEFAULT 0,  -- cleared the edge/spread gates
                status                 TEXT,
                reason                 TEXT,
                commence_time          TEXT,
                position_id            INTEGER,         -- set only if DK_SCALED_SHADOW_MODE was off
                actual_outcome         REAL,            -- 1.0 = this side resolved YES-true, 0.0 = false
                outcome_check_attempts INTEGER NOT NULL DEFAULT 0
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
            ("close_reason", "ALTER TABLE positions ADD COLUMN close_reason TEXT"),
            ("entry_fee_paid", "ALTER TABLE positions ADD COLUMN entry_fee_paid REAL NOT NULL DEFAULT 0.0"),
            ("order_verified_at", "ALTER TABLE positions ADD COLUMN order_verified_at TEXT"),
            ("strategy", "ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'value_edge'"),
            ("closing_line_last_attempt_at", "ALTER TABLE positions ADD COLUMN closing_line_last_attempt_at TEXT"),
            # Whether sizing treated this as a maker_only opportunity (priced at the
            # fee-free mid) or a normal one (priced at the ask with the taker fee).
            # This is the single largest driver of bet size — a maker_only bet is sized
            # with fee_rate=0 and can come out ~2x larger than a HIGHER-edge taker bet
            # — but it was not stored, so past sizing could not be reconstructed
            # (only 44 of 87 live bets could be reproduced on 2026-08-14). NULL for
            # rows written before this column existed. Note fill_type is NOT a
            # substitute: that is the post-fill fee classification, not the pre-trade
            # sizing assumption.
            ("maker_only", "ALTER TABLE positions ADD COLUMN maker_only INTEGER"),
            # Entry-time sportsbook consensus probability for OUR side (2026-08-25).
            # Without this, "book CLV" (did the sharp market itself move toward or
            # away from our side after we bet) can't be computed directly -- edge
            # alone doesn't reconstruct it, since edge is measured against different
            # prices depending on maker_only (mid vs ask). consensus_close_prob
            # already exists (captured post-settlement, same de-vig convention) --
            # this is its entry-time counterpart. See core/clv_analytics.py.
            ("consensus_prob", "ALTER TABLE positions ADD COLUMN consensus_prob REAL"),
        ]:
            if col not in existing:
                conn.execute(ddl)
                logger.debug("Migration: added positions.%s", col)

        # positions carried NO indexes at all, so every lookup was a full table scan --
        # 4.7ms each on 1,392 rows, and growing linearly with the table. Rule 0 asks
        # "have we ever held this ticker?" once per candidate, several hundred times a
        # scan, which is what made the cost worth paying attention to.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_positions_ticker "
            "ON positions(market_ticker, is_paper)"
        )

        # book_probability_log migrations -- widening it from a narrow book-
        # calibration table into a general archive of every scanned candidate
        # (passed or placed), so future experiments aren't limited to the ~40
        # candidates that became real bets. See core/value_detector.py::_log()
        # for where these values already exist per-candidate, and
        # research/experiments/2026-08-11-book-weight-validation.md for why
        # this table exists at all.
        bpl_existing = {row[1] for row in conn.execute("PRAGMA table_info(book_probability_log)").fetchall()}
        for col, ddl in [
            ("home_team",     "ALTER TABLE book_probability_log ADD COLUMN home_team TEXT"),
            ("away_team",     "ALTER TABLE book_probability_log ADD COLUMN away_team TEXT"),
            ("kalshi_price",  "ALTER TABLE book_probability_log ADD COLUMN kalshi_price REAL"),
            ("kalshi_spread", "ALTER TABLE book_probability_log ADD COLUMN kalshi_spread REAL"),
            ("kalshi_volume", "ALTER TABLE book_probability_log ADD COLUMN kalshi_volume REAL"),
            ("limit_price",   "ALTER TABLE book_probability_log ADD COLUMN limit_price REAL"),
            ("edge",          "ALTER TABLE book_probability_log ADD COLUMN edge REAL"),
            ("status",        "ALTER TABLE book_probability_log ADD COLUMN status TEXT"),
            ("reason",        "ALTER TABLE book_probability_log ADD COLUMN reason TEXT"),
            ("maker_only",    "ALTER TABLE book_probability_log ADD COLUMN maker_only INTEGER NOT NULL DEFAULT 0"),
            ("scan_id",       "ALTER TABLE book_probability_log ADD COLUMN scan_id TEXT"),
            ("position_id",   "ALTER TABLE book_probability_log ADD COLUMN position_id INTEGER"),
        ]:
            if col not in bpl_existing:
                conn.execute(ddl)
                logger.debug("Migration: added book_probability_log.%s", col)


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
    maker_only: bool | None = None,
    consensus_prob: float | None = None,
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
                 entry_fee_paid, strategy, maker_only, consensus_prob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                None if maker_only is None else (1 if maker_only else 0),
                consensus_prob,
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


def strategies_ever_filled_on(ticker: str, is_paper: bool = False) -> set[str]:
    """Which strategies have ever had a FILLED position on this ticker.

    get_open_positions() answers "do we hold this now", which is a different question
    and the wrong one for re-entry. A Kalshi game ticker names one fixture at one start
    time and never recurs, so a filled position on it is permanent evidence that we
    already took this bet -- whether or not it is still open.

    Between 2026-08-21 and 08-22 the bot bought KXMLBBTTS-26AUG22SJMIN-BTTS four times
    for -$3.91: each stop-loss closed the position, which freed the ticker, and the
    next scan re-bought the same market at the same price because the edge had not
    moved. Empty set means never filled; failed attempts are Rule 0b's business, not
    this one.
    """
    with get_connection() as conn:
        return {
            r[0] for r in conn.execute(
                "SELECT DISTINCT strategy FROM positions "
                "WHERE market_ticker = ? AND is_paper = ? "
                "AND execution_status = 'submitted'",
                (ticker, 1 if is_paper else 0),
            )
        }


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


def settle_position_at_price(position_id: int, settlement_price: float) -> float:
    """
    Mark a position as closed at an arbitrary settlement price — for Kalshi markets
    that resolve to something other than a clean win/loss/void. Discovered 2026-08-28:
    a binary player-prop market whose underlying condition can't cleanly resolve (e.g.
    the player is scratched or gets no qualifying plate appearance) settles at Kalshi's
    "fair market price" instead — the market's `result` field reads "scalar" and the
    payout lives in `settlement_value_dollars`, not in yes/no. settle_position() only
    recognized won/lost/void, so these positions sat 'open' forever with pnl never
    computed — see execution/reconciliation.py, which is what caught this (Kalshi's
    portfolio showed 0 contracts against 4 positions we still tracked as open, one for
    5+ days). Generalizes the same formula settle_position() uses (won = price 1.0,
    lost = price 0.0, void = price == entry_price) to an arbitrary settlement value.
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
        pnl = contracts * (settlement_price - entry_price) - entry_fee_paid
        conn.execute(
            """
            UPDATE positions
            SET status = 'closed', pnl = ?, settled_at = ?
            WHERE id = ?
            """,
            (pnl, datetime.utcnow().isoformat(), position_id),
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


def position_exists_for_order_id(order_id: str) -> bool:
    """
    Whether a position already exists for this exact Kalshi order_id. Guards
    execution/market_maker.py's fill-check against double-recording a fill it's
    already seen — normally impossible (each resting order is checked exactly
    once, right before being cancelled or fully consumed), but became possible
    once market_maker.py started recovering resting-order state from Kalshi
    after a restart (see _sync_resting_quotes_from_kalshi()): a recovered order
    that was already manually or automatically backfilled must not be
    re-recorded just because its historical fill_count_fp is read again.
    """
    if not order_id:
        return False
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM positions WHERE order_id = ? LIMIT 1", (order_id,)
        ).fetchone()
        return row is not None


# ── Closing Line Value ──────────────────────────────────────────────────────────

def get_positions_pending_closing_lines(
    max_attempts: int = 3, cooldown_hours: float = 2.0,
) -> list[sqlite3.Row]:
    """
    Closed positions still missing closing-line data, under the retry cap and
    past the retry cooldown. The cooldown matters because each attempt costs a
    real Odds API historical-odds call (~10 credits, more than a full live
    scan) -- without it, a position becomes eligible again on every due-scan
    (as often as every 2 minutes during near-game polling), which can burn
    through the whole retry cap in minutes on a historical snapshot that
    likely isn't posted yet. Real settled positions have already exhausted
    all 3 attempts with nothing to show for it (see git history 2026-08-11).
    """
    cutoff = (datetime.utcnow() - timedelta(hours=cooldown_hours)).isoformat()
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM positions
            WHERE status = 'closed'
              AND consensus_close_prob IS NULL
              AND closing_line_attempts < ?
              AND (closing_line_last_attempt_at IS NULL OR closing_line_last_attempt_at <= ?)
            """,
            (max_attempts, cutoff),
        ).fetchall()


def set_closing_lines(
    position_id: int,
    kalshi_close_price: float | None,
    consensus_close_prob: float | None,
) -> None:
    """Record closing-line values for a settled position, bump the attempt
    count, and stamp the attempt time (win or lose) so the retry cooldown in
    get_positions_pending_closing_lines() has something to measure against."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE positions
            SET kalshi_close_price = ?, consensus_close_prob = ?,
                closing_line_attempts = closing_line_attempts + 1,
                closing_line_last_attempt_at = ?
            WHERE id = ?
            """,
            (kalshi_close_price, consensus_close_prob, datetime.utcnow().isoformat(), position_id),
        )


def log_book_probabilities(scan_id: str, entries: list[dict]) -> None:
    """
    Persist a long-lived copy of every scanned candidate that had both a Kalshi
    price and a sportsbook consensus (kalshi_side is only set once a side has
    been resolved -- see core/value_detector.py::_log()). Unlike scan_log (wiped
    every scan), these rows accumulate indefinitely -- not just for book-accuracy
    research (the table's original purpose), but as a general archive of every
    evaluated candidate (edge, market conditions, why it was rejected or placed)
    for future experimentation against a real, less selection-biased sample than
    just the candidates that became bets.

    scan_id is stamped onto every row (not part of the entry dicts themselves --
    only the caller in main.py knows it) so link_book_probability_to_position()
    can later find the exact row for a candidate that became a real bet.
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
                 kalshi_side, consensus_prob, bookmaker_count, bookmakers_json, commence_time,
                 home_team, away_team, kalshi_price, kalshi_spread, kalshi_volume,
                 limit_price, edge, status, reason, maker_only, scan_id)
            VALUES
                (:scanned_at, :sport, :bet_type, :team_name, :threshold, :kalshi_ticker,
                 :kalshi_side, :consensus_prob, :bookmaker_count, :bookmakers_json, :commence_time,
                 :home_team, :away_team, :kalshi_price, :kalshi_spread, :kalshi_volume,
                 :limit_price, :edge, :status, :reason, :maker_only, :scan_id)
            """,
            [{**row, "scan_id": scan_id} for row in rows],
        )


def link_book_probability_to_position(
    scan_id: str, kalshi_ticker: str, team_name: str, position_id: int,
) -> None:
    """
    Stamp position_id onto the book_probability_log row for a candidate that
    just became a real bet, so realized economics (stake, fill_type, pnl,
    closing lines) can be read via a join to positions instead of duplicated
    here. (scan_id, kalshi_ticker, team_name) is the same composite key
    main.py::_update_scan_log() already relies on to find one candidate's row
    within a single scan -- exact, no timestamp-matching needed.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE book_probability_log
            SET position_id = ?
            WHERE scan_id = ? AND kalshi_ticker = ? AND team_name = ?
            """,
            (position_id, scan_id, kalshi_ticker, team_name),
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


def get_probability_movement(kalshi_ticker: str, kalshi_side: str) -> list[sqlite3.Row]:
    """
    All book_probability_log readings for one specific (ticker, side) outcome,
    oldest first -- the raw consensus_prob time series a line-movement/steam
    signal is computed from. kalshi_side is part of the key because a single
    ticker can have both a yes leg and a no leg logged separately (e.g. Over/
    Under on the same totals ticker -- see core/value_detector.py::_log()).
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM book_probability_log
            WHERE kalshi_ticker = ? AND kalshi_side = ?
            ORDER BY scanned_at ASC
            """,
            (kalshi_ticker, kalshi_side),
        ).fetchall()


# ── DK-scaled shadow log ─────────────────────────────────────────────────────

_DK_SHADOW_COLUMNS = (
    "sport", "home_team", "away_team", "participant", "market_key", "kalshi_ticker",
    "kalshi_side", "target_point", "anchor_point", "distance", "anchor_fair_prob",
    "anchor_raw_prob", "target_raw_prob", "scaling_ratio", "scaled_prob",
    "kalshi_price", "edge", "would_bet", "status", "reason", "commence_time",
)


def log_dk_scaled_estimates(scan_id: str, entries: list[dict]) -> None:
    """Persist every DK-scaled player-prop estimate from one scan (see
    core/value_detector.py::_record_dk_shadow). Unlike scan_log, these accumulate
    indefinitely -- they are the calibration dataset, not a live snapshot."""
    if not entries:
        return
    cols = ", ".join(_DK_SHADOW_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _DK_SHADOW_COLUMNS)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.executemany(
            f"INSERT INTO dk_scaled_shadow_log (scan_id, scanned_at, {cols}) "
            f"VALUES (:scan_id, :scanned_at, {placeholders})",
            [{**e, "scan_id": scan_id, "scanned_at": now} for e in entries],
        )


def get_pending_dk_scaled_outcomes(max_attempts: int = 5) -> list[sqlite3.Row]:
    """Logged DK-scaled estimates whose real outcome hasn't been resolved yet, under
    the retry cap -- same shape as get_pending_book_probability_outcomes()."""
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT * FROM dk_scaled_shadow_log
            WHERE actual_outcome IS NULL
              AND outcome_check_attempts < ?
            """,
            (max_attempts,),
        ).fetchall()


def set_dk_scaled_outcome(log_id: int, actual_outcome: float | None) -> None:
    """Record the real (Kalshi-resolved) outcome for one DK-scaled estimate and bump
    the attempt count -- see set_book_probability_outcome() for the same pattern."""
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE dk_scaled_shadow_log
            SET actual_outcome = ?, outcome_check_attempts = outcome_check_attempts + 1
            WHERE id = ?
            """,
            (actual_outcome, log_id),
        )


# ONE row per distinct opportunity, not per scan.
#
# core/value_detector.py::_detect_player_prop re-evaluates and re-logs every rung on
# EVERY scan from the moment it first appears until first pitch, so a single real
# opportunity produces 10-15 near-identical rows (measured: 206 rows from one scan
# on 2026-08-26, covering far fewer distinct rungs). Counting those raw does two bad
# things: it inflates `n` so the page looks validated long before it is, and it
# weights the Brier score toward rungs that merely sat around longer rather than
# toward independent observations.
#
# (kalshi_ticker, kalshi_side) is the opportunity key -- a Kalshi ticker names one
# rung of one player's ladder in one game and never recurs, and each side is a
# separate bet priced off its own number.
#
# Which of the duplicates survives mirrors what the bot would actually have DONE:
# `would_bet DESC` first (if any scan would have fired, that decision is the one
# being validated), then `scanned_at ASC` (the EARLIEST such scan -- the bot acts on
# first qualification, and storage/db.py::strategies_ever_filled_on then blocks
# re-entry on that ticker, so later re-evaluations would never have become trades).
# For a rung that never qualified, this keeps its first evaluation, which is the
# like-for-like comparison.
_DK_DEDUPED = """
    SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (
            PARTITION BY kalshi_ticker, kalshi_side
            ORDER BY would_bet DESC, scanned_at ASC, id ASC
        ) AS _rn
        FROM dk_scaled_shadow_log
    ) WHERE _rn = 1
"""


def get_dk_scaled_shadow_summary(min_distance: float = 0.0) -> dict:
    """Aggregate calibration stats for settled DK-scaled estimates.

    Returns overall Brier score / sample counts, plus a breakdown by distance-from-
    anchor bucket -- the single question the 2026-08-24 review said mattered most:
    "how does prediction error change with distance from the Pinnacle anchor."
    Buckets on abs(distance) since the direction (above/below the anchor) is not
    itself the hypothesis being tested here.

    Every count here is DEDUPLICATED to one row per (ticker, side) -- see _DK_DEDUPED
    for why raw row counts would badly overstate the sample.
    """
    with get_connection() as conn:
        try:
            rows = conn.execute(
                f"SELECT scaled_prob, actual_outcome, distance, would_bet, edge "
                f"FROM ({_DK_DEDUPED}) WHERE actual_outcome IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            return {"n": 0, "n_settled": 0, "brier": None, "buckets": [],
                    "n_would_bet": 0, "n_raw_rows": 0}

    n_settled = len(rows)
    with get_connection() as conn:
        n_total = conn.execute(f"SELECT COUNT(*) FROM ({_DK_DEDUPED})").fetchone()[0]
        n_would_bet = conn.execute(
            f"SELECT COUNT(*) FROM ({_DK_DEDUPED}) WHERE would_bet = 1"
        ).fetchone()[0]
        # Kept visible so the page can show how much re-scanning is behind the
        # deduped figure -- a large gap is normal, not a fault.
        n_raw_rows = conn.execute("SELECT COUNT(*) FROM dk_scaled_shadow_log").fetchone()[0]

    if n_settled == 0:
        return {"n": n_total, "n_settled": 0, "brier": None, "buckets": [],
                "n_would_bet": n_would_bet, "n_raw_rows": n_raw_rows}

    brier = sum((r["scaled_prob"] - r["actual_outcome"]) ** 2 for r in rows) / n_settled

    # Distance buckets: 0-0.5, 0.5-1.5, 1.5-3, 3+ rungs from the Pinnacle anchor.
    edges = [0.0, 0.5, 1.5, 3.0, float("inf")]
    buckets = []
    for lo, hi in zip(edges, edges[1:]):
        bucket_rows = [r for r in rows if lo <= abs(r["distance"] or 0.0) < hi]
        if not bucket_rows:
            buckets.append({"range": f"{lo:g}–{hi:g}" if hi != float("inf") else f"{lo:g}+",
                            "n": 0, "brier": None, "mean_error": None})
            continue
        n = len(bucket_rows)
        b_brier = sum((r["scaled_prob"] - r["actual_outcome"]) ** 2 for r in bucket_rows) / n
        mean_error = sum(r["scaled_prob"] - r["actual_outcome"] for r in bucket_rows) / n
        buckets.append({
            "range": f"{lo:g}–{hi:g}" if hi != float("inf") else f"{lo:g}+",
            "n": n, "brier": round(b_brier, 4), "mean_error": round(mean_error, 4),
        })

    return {
        "n": n_total, "n_settled": n_settled, "n_would_bet": n_would_bet,
        "brier": round(brier, 4), "buckets": buckets, "n_raw_rows": n_raw_rows,
    }


def get_dk_scaled_shadow_rows(limit: int = 200, settled_only: bool = False) -> list[sqlite3.Row]:
    """Most recent DK-scaled shadow rows, newest first, for the /dk-scaled dashboard
    table. settled_only restricts to rows with a resolved actual_outcome.

    Deduplicated to one row per (ticker, side) -- see _DK_DEDUPED. Without this the
    table shows the same rung 10-15 times in a row and the operator cannot tell how
    many distinct opportunities are actually behind it.
    """
    where = "WHERE actual_outcome IS NOT NULL" if settled_only else ""
    with get_connection() as conn:
        try:
            return conn.execute(
                f"SELECT * FROM ({_DK_DEDUPED}) {where} "
                f"ORDER BY scanned_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def get_dk_scaled_settled_rows() -> list[dict]:
    """Every settled DK-scaled shadow-log estimate, deduplicated to one row per
    (ticker, side) -- see _DK_DEDUPED. Feeds research/metrics.py::
    dk_scaled_shadow_backtest(), the "would betting these have been profitable"
    follow-up to get_dk_scaled_shadow_summary()'s calibration-only Brier score.

    Unlike get_dk_scaled_shadow_rows() (built for a paginated, most-recent-first
    dashboard table), this has no LIMIT -- a backtest needs the whole settled
    population, and this table is small enough (four figures as of 2026-08-26)
    that loading it all is not the book_probability_log OOM risk documented on
    load_scanned_candidates() above; that table is two orders of magnitude
    larger and never pruned, this one settles at the bot's trade-evaluation
    rate, not its raw-scan rate."""
    with get_connection() as conn:
        try:
            rows = conn.execute(
                f"SELECT kalshi_price, edge, distance, would_bet, actual_outcome, "
                f"sport, market_key FROM ({_DK_DEDUPED}) WHERE actual_outcome IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]


# ── Dashboard Queries ─────────────────────────────────────────────────────────

def get_all_positions(is_paper: bool = False) -> list[sqlite3.Row]:
    """All positions (open and closed) for one mode, newest first."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM positions WHERE is_paper = ? ORDER BY entered_at DESC",
            (1 if is_paper else 0,),
        ).fetchall()


def get_positions_for_clv_analytics(is_paper: bool = False) -> list[dict]:
    """Closed positions with the raw fields core/clv_analytics.py needs, oldest
    first (so weekly_clv_series doesn't need to re-sort). Returns plain dicts, not
    sqlite3.Row -- clv_analytics.compute_row() does dict unpacking (**pos) to build
    its output, which sqlite3.Row does not support directly."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT sport, bet_type, team_name, entered_at, commence_time,
                   market_price, kalshi_close_price, consensus_prob,
                   consensus_close_prob, pnl, stake, threshold, maker_only,
                   market_ticker
            FROM positions
            WHERE is_paper = ? AND status = 'closed'
            ORDER BY entered_at ASC
            """,
            (1 if is_paper else 0,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_qualifying_candidates_with_outcomes(is_paper: bool = False) -> list[dict]:
    """Every book_probability_log row that qualified as a value bet AND has a
    resolved real-world outcome -- the population counterfactual_backtest() in
    research/metrics.py needs to ask "would betting the whole qualifying pool
    (or a random subset of it) have done as well as the bot's composite-score
    ranking actually did." WHERE-filtered in SQL, not loaded-then-filtered --
    see load_scanned_candidates()'s docstring for why an unfiltered read of
    this table OOM-killed the production cron job.

    Filters: status = 'value' (every edge-allowed candidate gets status='value'
    regardless of whether it was actually placed -- see
    core/value_detector.py; position_id is what distinguishes "qualified and
    placed" from "qualified but skipped", e.g. blocked by the daily order cap,
    correlation rules, or exposure limits, or just not in that scan's top-5-by-
    composite-score -- NULL status is a distinct, legacy case: rows written
    before the 2026-08-11 schema widening, which never got a status at all and
    are already excluded by the edge IS NOT NULL filter below), actual_outcome
    IS NOT NULL (resolved -- see encoding note below), edge IS NOT NULL
    (pre-widening rows have no edge to size a hypothetical bet from).

    actual_outcome encoding (see execution/auto_settle.py::
    _backfill_book_probability_outcomes()): 1.0 = the side actually recorded
    in this row (kalshi_side) resolved TRUE, 0.0 = it resolved FALSE. A void
    market is written back as None (same as "not yet resolved" -- the backfill
    has no separate void encoding), so void rows are indistinguishable from
    pending ones and are correctly excluded by the actual_outcome IS NOT NULL
    filter either way. This lines up directly with storage/db.py::
    settle_position()'s win/loss formula (won: stake*(1-price)/price, lost:
    -stake) with price = kalshi_price -- a hypothetical bet on this row would
    have been a "won" if actual_outcome == 1.0, "lost" if == 0.0. Entry fees
    are NOT modeled here (these candidates were never filled, so there is no
    real fee to read, unlike settle_position's entry_fee_paid) -- callers
    computing hypothetical P&L should treat it as pre-fee.

    is_paper is accepted for interface consistency with get_all_positions() /
    get_positions_for_clv_analytics() above, but book_probability_log has no
    is_paper column -- every scan cycle logs candidates regardless of which
    mode the bot process was started in, and most rows never link to a
    position (position_id IS NULL) so there is no reliable way to attribute a
    candidate row to paper vs. live after the fact. It is therefore currently
    a no-op; left in the signature so a future caller that DOES need the
    distinction (e.g. by joining position_id to positions.is_paper for the
    minority of linked rows) has an obvious place to add it rather than
    silently mixing modes.

    ORDER BY id: not meaningful data, but pins row order so a caller sampling
    from this list (e.g. counterfactual_backtest()'s seeded random.sample)
    gets identical results on identical data, rather than relying on SQLite's
    unspecified default scan order happening to be stable.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT kalshi_price, edge, actual_outcome, position_id, sport,
                   bet_type, scanned_at
            FROM book_probability_log
            WHERE status = 'value' AND actual_outcome IS NOT NULL AND edge IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


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


_MM_DECISION_COLUMNS = (
    "sport", "kalshi_ticker", "team_name", "bet_type", "kalshi_bid", "kalshi_ask",
    "kalshi_spread", "kalshi_volume", "consensus_prob", "bookmaker_count",
    "consensus_std", "yes_quote", "no_quote", "net_per_pair", "clip_dollars",
    "contracts", "action", "reason",
)


def log_ambiguous_match(
    context: str,
    kalshi_ticker: str,
    kalshi_name: str,
    home_team: str,
    away_team: str,
    sport: str | None = None,
    bet_type: str | None = None,
    home_score: float | None = None,
    away_score: float | None = None,
) -> None:
    """
    Record a market we refused to bet because we could not tell which team it covers.

    Upserts on (context, kalshi_ticker, kalshi_name): the first sighting inserts, every
    later one bumps last_seen and occurrences. The same fixture is rescanned every
    cycle, so without that a single ambiguous game writes ~100 rows a day and the
    table stops being readable.

    Never raises. This is diagnostics on a live trading path -- a logging failure must
    not take down a scan. See the ambiguous_match_log DDL for why these are tracked.
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ambiguous_match_log
                    (context, kalshi_ticker, kalshi_name, sport, bet_type,
                     home_team, away_team, home_score, away_score,
                     first_seen, last_seen, occurrences)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(context, kalshi_ticker, kalshi_name) DO UPDATE SET
                    last_seen   = excluded.last_seen,
                    occurrences = occurrences + 1,
                    home_score  = COALESCE(excluded.home_score, home_score),
                    away_score  = COALESCE(excluded.away_score, away_score)
                """,
                (context, kalshi_ticker, kalshi_name, sport, bet_type,
                 home_team, away_team, home_score, away_score, now, now),
            )
    except Exception as e:  # pragma: no cover - diagnostics must never break a scan
        logger.warning("Could not log ambiguous match for %s: %s", kalshi_ticker, e)


def get_ambiguous_matches(include_resolved: bool = False,
                          days: int | None = None) -> list[sqlite3.Row]:
    """Ambiguous team matches still awaiting a matcher fix, most recent first."""
    sql = ("SELECT * FROM ambiguous_match_log "
           "WHERE (? OR resolved = 0)")
    params: list = [1 if include_resolved else 0]
    if days is not None:
        sql += " AND last_seen >= ?"
        params.append(
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    sql += " ORDER BY last_seen DESC"
    with get_connection() as conn:
        return conn.execute(sql, params).fetchall()


def resolve_ambiguous_match(match_id: int) -> None:
    """Mark one logged ambiguity as handled, so it drops out of the open list."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE ambiguous_match_log SET resolved = 1 WHERE id = ?", (match_id,))


def log_mm_decisions(tick_id: str, entries: list[dict]) -> None:
    """Write every candidate the market maker evaluated on one tick, replacing the
    prior tick's rows. See the mm_decision_log DDL in init_db() for why only one
    tick is retained. Missing keys default to NULL so callers can pass a partial
    dict for a candidate rejected before the later fields were computed."""
    if not entries:
        return
    cols = ", ".join(_MM_DECISION_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _MM_DECISION_COLUMNS)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM mm_decision_log WHERE tick_id != ?", (tick_id,))
        conn.executemany(
            f"INSERT INTO mm_decision_log (tick_id, decided_at, {cols}) "
            f"VALUES (:tick_id, :decided_at, {placeholders})",
            [
                {
                    **{c: None for c in _MM_DECISION_COLUMNS},
                    **e,
                    "tick_id": tick_id,
                    "decided_at": now,
                }
                for e in entries
            ],
        )


def record_mm_tick_stats(candidates: int, quoted: int, legs_placed: int,
                          legs_kept: int, fills: int, reasons: dict) -> None:
    """Fold one MM tick into today's rollup row. See the mm_daily_stats DDL for
    why this is a daily aggregate rather than per-tick history."""
    import json as _json
    day = datetime.now(timezone.utc).date().isoformat()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM mm_daily_stats WHERE day=?", (day,)).fetchone()
        merged: dict[str, int] = {}
        if row is not None:
            try:
                merged = _json.loads(row["reasons_json"] or "{}")
            except (ValueError, TypeError):
                merged = {}
        for k, v in (reasons or {}).items():
            merged[k] = merged.get(k, 0) + int(v)

        if row is None:
            conn.execute(
                "INSERT INTO mm_daily_stats (day, ticks, candidates, quoted, "
                "legs_placed, legs_kept, fills, reasons_json) VALUES (?,?,?,?,?,?,?,?)",
                (day, 1, candidates, quoted, legs_placed, legs_kept, fills,
                 _json.dumps(merged)),
            )
        else:
            conn.execute(
                "UPDATE mm_daily_stats SET ticks=ticks+1, candidates=candidates+?, "
                "quoted=quoted+?, legs_placed=legs_placed+?, legs_kept=legs_kept+?, "
                "fills=fills+?, reasons_json=? WHERE day=?",
                (candidates, quoted, legs_placed, legs_kept, fills,
                 _json.dumps(merged), day),
            )


def get_mm_daily_stats(days: int = 7) -> list[sqlite3.Row]:
    """Most recent `days` rollup rows, newest first. [] if MM has never ticked."""
    with get_connection() as conn:
        try:
            return conn.execute(
                "SELECT * FROM mm_daily_stats ORDER BY day DESC LIMIT ?", (days,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []


def get_mm_pairing(is_paper: bool = False) -> list[dict]:
    """
    Per-ticker paired vs UNPAIRED contract counts across open market-making fills.

    Market making is only near-riskless when both legs fill: a matched pair (1 YES
    + 1 NO) costs under $1 and pays exactly $1, so the outcome doesn't matter. A
    leg that fills alone is not market making at all — it is a naked directional
    position wearing a market-making label, exposed to the full $1 swing.

    This is not hypothetical. KXMLSGAME-26AUG19RSLDAL-RSL (reconciled 2026-08-15
    from Kalshi's fill history) took 9 NO fills against 1 YES fill: ONE matched
    pair worth +$0.04, and EIGHT naked contracts worth $3.84 of directional risk
    on a market the bot never intended to have an opinion about.

    Returns one dict per ticker with yes/no contract counts, matched pairs, the
    unpaired remainder, its dollar value, and which side is naked.
    """
    rows = get_open_positions(is_paper=is_paper)
    by_ticker: dict[str, dict] = {}
    for pos in rows:
        if pos["strategy"] != "market_making":
            continue
        t = pos["market_ticker"]
        price = pos["market_price"] or 0.0
        contracts = (pos["stake"] / price) if price else 0.0
        slot = by_ticker.setdefault(t, {"ticker": t, "yes": 0.0, "no": 0.0,
                                        "yes_cost": 0.0, "no_cost": 0.0})
        slot[pos["side"]] = slot.get(pos["side"], 0.0) + contracts
        slot[f"{pos['side']}_cost"] += pos["stake"] or 0.0

    out = []
    for t, s in by_ticker.items():
        paired = min(s["yes"], s["no"])
        unpaired = abs(s["yes"] - s["no"])
        naked_side = "yes" if s["yes"] > s["no"] else ("no" if s["no"] > s["yes"] else "")
        # Value the naked remainder at that side's average fill price.
        avg = 0.0
        if naked_side == "yes" and s["yes"]:
            avg = s["yes_cost"] / s["yes"]
        elif naked_side == "no" and s["no"]:
            avg = s["no_cost"] / s["no"]
        out.append({
            "ticker": t, "yes_contracts": s["yes"], "no_contracts": s["no"],
            "paired": paired, "unpaired": unpaired, "naked_side": naked_side,
            "unpaired_dollars": round(unpaired * avg, 2),
        })
    return sorted(out, key=lambda r: -r["unpaired_dollars"])


def get_last_mm_tick() -> list[sqlite3.Row]:
    """Every candidate from the most recent MM tick — quoted ones first, then the
    rejections. Returns [] if MM has never run (the dashboard may query before
    ENABLE_MARKET_MAKING has ever been on)."""
    with get_connection() as conn:
        try:
            return conn.execute(
                """
                SELECT * FROM mm_decision_log
                ORDER BY CASE action WHEN 'placed' THEN 0 WHEN 'kept' THEN 1
                                     WHEN 'cancelled' THEN 2 ELSE 3 END,
                         net_per_pair DESC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []


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


def get_last_fetched_at(sport: str) -> float | None:
    """Unix timestamp of the last successful Odds API fetch for this sport,
    persisted across process restarts -- see sport_poll_state in init_db()."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_fetched_at FROM sport_poll_state WHERE sport = ?", (sport,)
        ).fetchone()
        return row["last_fetched_at"] if row else None


def set_last_fetched_at(sport: str, timestamp: float) -> None:
    """Record when this sport was last fetched from the Odds API."""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sport_poll_state (sport, last_fetched_at) VALUES (?, ?)
            ON CONFLICT(sport) DO UPDATE SET last_fetched_at = excluded.last_fetched_at
            """,
            (sport, timestamp),
        )


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
