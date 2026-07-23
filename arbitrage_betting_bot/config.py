"""
Central configuration. All tunable parameters live here.
Fill in your API keys in .env.
"""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────────────
ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
KALSHI_API_KEY: str = os.getenv("KALSHI_API_KEY", "")
KALSHI_API_EMAIL: str = os.getenv("KALSHI_API_EMAIL", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# ── Bankroll ──────────────────────────────────────────────────────────────────
BANKROLL: float = float(os.getenv("BANKROLL", "1000"))

# ── Risk Management ───────────────────────────────────────────────────────────
KELLY_FRACTION: float = 0.25          # Use quarter-Kelly to reduce variance
MIN_BET_DOLLARS: float = 0.5          # Absolute floor — Kalshi minimum is ~$0.01 per contract
MAX_BET_DOLLARS: float = 100.0        # Hard dollar cap per bet
MAX_PCT_BANKROLL: float = 0.05        # Max 5% of bankroll per single bet
MAX_TOTAL_EXPOSURE_PCT: float = 0.30  # Max 30% of bankroll deployed at once
MAX_SPORT_EXPOSURE_PCT: float = 0.15  # Max 15% of bankroll in one sport
MAX_OPEN_POSITIONS: int = 10          # Max simultaneous open positions — exposure % is the primary gate
MAX_DAILY_CAPITAL_RISK_PCT: float = 0.30  # Max % of bankroll staked in new positions per calendar day (UTC)

# ── Trailing Stop (mid-position exit risk management) ──────────────────────────
# Kalshi has no native stop/conditional order type — this is simulated by polling
# price each scan (piggybacked on auto_settle's existing per-position market fetch)
# and placing a real closing order once price retraces to the trailing level.
# Master switch — defaults off. Validate in paper mode before enabling live.
ENABLE_TRAILING_STOP: bool = os.getenv("ENABLE_TRAILING_STOP", "false").lower() == "true"
TRAILING_STOP_ARM_MOVE: float = 0.10        # min favorable move (price units) before the stop arms
TRAILING_STOP_LOCK_FRACTION: float = 0.20   # fraction of the move-from-entry protected once armed
TRAILING_STOP_ORDER_TIMEOUT_SECONDS: int = 30  # how long the closing GTC order rests before retrying next scan

# ── Scheduling ────────────────────────────────────────────────────────────────
# Variable-frequency polling: each sport is fetched at a rate based on its
# nearest upcoming game. Sports with no game within 1 hour use the default
# 45-minute interval; sports near game time are fetched more often.
POLL_INTERVAL_DEFAULT_SECONDS: int   = int(os.getenv("POLL_INTERVAL_DEFAULT_SECONDS",  "2700"))  # 45 min — baseline
POLL_INTERVAL_PRE_GAME_SECONDS: int  = int(os.getenv("POLL_INTERVAL_PRE_GAME_SECONDS",  "600"))  # 10 min — within 1 h
POLL_INTERVAL_NEAR_GAME_SECONDS: int = int(os.getenv("POLL_INTERVAL_NEAR_GAME_SECONDS", "120"))  # 2 min  — within 30 min
PRE_GAME_THRESHOLD_HOURS: int        = int(os.getenv("PRE_GAME_THRESHOLD_HOURS",    "1"))
NEAR_GAME_THRESHOLD_MINUTES: int     = int(os.getenv("NEAR_GAME_THRESHOLD_MINUTES", "30"))
# Back-compat alias (used by --once path and any external tooling)
POLL_INTERVAL_SECONDS: int = POLL_INTERVAL_DEFAULT_SECONDS

# ── Sports to Monitor ─────────────────────────────────────────────────────────
# Full list: https://the-odds-api.com/sports-odds-data/sports-apis.html
SPORTS: list[str] = [
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_usa_mls",
    "soccer_epl",
    "soccer_uefa_champs_league",
]

# ── Odds API ──────────────────────────────────────────────────────────────────
ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
ODDS_API_REGIONS: str = "us,eu"  # eu adds Pinnacle + sharp exchanges at no extra credit cost
ODDS_API_MARKETS: str = "h2h"          # default fallback
ODDS_API_ODDS_FORMAT: str = "american"

# Which Odds API market types to fetch per sport.
# Multiple types can be comma-separated (one API call per sport).
SPORT_MARKETS: dict[str, str] = {
    "basketball_nba":              "h2h,totals,spreads",
    "baseball_mlb":                "h2h,totals,spreads",
    "icehockey_nhl":               "h2h,totals,spreads",
    "soccer_usa_mls":              "h2h,totals,spreads",
    "soccer_epl":                  "h2h,totals,spreads",
    "soccer_uefa_champs_league":   "h2h,totals,spreads",
    # BTTS excluded — not available in us region from Odds API
    # alternate_totals/alternate_spreads cover all the non-main lines that
    # Kalshi lists (e.g. 7.5, 9.5, 10.5 in addition to the main 8.5 line)
}

# ── Kalshi ────────────────────────────────────────────────────────────────────
KALSHI_API_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"

# ── Market Matching ───────────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD: int = 80       # Minimum rapidfuzz score (0-100)

# ── De-vig Method ──────────────────────────────────────────────────────────────
# "shin" corrects for favorite-longshot bias (Shin 1992/1993) — proportional
# normalization misprices heavy favorites/underdogs. "proportional" is the old
# simple-normalization method, kept as an instant revert switch since this
# changes every edge calculation in the bot.
DEVIG_METHOD: str = os.getenv("DEVIG_METHOD", "shin")

# ── Opportunity Quality Filters ───────────────────────────────────────────────
# Tiered by bet type: H2H moneylines have deep sportsbook coverage; totals,
# spreads, and soccer draw markets are typically covered by fewer books, so a
# single global threshold either starves the thin markets or under-protects
# the deep ones. All tiers currently hold the same values as the old global
# constants (min_bookmaker_count=2, max_kalshi_spread=0.05, min_kalshi_volume=0,
# high_uncertainty_std=0.04/min_books=4) — this is a structural change, not a
# threshold change. Once the calibration dashboard has enough settled bets to
# show which tiers are over/under-performing, tune them independently here.
_DEFAULT_QUALITY_FILTER: dict = {
    "min_bookmaker_count": 2,
    "max_kalshi_spread": 0.05,
    "min_kalshi_volume": 0.0,
    "high_uncertainty_std": 0.04,
    "high_uncertainty_min_books": 4,
}
QUALITY_FILTERS: dict[str, dict] = {
    "h2h":    dict(_DEFAULT_QUALITY_FILTER),
    "totals": dict(_DEFAULT_QUALITY_FILTER),
    "spread": dict(_DEFAULT_QUALITY_FILTER),
    "draw":   dict(_DEFAULT_QUALITY_FILTER),  # soccer 3-way TIE market
}


def quality_filters(bet_type: str, is_draw: bool = False) -> dict:
    """Return the quality-filter thresholds for a given bet type."""
    if is_draw:
        return QUALITY_FILTERS["draw"]
    return QUALITY_FILTERS.get(bet_type, _DEFAULT_QUALITY_FILTER)


# Kalshi charges a real fee whenever an order crosses the spread at placement — not
# a fixed 0%/maker-only rate as originally assumed here. The actual fee per fill is
# read directly from Kalshi's own order record at fill time (see
# execution/kalshi_executor.py::_actual_fee_dollars) and stored on the position
# (entry_fee_paid), rather than estimated with a formula — a formula got this wrong
# before. Execution is two-step: (1) GTC at mid, adaptive timeout; (2) GTC at ask,
# short timeout — step 2 crosses the book by design and often incurs a real fee.
LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS: int = 600   # step 1 — game > 1 hour away
LIMIT_ORDER_TIMEOUT_PRE_GAME_SECONDS: int = 300  # step 1 — within 1 hour
LIMIT_ORDER_TIMEOUT_NEAR_GAME_SECONDS: int = 120 # step 1 — within 30 minutes
LIMIT_ORDER_ASK_TIMEOUT_SECONDS: int = 30        # step 2 — GTC at ask (short; ask should fill fast)
FAILED_BET_COOLDOWN_SECONDS: int = int(os.getenv("FAILED_BET_COOLDOWN_SECONDS", "10800"))  # 3 hours
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.015"))  # Minimum NET edge after Kalshi taker fee

# Pre-trade fee ESTIMATE, used only for the edge gate and Kelly sizing — before a bet
# is placed we don't yet know whether it'll fill via step 1 (mid, maker, ~0% fee) or
# step 2 (ask, crosses the book, real taker fee). Assume the worst case (taker), same
# "size for the guaranteed floor" philosophy already used for the ask-price edge check.
# This is NOT used at settlement — that uses the real fee actually charged, captured
# per-fill (see execution/kalshi_executor.py::_actual_fee_dollars).
KALSHI_TAKER_FEE_RATE_ESTIMATE: float = 0.07

# ── Notifications ─────────────────────────────────────────────────────────────
PUSHOVER_USER_KEY: str  = os.getenv("PUSHOVER_USER_KEY", "")   # From pushover.net account page
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")  # From pushover.net app creation

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_USERNAME: str = os.getenv("DASHBOARD_USERNAME", "")  # Required username; empty = any username accepted
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")  # HTTP Basic Auth; empty = no auth (local dev)
DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "")            # e.g. http://167.172.148.64:5000 — shown as link in Pushover
