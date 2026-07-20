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
MIN_BET_DOLLARS: float = 1.0          # Absolute floor — Kalshi minimum is ~$0.01 per contract
MIN_BET_PCT: float = 0.01             # Minimum bet as % of bankroll (1%); scales with account size
MAX_BET_DOLLARS: float = 100.0        # Hard dollar cap per bet
MAX_PCT_BANKROLL: float = 0.05        # Max 5% of bankroll per single bet
MAX_TOTAL_EXPOSURE_PCT: float = 0.30  # Max 30% of bankroll deployed at once
MAX_SPORT_EXPOSURE_PCT: float = 0.15  # Max 15% of bankroll in one sport
MAX_OPEN_POSITIONS: int = 10          # Max simultaneous open positions — exposure % is the primary gate
MAX_DAILY_CAPITAL_RISK_PCT: float = 0.30  # Max % of bankroll staked in new positions per calendar day (UTC)

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
ODDS_API_REGIONS: str = "us"
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

# ── Opportunity Quality Filters ───────────────────────────────────────────────
MIN_BOOKMAKER_COUNT: int = 2          # Consensus must come from ≥2 books
MAX_KALSHI_SPREAD: float = 0.05       # Kalshi bid-ask spread ≤ 5¢ (ensures fillable price)
LIMIT_ORDER_SPREAD_THRESHOLD: float = 0.02   # Try mid-price limit order when spread exceeds 2¢
LIMIT_ORDER_TIMEOUT_SECONDS: int = 30        # Seconds to wait for limit order fill before falling back to IOC
MIN_KALSHI_VOLUME: float = 0.0        # Disabled — spread filter (MAX_KALSHI_SPREAD) is sufficient liquidity gate
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.04"))  # Minimum GROSS edge before fees (4% — net ~3% after Kalshi fee)
KALSHI_FEE_RATE: float = 0.03         # Kalshi charges ~3% of gross profit at settlement

# ── Notifications ─────────────────────────────────────────────────────────────
PUSHOVER_USER_KEY: str  = os.getenv("PUSHOVER_USER_KEY", "")   # From pushover.net account page
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")  # From pushover.net app creation

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_USERNAME: str = os.getenv("DASHBOARD_USERNAME", "")  # Required username; empty = any username accepted
DASHBOARD_PASSWORD: str = os.getenv("DASHBOARD_PASSWORD", "")  # HTTP Basic Auth; empty = no auth (local dev)
DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "")            # e.g. http://167.172.148.64:5000 — shown as link in Pushover
