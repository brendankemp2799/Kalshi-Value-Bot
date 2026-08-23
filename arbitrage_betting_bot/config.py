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
# 0.225, not 0.25, since 2026-08-21 -- this is a NO-OP on realised stake size, not a
# risk reduction. kelly_calculator applies uncertainty_factor = max(0.5, 1 -
# std/0.05*0.5), which shrinks the fraction when books disagree. With Pinnacle alone
# (see ODDS_API_BOOKMAKERS) consensus_std is identically 0, so that discount can never
# fire and every bet would silently size at the full fraction.
#
# Measured across 164 real bets, the discount actually applied averaged 0.898
# (median 0.936, p10 0.811). Leaving KELLY_FRACTION at 0.25 would therefore have
# raised mean stake by 11.3% as a side effect of a data-source change -- an
# unintended risk increase. 0.25 * 0.898 = 0.2246, rounded to 0.225.
#
# Restore to 0.25 if a multi-book panel returns, or the discount will be applied twice.
KELLY_FRACTION: float = 0.225
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

# Correlated-bet limits, expressed as MULTIPLES OF MAX_PCT_BANKROLL rather than as
# independent percentages. That relationship is the whole point.
#
# This started as a flat MAX_GAME_EXPOSURE_PCT = 0.02, chosen as a round number without
# checking it against the per-bet cap. At 5%/2% the largest single bet allowed was 2.5x
# the entire game budget -- incoherent in both directions. A $8.46 bet was permitted
# but then locked the game against everything else forever, while $1 + $1 + $1.50
# across three markets was refused at the third, even though three small correlated
# bets carry less joint risk than one bet 2.5x their total. Measured 2026-08-23: 5 of
# 195 filled bets were larger than the entire game budget.
#
# What is actually being managed is that Kelly sizes every bet as if independent. So
# the rule is: bets that load on the SAME factor must not, together, exceed what a
# single bet was allowed to be. Correlated bets count as one position.
#
#   factor cap = MAX_FACTOR_EXPOSURE_MULTIPLE x MAX_PCT_BANKROLL
#       all "scoring" bets on one game (totals + btts + rfi + player props), or all
#       "result" bets (h2h + spread), summed. See core/correlation_tracker.py.
#
#   game cap   = MAX_GAME_EXPOSURE_MULTIPLE x MAX_PCT_BANKROLL
#       every open position on the game regardless of factor. Bounds the case where
#       all legs lose together -- including mutually exclusive h2h outcomes, which are
#       exempt from the FACTOR cap (they cannot both win) but not from this one (they
#       can both lose).
#
# Because both derive from MAX_PCT_BANKROLL, a max-size single bet always fits.
MAX_FACTOR_EXPOSURE_MULTIPLE: float = float(
    os.getenv("MAX_FACTOR_EXPOSURE_MULTIPLE", "1.0"))
MAX_GAME_EXPOSURE_MULTIPLE: float = float(
    os.getenv("MAX_GAME_EXPOSURE_MULTIPLE", "2.0"))
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
    "americanfootball_nfl":         210,   # ~3.5h including stoppages/overtime
    "soccer_spain_la_liga":         120,
    "soccer_italy_serie_a":         120,
    "soccer_france_ligue_one":      120,
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
# ── TURNED OFF LIVE 2026-08-23 ────────────────────────────────────────────────
#
# Set ENABLE_STOP_LOSS=false on the droplet. The machinery below is retained and
# fully functional -- flip the env var back to `true` and restart to re-enable.
#
# WHY. Every stop-loss close was replayed against its market's actual settlement, so
# the counterfactual ("what if we had just held?") is observed rather than modelled.
# Across 70 settled stop-loss exits, stopping realised $8.46 LESS than holding would
# have. The mechanism is clearer than the P&L: bucketed by the price we exited at,
#
#     exit <0.05   n=32   22% of them went on to WIN
#     exit 0.05-15 n=15   13%
#     exit 0.15-25 n=8    50%
#     exit >=0.25  n=9    33%
#
# In every bucket the exit price sits BELOW the realised win rate. The break-even rule
# is "stop only if exit proceeds exceed the probability of winning if held", and it
# never held -- we were systematically selling below fair value, partly because the
# exit crosses to the bid on a thin book.
#
# THE EVIDENCE WAS NOT UNIFORM, and this switch is deliberately blunter than the data.
# By segment (spread excluded as a disabled bet type):
#     stop_loss h2h     n=29  -$14.30  95% CI [-26.0, -3.8]   <- the real result
#     trailing  h2h     n=11   -$7.17     CI [-17.7, +1.9]
#     stop_loss totals  n=30   +$2.59     CI [ -5.3, +9.1]   <- stop was EARNING here
#     stop_loss btts    n=4    +$3.55     CI [ +2.9,  +3.9]
# h2h alone accounted for -$21.47 while every other segment combined was +$7.01.
# Disabling h2h only would have returned +$6.35; disabling everything, -$0.65. The
# blanket switch-off was chosen by the operator with that $7.00 difference known.
#
# WHAT IS GIVEN UP. Open positions now always ride to settlement, so a bet that is
# wrong for a reason we cannot see (injury news, a lineup change, or a bug in our own
# pricing) runs to zero. The stop-loss recovered $2.06 of the wrong-side prop bug on
# 2026-08-22 -- its value is insurance against OUR errors, not against market moves.
# Capital lock-up is not a cost here: zero scans have ever been blocked by an exposure
# cap, peak utilisation 27.3% against the 30% limit.
#
# BEFORE RE-ENABLING, re-run the replay rather than trusting the numbers above -- they
# rest on one month in which holding happened to pay, and the totals/props segments
# were never significant in either direction.
ENABLE_STOP_LOSS: bool = os.getenv("ENABLE_STOP_LOSS", "false").lower() == "true"
# The threshold is PER BET TYPE, because the two books behave nothing alike after an
# adverse move. Measured 2026-08-17 (research/experiments/2026-08-17-stop-loss-by-bet-
# type.md) by replaying every settled position's real 1-minute candlestick path:
#
#   of positions that fell 20c below entry, how many still WON?
#     totals   3/28 = 10.7%   (base rate 44.9%)
#     h2h      7/22 = 31.8%   (base rate 47.4%)
#
# Stopping is correct iff the exit proceeds exceed the win probability of holding
# (s > p; see _stop_loss_move()). At a 20c drop that is +0.123 for totals and -0.099
# for h2h — i.e. the SAME threshold is strongly right on one book and wrong on the
# other. There is a mechanism: a totals market resolves by accumulation (runs/goals
# only ever get added, the clock runs one way), so once it is 20c underwater the
# innings needed to rescue it have physically been spent. An h2h market has no such
# ratchet — a 20c move means a lead changed hands, and leads change hands again.
STOP_LOSS_MOVE: float = 0.30   # h2h/spread, and the default for any unlisted bet type
STOP_LOSS_MOVE_BY_BET_TYPE: dict[str, float] = {
    "totals": 0.20,
}
# 0.30 for h2h is also the MINIMUM-REGRET choice, not just the best point estimate:
# across thresholds the h2h s-p margin runs -0.082 (0.10), -0.099 (0.20), -0.041
# (0.25), +0.011 (0.30), -0.053 (0.35). 0.30 is where s ~= p, so it is the threshold
# least sensitive to the recovery rate being mis-measured — which matters, because
# that rate rests on n=22.
#
# Replaced STOP_LOSS_MOVE_TOTALS_EARLY (a 0.35 -> 0.20 time ramp, 2026-08-12 .. 08-17).
# The ramp was added after position #315 (Baltimore/Minnesota Under 8.5), where a thin
# ~24c-wide quote spike at the end of the 1st inning triggered the flat 0.20 stop on a
# game that finished well over. That incident was real, but the ramp was the wrong fix
# for it: it widened the stop for the ENTIRE early game to defend against a
# single-tick quote artifact, and measured out at -5.4pp of equal-weighted ROI on
# totals (P(ramp better) = 9%). STOP_LOSS_CONFIRM_CHECKS below defends against the
# artifact directly instead, letting totals keep the tight stop the data supports.
#
# Number of CONSECUTIVE checks the price must sit at/below the stop level before the
# position is cut. At POSITION_MONITOR_INTERVAL_SECONDS=30 this costs at most ~30s of
# extra adverse exposure; in exchange, no single bad quote can close a position. This
# is what makes the tight totals stop safe — #315 was one spike, not a trend.
STOP_LOSS_CONFIRM_CHECKS: int = 2

# ── Market Making (passive two-sided quoting) ───────────────────────────────────
# Unified with the directional strategy, not a separate bot: for any matched market
# whose Kalshi spread is too wide to cross directionally (see max_kalshi_spread
# above), rest quotes inside the spread instead and capture it net of Kalshi's maker
# fee (25% of the taker formula — see KALSHI_TAKER_FEE_RATE_ESTIMATE below; there is
# no maker rebate for a retail account). That 25% figure was an assumption when
# written; it has since been measured once and held: the only maker fee Kalshi
# charged across 139 filled orders (2026-08-15) implied 0.0178, vs the 0.0175 assumed
# here. But it was charged on 1 of 139 — MM_MIN_NET_PER_PAIR below deliberately
# assumes it is ALWAYS charged, which makes MM quote less, never more. Fills flow
# into the same `positions` table
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
#
# Gated on KalshiMarket.volume_24h (recent FLOW), NOT .volume. `volume` is
# max(lifetime volume, open interest) -- two lifetime-to-date stocks that never
# decay, so a market that traded 5,000 contracts three weeks ago and nothing
# since scored identically to one trading right now. A maker only gets filled
# when a counterparty CROSSES, so trade arrival is the quantity that matters;
# accumulated history and resting size are not.
#
# Depth was considered and rejected as the metric: of 125 live wide-spread
# markets on 2026-08-15, 74 had two-sided depth >= 20 contracts while having
# never traded at all (one showed 6,419 contracts resting against zero lifetime
# volume). Depth is the supply of liquidity -- our competition -- not our
# counterparty.
MM_MIN_VOLUME: float = float(os.getenv("MM_MIN_VOLUME", "100"))

# Stop quoting this many seconds BEFORE kickoff, and cancel anything resting.
#
# Nothing used to do this. The bot only SCANS pre-game (odds_fetcher discards
# events whose commence_time has passed), but a resting MM quote does not expire
# at kickoff -- the Kalshi market keeps trading through the game. The sequence
# was: quote rests -> game starts -> the event drops out of mm_candidates ->
# run_mm_tick stops seeing that ticker entirely, so it is never fill-checked or
# cancelled -> only sweep_orphaned_quotes eventually kills it, and only once the
# order is over an hour old. In between we sat on a two-sided quote priced off a
# sportsbook consensus captured before kickoff, in a market now absorbing
# play-by-play information. That is the single most toxic state this bot can be
# in, and it was unguarded.
#
# Standing down BEFORE kickoff (rather than at it) matters because the ticker is
# still an MM candidate at that point, so run_mm_tick can still see it and cancel
# it. Once the game starts it is gone from the candidate list and unmanageable.
# The margin covers the observed MM tick stall: ticks are ~30s normally but
# measured gaps of 2.4 and 19 minutes occur while run_scan() blocks the loop on
# resting GTC orders, so a margin under ~20 min can be slept through.
MM_STOP_QUOTING_BEFORE_KICKOFF_SECONDS: int = int(
    os.getenv("MM_STOP_QUOTING_BEFORE_KICKOFF_SECONDS", "1200")   # 20 minutes
)

# Fair-value confidence. _maybe_mm_candidate() only forwards markets the
# DIRECTIONAL strategy already accepted on book count and disagreement -- but
# that check is min_bookmaker_count=2, and its high_uncertainty_std=0.04 test
# only applies once high_uncertainty_min_books=4 books are present. So a 2-book
# market with a 0.10 std passes it. Resting a two-sided quote centered on that
# consensus is strictly worse than taking one side of it, because both legs are
# wrong at once. These apply unconditionally.
MM_MIN_BOOKMAKERS: int = int(os.getenv("MM_MIN_BOOKMAKERS", "1"))  # see ODDS_API_BOOKMAKERS: single-book panel since 2026-08-21
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
# The near-game tiers were RETIRED on 2026-08-20 (both set to the default) after
# measuring what they ever produced. Time-to-event at placement, every bet the bot has
# made:
#
#     bets placed  0-30 min before kickoff :   0
#     bets placed 30-60 min before kickoff :   0
#     bets placed      >60 min before      : 156   (99.4%)
#     median time-to-event: 26.4 hours     mean: 67 hours
#
# Zero bets, ever, from either accelerated tier -- while they accounted for ~61% of all
# scan cycles and therefore most of the Odds API bill. They fired because SOME game in
# the universe was about to start, not because anything was left to do: we decide on a
# game roughly a day before it begins.
#
# This does NOT slow risk management. Open positions are monitored on
# POSITION_MONITOR_INTERVAL_SECONDS (30s), a separate loop that never touches the Odds
# API. Restore by setting the env vars if the strategy ever becomes time-sensitive.
POLL_INTERVAL_DEFAULT_SECONDS: int   = int(os.getenv("POLL_INTERVAL_DEFAULT_SECONDS",  "2700"))  # 45 min — baseline
POLL_INTERVAL_PRE_GAME_SECONDS: int  = int(os.getenv("POLL_INTERVAL_PRE_GAME_SECONDS", "2700"))  # retired (was 10 min)
POLL_INTERVAL_NEAR_GAME_SECONDS: int = int(os.getenv("POLL_INTERVAL_NEAR_GAME_SECONDS", "2700"))  # retired (was 2 min)
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
# Expanded 2026-08-21. Kalshi lists game markets for all of these and The Odds API
# carries every one, with Pinnacle quoting h2h/totals on each -- verified per league
# before adding, not assumed. Measured tradability of the 598 new game markets against
# our own max_kalshi_spread=0.05 and a volume floor: ~50% pass both, versus ~61% for
# the leagues we already trade. That roughly doubles usable inventory.
#
# NCAAF is deliberately EXCLUDED despite being the largest single prize (184 open
# game events). College football has ~260 teams with genuinely colliding names --
# Miami FL vs Miami OH, Texas vs Texas St. vs Texas Tech, dozens of "St." variants --
# which is the same failure class as position #930 (bought "Chicago WS" believing it
# was "Chicago Cubs") at ten times the surface area. Add it only after measuring the
# ambiguous-match refusal rate on real fixtures.
SPORTS: list[str] = [
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_usa_mls",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "americanfootball_nfl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
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

# ── Credit control (measured 2026-08-20) ───────────────────────────────────────
# The Odds API bills  cost = (number of markets) x (number of units), where units is
# the region count OR, if `bookmakers` is sent instead of `regions`, ceil(books/10).
# Measured directly against the live API:
#
#     regions=us,eu   markets=h2h,totals,spreads   -> 6 credits   (31 books)
#     bookmakers=10   markets=h2h,totals,spreads   -> 3 credits   (10 books)
#     bookmakers=1    markets=h2h,totals,spreads   -> 3 credits   ( 1 book)
#     bookmakers=11   markets=h2h                  -> 2 credits   <- step at 10
#
# So 1 book and 10 books cost the SAME. Naming <=10 books halves every request while
# keeping Pinnacle (which lives in the `eu` region, so `regions=us` alone loses it).
# Sending this list makes ODDS_API_REGIONS unused for the bulk odds call.
#
# Chosen for LINE COVERAGE, not sharpness -- those are different properties and
# coverage is what pays here. A totals bet needs a book quoting Kalshi's exact
# strike; the sharpest book is useless if it has no price at that number. Ranked by
# how many of our real candidates each book could price (see the 2026-08-20 credit
# analysis), with Pinnacle kept because the 10th slot is free.
# PINNACLE ONLY as of 2026-08-21. Measured, per game, against the strikes Kalshi
# actually lists:
#
#   Kalshi strikes per game : median 2 (main line +/- 0.5-1.0)
#   Pinnacle prices them    : 17/17 = 100%   (its ladder spans main +/- ~1.5)
#   full 10-book panel      : 17/17 = 100%
#   Brier, n=526            : pinnacle 0.24548  vs  10-book blend 0.24541  (t=+0.37)
#
# Identical coverage, statistically identical accuracy. An earlier analysis claimed
# Pinnacle covered only 40% -- that was wrong: it pooled strikes across every game in
# a sport and tested them against ONE game's ladder, so the misses were other games'
# totals, not unpriced strikes.
#
# A useful side effect: Pinnacle does not quote games nobody trades. It was absent for
# 12% of historical candidates, and those markets had a MEDIAN Kalshi volume of 132
# against 8,636 where it was present -- a 65x difference, with a quarter of them at <=1
# contract of lifetime volume. The 8 bets we placed in them returned -37.5% against
# -2.6% elsewhere. Requiring Pinnacle therefore acts as the liquidity floor that
# min_kalshi_volume (still 0.0) never provided.
#
# Cost is unchanged: billing is per block of 10 books, so 1 book and 10 books are both
# 1 unit. This buys simplicity and noise reduction, not credits.
ODDS_API_BOOKMAKERS: str = os.getenv("ODDS_API_BOOKMAKERS", "pinnacle")

# Never look at a game starting more than this far out. Measured 2026-08-20 across
# every order the bot has placed: orders >48h from kickoff filled 57/751 = 7.6% of
# the time, versus 45.8% in the 3-12h window. That tail is the bulk of our order
# traffic and almost none of it converts. It also caps per-event alternate-line cost,
# which bills per game in the window.
MAX_TIME_TO_EVENT_HOURS: int = int(os.getenv("MAX_TIME_TO_EVENT_HOURS", "48"))

# Fetch each game's FULL totals ladder (alternate_totals) instead of relying on
# whichever single line each book happens to feature.
#
# The bulk /odds endpoint returns exactly ONE totals line per book -- that book's
# featured line -- and books disagree about what it is (for one MLB game: 18 books at
# 9.0, 4 at 9.5). Kalshi lists its own strikes, so matching was luck: 20 of 29 totals
# candidates we would have bet had NO book quoting Kalshi's number, even with 31
# books. alternate_totals returns the whole ladder (MLB 4.5-15.5, EPL 0.5-8.5).
#
# It is only served by the PER-EVENT endpoint (the bulk endpoint 422s), so it bills
# 1 credit per game per refresh with <=10 bookmakers. Refresh cadence is tiered by
# time-to-event because fill rate and ROI both concentrate near the game.
ENABLE_ALTERNATE_LINES: bool = os.getenv("ENABLE_ALTERNATE_LINES", "true").lower() == "true"
# (max_hours_out, refresh_every_hours) -- first match wins, so keep ascending.
# Prop markets fetched PER EVENT, per sport. Like alternate_totals these are
# "additional markets": the bulk /odds endpoint 422s on them, so each bills 1 credit
# per game per market. They are therefore fetched ONLY for games where Kalshi lists a
# matching market, which we know for free before spending anything.
#
# Chosen by measured tradability against max_kalshi_spread=0.05 plus a volume floor:
#   btts                  KXEPLBTTS 82%, KXLALIGABTTS 67%, KXSERIEABTTS 64%,
#                         KXMLSBTTS 47%, KXLIGUE1BTTS 40%
#   totals_1st_1_innings  KXMLBRFI 50%   (Over 0.5 == "a run scores in the 1st")
#
# Everything first-half measured 0-5% tradable and is excluded. Player props are
# excluded too: Kalshi has no per-game player series, only season-long ones quoted
# 0.00/0.99.
PROP_MARKETS: dict[str, str] = {
    # MLB fetches the first-inning total AND the three player markets Pinnacle
    # actually quotes, in one per-event call (cost = markets x 1 unit).
    "baseball_mlb":            "totals_1st_1_innings,pitcher_strikeouts,"
                               "batter_home_runs,batter_total_bases",
    "soccer_usa_mls":          "btts",
    "soccer_epl":              "btts",
    "soccer_spain_la_liga":    "btts",
    "soccer_italy_serie_a":    "btts",
    "soccer_france_ligue_one": "btts",
}
ENABLE_PROP_MARKETS: bool = os.getenv("ENABLE_PROP_MARKETS", "true").lower() == "true"

ALTERNATE_LINE_REFRESH_TIERS: list[tuple[int, int]] = [
    (12, 1),    # inside 12h: hourly
    (24, 3),    # 12-24h: every 3h
    (48, 6),    # 24-48h: every 6h
]

# Which Odds API market types to fetch per sport.
# Multiple types can be comma-separated (one API call per sport).
# `spreads` removed 2026-08-20. It cost a full third of every bulk request (cost is
# markets x units) and produced 9 bets at -29.8% ROI (-$4.93) -- our worst bet type by
# a wide margin, the one the #930 wrong-team bug landed on, and the only one with no
# stop-loss evidence behind its threshold. Re-add by putting "spreads" back here; the
# matcher and detector still handle it.
SPORT_MARKETS: dict[str, str] = {
    "basketball_nba":              "h2h,totals",
    "baseball_mlb":                "h2h,totals",
    "icehockey_nhl":               "h2h,totals",
    "soccer_usa_mls":              "h2h,totals",
    "soccer_epl":                  "h2h,totals",
    "soccer_uefa_champs_league":   "h2h,totals",
    "americanfootball_nfl":        "h2h,totals",
    "soccer_spain_la_liga":        "h2h,totals",
    "soccer_italy_serie_a":        "h2h,totals",
    "soccer_france_ligue_one":     "h2h,totals",
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
    # 1, not 2, since 2026-08-21: we fetch Pinnacle alone (see ODDS_API_BOOKMAKERS),
    # so every candidate has exactly one book and a floor of 2 would reject all of
    # them. The corroboration this used to provide is now supplied by Pinnacle's own
    # absence -- it does not quote untraded markets, which is what the floor was
    # really protecting against. Restore to 2 if a multi-book panel ever returns.
    "min_bookmaker_count": 1,
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
    # Props (2026-08-21). Same thresholds as everything else for now -- a prop is not
    # inherently lower quality, it is just thinner, and thinness is already caught by
    # max_kalshi_spread. Split these out if props start behaving differently.
    "btts":   dict(_DEFAULT_QUALITY_FILTER),
    "rfi":    dict(_DEFAULT_QUALITY_FILTER),
    "player_prop": dict(_DEFAULT_QUALITY_FILTER),
}


# A market whose spread exceeds max_kalshi_spread used to be discarded by
# _quality_check() BEFORE its edge was ever computed, then handed to the market
# maker. That made spread width the routing decision and meant a wide market with
# real directional value was simply never evaluated as a bet — and after the MM
# centering gate was added (2026-08-14), a market whose consensus sat outside the
# book got rejected by MM too, so nobody traded it at all.
#
# With this True, a wide-spread market is still edge-evaluated, and a PASSIVE
# (maker_only) opportunity in one is allowed: it rests at the mid and walks away
# at no cost if unfilled, so the downside is bounded at zero.
#
# Crossing a wide spread is NOT allowed regardless of this flag. max_kalshi_spread
# is a market-QUALITY signal as much as an execution-cost one — a wide spread means
# thin, stale, unreliable pricing — and paying the ask into that is exactly the
# trade we do not want. Such markets are logged 'spread_too_wide_take' and routed
# to MM as before.
#
# Measured on one live tick (40 MM candidates, 2026-08-15): 0 would have crossed
# at the ask, 7 had passive edge (1.1%-4.6%), 33 had no edge at all. So this is a
# correctness fix with a small expected effect, not a volume increase.
ALLOW_WIDE_SPREAD_MAKER: bool = os.getenv("ALLOW_WIDE_SPREAD_MAKER", "true").lower() == "true"


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
# "spread" removed from the default on 2026-08-21, alongside dropping `spreads` from
# SPORT_MARKETS. The two must move together: without spread ODDS there is nothing to
# build a consensus from, so every matched spread market logged `no_consensus` -- 82
# per scan, ~2,600 rows/day of pure noise into book_probability_log, the table that
# OOM-killed the daily cron once already. Gating here skips them before they are
# evaluated at all. Re-enable BOTH to bring spreads back.
ENABLED_BET_TYPES: set[str] = {
    t.strip().lower()
    for t in os.getenv("ENABLED_BET_TYPES", "h2h,totals,btts,rfi,player_prop").split(",")
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
# Minimum gap between consecutive order POSTs to Kalshi, across all threads.
#
# main.py places every approved order from a ThreadPoolExecutor sized
# max_workers=len(approved_live) -- one thread per order, all POSTing at once. That
# was harmless while a scan approved a handful of orders. Props changed the shape: one
# MLB game carries ~50 prop markets, all evaluated in the same scan, so approvals
# concentrate into a single burst instead of trickling. On 2026-08-22 a scan approved
# 33 orders, 12 POSTs landed inside 600ms, and Kalshi rejected all 12 with HTTP 429 --
# every 429 the bot has ever seen came from that day.
#
# The fix is spacing, not a smaller pool: each worker then blocks up to
# LIMIT_ORDER_TIMEOUT_DEFAULT_SECONDS waiting on its GTC order, so a small pool would
# serialise the WAITING and take hours to work through a batch. This gate applies to
# the POST only and leaves the poll loops fully parallel.
#
# 0.15s is ~6.7 orders/sec, so a 33-order batch takes ~5s to place. Set to 0 to disable.
KALSHI_ORDER_MIN_SPACING_SECONDS: float = float(
    os.getenv("KALSHI_ORDER_MIN_SPACING_SECONDS", "0.15"))

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
