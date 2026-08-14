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
# How far buying ONE whole contract may overshoot the Kelly target before the bet is
# skipped. Contracts are indivisible, so at a small bankroll a +EV Kelly result is
# often smaller than a single contract; the question is how much over-betting that
# forced rounding-up is worth tolerating. 1.5x means: take the bet if one contract
# costs up to 50% more than Kelly asked for, otherwise pass.
#
# Replaces the former MIN_BET_DOLLARS = $0.50 gate, now removed. That floor was
# arbitrary (its own comment noted Kalshi's minimum is ~$0.01/contract) and
# discontinuous at this bankroll: $0.47 was rejected outright while $0.51 was rounded
# UP to a whole contract costing as much as $0.65 — same economics, opposite outcome.
# One live scan showed it discarding 5 opportunities Kelly had already judged +EV.
MAX_ROUNDING_OVERSHOOT: float = float(os.getenv("MAX_ROUNDING_OVERSHOOT", "1.5"))
MAX_BET_DOLLARS: float = 100.0        # Hard dollar cap per bet
MAX_PCT_BANKROLL: float = 0.05        # Max 5% of bankroll per single bet
MAX_TOTAL_EXPOSURE_PCT: float = 0.30  # Max 30% of bankroll deployed at once
MAX_SPORT_EXPOSURE_PCT: float = 0.15  # Max 15% of bankroll in one sport
MAX_OPEN_POSITIONS: int = 40          # Backstop circuit-breaker only (e.g. a bug placing runaway
                                       # duplicate positions) -- exposure % (below) and correlation
                                       # rules (core/correlation_tracker.py) are the real, bankroll-
                                       # aware risk gates and are always enforced independently of
                                       # this. Was 10, which at real stake sizes bound ~4x tighter
                                       # than MAX_TOTAL_EXPOSURE_PCT ever would, and unlike the %
                                       # caps doesn't scale with bankroll at all. Confirmed live
                                       # (2026-08-11): 10 open positions totaled just 7.1% of
                                       # bankroll, games 3.8-11.3 days out (no same-day correlation
                                       # either) -- the count cap was the only thing blocking further
                                       # scanning, and hitting it skips the Odds API fetch and
                                       # detect_value() entirely, not just new bets.
MAX_DAILY_CAPITAL_RISK_PCT: float = 0.30  # Max % of bankroll staked in new positions per calendar day (UTC)

# ── Trailing Stop (mid-position exit risk management) ──────────────────────────
# Kalshi has no native stop/conditional order type — this is simulated by polling
# price each scan (piggybacked on auto_settle's existing per-position market fetch)
# and placing a real closing order once price retraces to the trailing level.
# DISABLED in production (.env) as of 2026-08-09 — see
# research/experiments/2026-08-09-trailing-stop-vs-stoploss-only.md. Replaying all
# 38 settled live positions' real intraday price paths through trailing-stop + the
# still-enabled stop-loss showed EVERY threshold tested (flat 0.10, flat 0.15,
# today's dynamic ramp) net-negative (-$1.83 to -$4.59), while stop-loss ALONE (no
# trailing stop) netted +$18.70 over the same trades — trailing stop was cutting
# real winners short more than it was preventing losses, at every threshold tried.
# The tuning params below are kept for the record / in case this is re-enabled and
# re-tested later, not because they're currently in effect.
# Master switch — defaults off. Validate in paper mode before enabling live.
ENABLE_TRAILING_STOP: bool = os.getenv("ENABLE_TRAILING_STOP", "false").lower() == "true"
# Arm threshold is time-into-game dependent, not a flat constant (see
# _dynamic_arm_move() in execution/risk_manager.py) — linearly interpolated between
# these two bounds by elapsed fraction of the sport's expected game duration below.
TRAILING_STOP_ARM_MOVE_EARLY: float = 0.20  # min favorable move to arm, at/near game start
TRAILING_STOP_ARM_MOVE_LATE: float = 0.08   # min favorable move to arm, at/after expected game end
TRAILING_STOP_LOCK_FRACTION: float = 0.35   # fraction of the move-from-entry protected once armed
# Made dynamic on 2026-08-09 (research/experiments/2026-08-09-trailing-stop-arm-
# threshold.md addendum): a flat threshold treats an early-game move and a late-game
# move as the same signal, but they aren't — a swing minutes after a game starts has
# far more time (and far more remaining plays) to revert than the same swing with the
# game nearly over. EARLY=0.20 is more tolerant than the flat 0.15 this replaced,
# specifically to survive the single-play whipsaw that motivated that fix (position
# #262, Baltimore/Texas Over 8.5, armed on a 1-minute spike to 54c only 8 minutes into
# the game, then got stopped out for $0.01 three minutes before the market ran to
# 96-99c). LATE=0.08 arms more eagerly than the original flat 0.10 — with little game
# time left for a real trend to keep developing, and genuine reversal risk (a
# walk-off, a last-minute goal) still on the table, banking gains sooner is the safer
# trade. Reasoned starting points, not fit to the n=10 trailing-stop-close sample by
# search/optimization — revisit once more closes accumulate under this logic.
# Flat-threshold history (0.10 -> 0.15 on 2026-08-09) kept for context: of the 10 live
# trailing-stop closes to date, the 5 that armed on a <0.16 move netted -$0.41 combined
# (3 of 5 net losses despite the "protection"); the 5 that armed on a >=0.16 move
# netted +$0.27. STOP_LOSS_MOVE below is unaffected either way and still backstops
# genuine reversals regardless of whether the trailing stop ever arms.
# Raised from 0.20 on 2026-08-08 (LOCK_FRACTION, not ARM_MOVE): real trailing-stop
# closes showed 0.20 protecting so little of a typical ~16c move that Kalshi's fee
# (peaking ~30-60c, right where these positions trade) consumed 80-100% of the
# captured slice — several genuine wins closed at breakeven or a small net loss. 0.35
# leaves more room to run while still resistant to the fee eating the whole locked-in
# gain.

# Expected real-world game duration per sport (minutes), used only to compute the
# dynamic trailing-stop arm move above — not used for scheduling/polling elsewhere.
SPORT_EXPECTED_DURATION_MINUTES: dict[str, int] = {
    "basketball_nba":            150,
    "baseball_mlb":               190,
    "icehockey_nhl":               150,
    "soccer_usa_mls":              120,
    "soccer_epl":                   120,
    "soccer_uefa_champs_league":    120,
}
SPORT_EXPECTED_DURATION_DEFAULT_MINUTES: int = 150  # fallback for an unrecognized sport key
# How often open positions are checked against Kalshi, independent of the Odds-API scan
# cadence above. Kalshi's own APIs (market quotes, portfolio positions/fills) aren't
# credit-metered, so this can run much faster than the Odds-API-driven scan without any
# cost — see POSITION_MONITOR_INTERVAL_SECONDS below and _run_variable_loop().
POSITION_MONITOR_INTERVAL_SECONDS: int = int(os.getenv("POSITION_MONITOR_INTERVAL_SECONDS", "30"))

# ── Stop Loss (mid-position adverse-move exit) ──────────────────────────────────
# Symmetric counterpart to the trailing stop above: the trailing stop only protects
# positions that first move favorably — this cuts a position that's moving against
# entry, before it rides all the way to a full loss. Added 2026-08-08 after real bet
# history showed positions with no risk management applied at all (never armed the
# trailing stop) had a -51% ROI vs -1% for trailing-stop-managed exits. Confirmed
# against real candlestick history that these losses decline gradually (20-185 min),
# not in a single tick, so a polling check has time to catch them.
# Master switch — defaults off. Validate in paper mode before enabling live.
ENABLE_STOP_LOSS: bool = os.getenv("ENABLE_STOP_LOSS", "false").lower() == "true"
STOP_LOSS_MOVE: float = 0.20   # adverse move (price units) from entry that triggers a cut
# Totals-only, time-ramped widening of the stop above — added 2026-08-13 after a real
# incident (position #315, Baltimore/Minnesota Under 8.5): a thin, ~24c-wide quote
# spike right at the end of the 1st inning triggered the flat 0.20 stop; the game went
# on to finish well over the total (12 runs). A totals market's price early in a game
# reflects a much smaller sample (often one inning/quarter) of the full-game outcome
# it's meant to predict than a moneyline market does at the same point, making it
# structurally noisier early on — see execution/risk_manager.py::_dynamic_stop_loss_move()
# for the ramp (ramps down to the flat STOP_LOSS_MOVE above by the sport's expected
# game duration, config.SPORT_EXPECTED_DURATION_MINUTES, same mechanism the trailing
# stop's arm move already uses). Not applied to h2h/spread — this incident and the
# reasoning behind it are specific to totals.
STOP_LOSS_MOVE_TOTALS_EARLY: float = 0.35

# ── Market Making (passive two-sided quoting) ───────────────────────────────────
# Unified with the directional strategy, not a separate bot: for any matched market
# whose Kalshi spread is too wide to cross directionally (see max_kalshi_spread
# above), rest quotes inside the spread instead and capture it net of Kalshi's maker
# fee (25% of the taker formula — see KALSHI_TAKER_FEE_RATE_ESTIMATE below; there is
# no maker rebate for a retail account). Fills flow into the same `positions` table
# (positions.strategy='market_making') and are covered by the existing trailing-stop/
# stop-loss risk management with no separate exit-risk code.
#
# Calibrated 2026-08-08 via mm_backtest.py against real Kalshi candlestick history
# for 40 live matched wide-spread markets (~3 days of trading each, hourly candles):
# half_spread_frac 0.25/0.35/0.5 all showed net positive simulated capture net of
# fees, with 0.35 chosen as the conservative middle setting (0.25 fit this specific
# small sample best but is closer to over-fit; 0.5's fill rate was too low to be
# useful). Sample is small and noisy (one adverse-selection-heavy ticker dominated
# the P&L swing) — revisit once real paper/live fills accumulate.
# Master switch — defaults off. Validate in paper mode before enabling live.
ENABLE_MARKET_MAKING: bool = os.getenv("ENABLE_MARKET_MAKING", "false").lower() == "true"
MM_MAX_EXPOSURE_PCT: float = 0.05        # sub-cap within the shared bankroll (not a separate pot)
MM_MAX_CLIP_DOLLARS: float = 15.0        # size per quote leg
MM_MIN_SPREAD_TO_QUOTE: float = 0.05     # only quote where directional strategy wouldn't cross
MM_QUOTE_HALF_SPREAD_FRACTION: float = 0.35  # how far inside the Kalshi spread to rest each quote
MM_FAIR_VALUE_BAND: tuple[float, float] = (0.15, 0.85)  # skip deep favorites/underdogs
MM_INTERVAL_SECONDS: int = int(os.getenv("MM_INTERVAL_SECONDS", "30"))  # Kalshi-only requote tick

# consensus_prob is only refreshed on full due-scans (up to POLL_INTERVAL_DEFAULT_
# SECONDS apart), but the Kalshi-side spread is refreshed every MM_INTERVAL_SECONDS
# tick — so a quote's center can be sitting on a stale sportsbook read even though
# its width looks current. Both of these are zero-Odds-API-cost mitigations (using
# only the free Kalshi feed already being polled every tick), not a rescan.
MM_STALE_DRIFT_CANCEL: float = 0.05          # Kalshi mid move since candidate's scan -> pause quoting it
MM_STALE_WIDEN_MAX_MULTIPLIER: float = 1.5   # half-spread multiplier at max staleness (full poll interval old)

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

# On startup, skip a sport's initial full fetch if it was already fetched
# (per the persisted timestamp in storage/db.py::sport_poll_state) within this
# many seconds -- a redeploy shouldn't force a fresh Odds API fetch of every
# in-season sport if the last one was moments ago. Deliberately tight (matches
# the shortest real polling tier, near-game) so this only dedupes back-to-back
# restarts, never risks operating on stale data during a genuinely time-
# sensitive window. Added 2026-08-11 after finding 6 same-night redeploys had
# each forced a full unconditional re-fetch regardless of actual staleness.
STARTUP_REFETCH_SKIP_WINDOW_SECONDS: int = 120

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


# Bet types the directional strategy is allowed to trade. Comma-separated env var.
# A kill switch, not a tuning knob: it exists so a segment can be stopped immediately
# via .env without a code change or redeploy. All types are ON by default.
#
# Standing context on h2h, recorded so the next person does not have to rediscover it.
# As of 2026-08-13, across 52 settled live positions:
#     h2h     -$15.83 on $28.00 staked  (-56.5% ROI, n=18)
#     totals   +$1.90 on $41.06 staked  (+4.6% ROI,  n=31)
#     spread   -$1.40 on  $6.81 staked  (-20.6% ROI, n=3)
# h2h alone exceeds the entire net loss, and the 2026-08-12 weekly review found the
# same split independently (h2h -65.88% n=13 vs totals +6.82% n=25).
#
# h2h is nonetheless left ENABLED. n=18 is far too small to act on: at these stakes a
# handful of outcomes swings the whole figure, which is exactly the failure mode that
# invalidated several of the live-betting experiments in research/ (a rule showing
# +28.1% ROI at n=16 decayed to +3.2% by n=247). Turning a segment off on that evidence
# would be the same mistake in the opposite direction. Revisit when h2h reaches a sample
# where the difference is actually distinguishable from noise -- roughly n >= 100.
#
# To stop a segment immediately: ENABLED_BET_TYPES=totals,spread in .env, then restart.
#
# The default lists every value KalshiMarket.bet_type can take, so this gate never
# disables something by omission. "btts" is included even though no series currently
# maps to it -- otherwise wiring up the BTTS series later would silently do nothing.
# Note the soccer TIE market is bet_type "h2h" (it is distinguished by
# kalshi_outcome == "tie", not by bet_type), so "draw" is deliberately NOT a value here.
ENABLED_BET_TYPES: set[str] = {
    t.strip().lower()
    for t in os.getenv("ENABLED_BET_TYPES", "h2h,totals,spread,btts").split(",")
    if t.strip()
}


# Kalshi charges a real fee whenever an order crosses the spread at placement — not
# a fixed 0%/maker-only rate as originally assumed here. The actual fee per fill is
# read directly from Kalshi's own order record at fill time (see
# execution/kalshi_executor.py::_actual_fee_dollars) and stored on the position
# (entry_fee_paid), rather than estimated with a formula — a formula got this wrong
# before. Execution is two-step: (1) GTC at mid, adaptive timeout; (2) GTC at ask,
# short timeout — step 2 crosses the book by design and often incurs a real fee.
# Step 2 is skipped for maker_only opportunities (core/value_detector.py::
# _eval_edge) — those only clear the edge bar at the fee-free mid price, so a step-1
# order that goes unfilled just gives up rather than crossing into a losing trade.
#
# Timeouts widened 2026-08-11 (was 600/300/120s): checked 10 days of real GTC-mid
# attempts and found every fill was either immediate or never happened at all within
# the old window (zero fills detected mid-wait via polling) — so length wasn't
# clearly the constraint, but the sample is small (14 attempts) and there's no
# evidence a longer wait costs anything for the *edge* itself (the adverse-move
# reprice check below still bails early regardless of the overall timeout). The real
# cost is structural, not financial: main.py's tick loop is single-threaded and
# blocks on ThreadPoolExecutor until every concurrent order this scan resolves, which
# also delays MM requoting and position risk-management checks bot-wide for that
# whole window — so DEFAULT/PRE_GAME (lots of slack vs. their 45min/10min poll
# cadence) got more patience, NEAR_GAME (120s) was left alone since it's already
# close to its own 2min poll cadence and is exactly the tier where staying
# responsive matters most.
LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS: int = 900   # step 1 — game > 1 hour away (was 600)
LIMIT_ORDER_TIMEOUT_PRE_GAME_SECONDS: int = 450  # step 1 — within 1 hour (was 300)
LIMIT_ORDER_TIMEOUT_NEAR_GAME_SECONDS: int = 120 # step 1 — within 30 minutes (unchanged)
LIMIT_ORDER_ASK_TIMEOUT_SECONDS: int = 30        # step 2 — GTC at ask (short; ask should fill fast)

# While step 1's mid-price order rests, periodically re-check Kalshi's own live
# price (free — no Odds API cost) instead of blindly waiting out the full timeout.
# If the live price has moved against the resting order by PASSIVE_ADVERSE_MOVE_CANCEL
# or more, cancel early and fall through to step 2 rather than risk filling at a
# price the market has already moved past.
PASSIVE_REPRICE_CHECK_INTERVAL_SECONDS: int = 30
PASSIVE_ADVERSE_MOVE_CANCEL: float = 0.05
# Edge this large is worth taking immediately rather than risking losing it while
# step 1 waits for a passive fill — skip straight to step 2 (ask, immediate).
LARGE_EDGE_SKIP_PASSIVE: float = 0.10
FAILED_BET_COOLDOWN_SECONDS: int = int(os.getenv("FAILED_BET_COOLDOWN_SECONDS", "10800"))  # 3 hours
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.01"))  # Minimum NET edge after Kalshi taker fee

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
