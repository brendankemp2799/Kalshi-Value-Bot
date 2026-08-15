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

# ── MM eligibility gates (added 2026-08-14) ───────────────────────────────────
# Before these, the ONLY things MM checked were "is the spread wide" and "is
# consensus within 0.15-0.85". A survey of all 979 live Kalshi sports markets on
# 2026-08-14 showed why that isn't enough:
#
#   universe                n    median volume   vol>=10k   ZERO volume
#   spread >= 5c (MM's)   230              0           17     179 (78%)
#   spread 2-4c           230             10           12      90
#   all sports markets    979             35          141     362
#
# On Kalshi sports, wide spread and liquidity are close to mutually exclusive --
# the spread is wide BECAUSE nobody is quoting there. 78% of the markets MM was
# willing to quote had never traded at all, so no amount of quote-price tuning
# could produce a fill in them.

# Minimum lifetime contract volume before MM will quote a market. The
# distribution is a cliff, not a gradient (wide-spread markets surviving each
# threshold: >=0 -> 198, >=1 -> 38, >=50 -> 20, >=100 -> 18, >=1000 -> 12), so
# nearly all of the benefit comes from excluding never-traded markets at all;
# 100 sits just past the handful-of-trades noise floor.
MM_MIN_VOLUME: float = float(os.getenv("MM_MIN_VOLUME", "100"))

# Fair-value confidence. _maybe_mm_candidate() only forwards markets the
# DIRECTIONAL strategy already accepted on book count and disagreement -- but
# that check is min_bookmaker_count=2, and its high_uncertainty_std=0.04 test
# only applies once high_uncertainty_min_books=4 books are present. So a 2-book
# market with a 0.10 std passes it. Resting a two-sided quote centered on that
# consensus is strictly worse than taking one side of it, because both legs are
# wrong at once. These apply unconditionally.
MM_MIN_BOOKMAKERS: int = int(os.getenv("MM_MIN_BOOKMAKERS", "3"))
MM_MAX_CONSENSUS_STD: float = float(os.getenv("MM_MAX_CONSENSUS_STD", "0.04"))

# Centering. MM's premise is "the market is roughly right and I'm paid to wait";
# a consensus sitting OUTSIDE Kalshi's bid/ask is the opposite claim -- it says
# the market is wrong, which is a directional signal, not a spread to capture.
# Quoting it anyway produced a concrete bug: consensus 0.70 against a 0.45/0.55
# book yields a 0.665 YES bid, above the ask, so place_resting_quote() crosses
# and we pay the TAKER fee (4x maker) on a price the directional model never
# validated -- and only the crossing leg fills, leaving a naked directional
# position wearing a market-making costume, which is exactly what equal-contract
# pairing exists to prevent. Tolerance allows consensus to sit slightly outside
# the touch without disqualifying an otherwise-centered market.
MM_CENTERING_TOLERANCE: float = float(os.getenv("MM_CENTERING_TOLERANCE", "0.02"))

# Hard floor on expected profit per MATCHED pair (one YES + one NO contract),
# after Kalshi's maker fee on both legs. A matched pair costs
# yes_bid_price + no_bid_price and always pays exactly $1, so the gross capture
# is 1 - pair_cost and the fee is ~0.0175*p*(1-p) per leg (~0.44c each near 50c,
# ~0.87c the pair). That sets a real break-even spread, which is why quoting the
# median 1c-spread market can never work:
#
#   spread   pair cost   gross    fees    NET per pair
#     1c        0.9930   0.70c   0.87c        -0.17c
#     2c        0.9860   1.40c   0.87c        +0.53c
#     3c        0.9790   2.10c   0.87c        +1.23c
#     5c        0.9650   3.50c   0.87c        +2.63c
#    12c        0.9160   8.40c   0.87c        +7.53c
#
# Enforced after the crossing guard clamps prices inward, since clamping can
# eat the entire margin.
MM_MIN_NET_PER_PAIR: float = float(os.getenv("MM_MIN_NET_PER_PAIR", "0.01"))
MM_MAKER_FEE_RATE: float = 0.0175  # 25% of KALSHI_TAKER_FEE_RATE_ESTIMATE (0.07)

# How many candidates MM should be able to quote at once. mm_clip_size() used to
# cap a single clip at MAX_PCT_BANKROLL * bankroll -- the SAME number as the
# total MM budget (MM_MAX_EXPOSURE_PCT * bankroll), so the first candidate
# consumed 89-97% of the entire allowance and every one after it was rejected by
# run_mm_tick()'s aggregate cap. Observed live: ~60 candidates per scan, one
# quoted, 59 silently skipped.
#
# There is a floor on how far this can be raised. Kalshi rounds each order's fee
# UP to the whole cent, so splitting the budget more ways eventually makes each
# clip small enough that rounding dominates. Measured at a $157.72 bankroll and
# 4 concurrent quotes: a clip funds 2 contracts/leg, whose true maker fee is
# $0.0087 and bills at $0.01 — a 1.1x overhead, i.e. real but not yet the binding
# constraint. It would bite hard at 1 contract per leg.
#
# At that same bankroll, 4 concurrent quotes yields (spread -> net per matched
# pair, after fees): 6c -> 3.13c, 8c -> 5.13c, 10c -> 6.13c, 16c -> 11.14c, with
# 4 candidates fitting inside the $7.89 budget at ~$1.9 notional each.
MM_MAX_CONCURRENT_QUOTES: int = int(os.getenv("MM_MAX_CONCURRENT_QUOTES", "4"))

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
# WARNING: adding a region is NOT free. The Odds API bills markets x regions per
# request, so "us,eu" with 3 markets costs 6 credits per sport-fetch, not 3 — the EU
# region is half your entire credit spend. This comment previously read "at no extra
# credit cost", which is wrong and hid the cost for a month: measured burn is ~1,279
# credits/day = ~38,400/month against a 20,000/month plan (192% of plan, exhausted
# around the 15th of each cycle). Dropping to "us" alone roughly halves it, at the cost
# of losing Pinnacle from the consensus — a real trade-off in signal quality, not free
# money. See research/ notes and the api_credits table for the measurements.
ODDS_API_REGIONS: str = "us,eu"
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
