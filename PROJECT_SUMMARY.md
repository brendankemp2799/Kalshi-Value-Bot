# Arbitrage Betting Bot — Project Summary

## Overview

A Python bot that monitors traditional sportsbooks (via The Odds API) and compares their implied
probabilities against Kalshi prediction market prices to identify mispriced contracts. When value
is detected, the bot sizes bets using the Kelly Criterion and **automatically places Kalshi orders**
with no human approval required.

**Pricing reference:** FanDuel, DraftKings, and other sharp books via The Odds API (de-vigged
consensus probability only — no bets placed there).  
**Execution:** Kalshi only — legal for California residents.

---

## Run Modes

| Command | Behavior |
|---|---|
| `python3 main.py` | **Live** — places real Kalshi orders automatically |
| `python3 main.py --paper` | **Paper trading** — full simulation, positions logged to DB, no orders placed |
| `python3 main.py --dry-run` | **Observation** — prints alerts only, no DB writes at all |
| `python3 main.py --once` | Single scan then exit (combinable with any mode) |
| `python3 main.py --bankroll 2000` | Override starting bankroll |
| `python3 dashboard_server.py --paper` | **Web dashboard** — serve P&L dashboard at localhost:5000 |

**Recommended path to live:** `--dry-run` (sanity check) → `--paper` (weeks of simulation) → fund Kalshi account → live.

**Poll interval:** 15 minutes by default (overridable via `POLL_INTERVAL_SECONDS` in `.env`).
The bot skips the full API fetch when the daily cap or bankroll exposure limit is already reached,
instead running only auto-settle to free up capacity.

---

## Directory Structure

```
arbitrage_betting_bot/
├── main.py                     # Orchestrator: scan loop, mode flags
├── config.py                   # All tunable parameters, loaded from .env
├── .env                        # Environment variables (API keys, bankroll)
├── requirements.txt            # requests, dotenv, rapidfuzz, schedule, rich, cryptography, flask, pytz
├── test_connections.py         # Pre-flight check: verify all API keys work
├── dashboard.py                # Terminal P&L dashboard + manual settle CLI
├── dashboard_server.py         # Flask web dashboard (localhost:5000, phone-accessible)
│
├── data/                       # External API clients (read-only)
│   ├── odds_fetcher.py         # The Odds API → list[OddsEvent], season-aware filtering
│   ├── kalshi_client.py        # Kalshi Trade API → list[KalshiMarket] (series-based fetch)
│   └── kalshi_auth.py          # RSA-PSS request signing (shared by client + executor)
│
├── core/                       # Pure business logic (no I/O)
│   ├── market_matcher.py       # Fuzzy-match sportsbook events to Kalshi markets
│   ├── odds_converter.py       # De-vig sportsbook odds → consensus probability + stats
│   ├── value_detector.py       # Compare consensus prob vs Kalshi price → edge + filters
│   ├── kelly_calculator.py     # Fractional Kelly sizing with hard caps
│   ├── bankroll_manager.py     # Track total and per-sport exposure vs bankroll
│   └── correlation_tracker.py  # Block bets on correlated events (same game/team)
│
├── execution/                  # Order placement (live mode only)
│   ├── trade_executor.py       # Resolves YES/NO/TIE side, calls kalshi_executor
│   ├── kalshi_executor.py      # Places market buy orders on Kalshi REST API
│   └── auto_settle.py          # Polls Kalshi for resolved markets, auto-closes positions
│
├── alerts/
│   └── alert_manager.py        # Rich terminal output with [PAPER] / [DRY RUN] tags, times in PT
│
└── storage/
    └── db.py                   # SQLite layer (betting_bot.db)
```

---

## Scan Pipeline

Each scan executes these steps in order:

```
0. Early-exit check (saves API credits)
   If daily cap OR total exposure limit already reached:
     → run auto-settle only, skip fetch

1. Fetch
   OddsAPIClient   → list[OddsEvent]     (season-filtered, sharp books)
   KalshiClient    → list[KalshiMarket]  (series-based: KXNBAGAME, KXMLBGAME, etc.)
                                          Includes TIE markets for 3-way sports (MLS/EPL/UCL)

2. Match
   market_matcher.match_events()
   → sport-gated: NBA events only match NBA markets, etc.
   → two-team cross-validation: both teams must fuzzy-match before a market is accepted
   → rapidfuzz token matching on yes_team + no_team fields (threshold: 80/100)
   → TIE markets attached separately for 3-way soccer events
   → list[MatchedEvent]

3. Detect Value + Hard Filters
   value_detector.detect_value()
   → consensus_stats() → (mean_prob, bookmaker_count, std_dev)
   → Hard filters (skip if any fail):
       - bookmaker_count < MIN_BOOKMAKER_COUNT (5)
       - Kalshi spread > MAX_KALSHI_SPREAD (5¢)   ← uses ASK price for edge calc, not mid
       - Kalshi volume < MIN_KALSHI_VOLUME ($500)
   → edge = consensus_prob − kalshi_ask_price     ← fill price, not mid
   → keep if edge ≥ MIN_EDGE_THRESHOLD (4%)
   → Soccer Draw handled via 3-way de-vig against TIE market YES ask price
   → list[ValueOpportunity] (unsorted)

4. Score & Rank
   For each opportunity:
     a. Calculate Kelly sizing
     b. Composite score = edge × full_kelly_fraction × book_confidence × agreement
        book_confidence = min(bookmaker_count / 10, 1.0)
        agreement       = max(0, 1 - consensus_std × 10)
   Sort by composite score descending — best quality bets first

5. Iterate (in ranked order)
   a. Correlation check    → block if same game/team already open
   b. Exposure check       → block if bankroll limits would be exceeded
   c. Daily cap check      → stop if MAX_DAILY_ALERTS reached
   d. Log opportunity to DB
   e. Print alert (terminal) — game time displayed in Pacific Time
   f. Log alert to DB

   ┌─ PAPER MODE ──────────────────────────────────────────────────────┐
   │  add_position(is_paper=True, execution_status="paper")            │
   │  No order placed. Correlation tracker sees this position.         │
   └───────────────────────────────────────────────────────────────────┘
   ┌─ LIVE MODE ───────────────────────────────────────────────────────┐
   │  trade_executor.execute_trade(opp, sizing)                        │
   │  → kalshi_executor: POST /portfolio/orders (market buy)           │
   │  add_position(is_paper=False, order_id=..., execution_status=...) │
   └───────────────────────────────────────────────────────────────────┘

6. Auto-Settle
   auto_settle.auto_settle_positions()
   → GET /markets/{ticker} for each open position
   → If result = "yes"/"no"/"void": compute P&L, close position in DB
   → Runs after every scan and on every dashboard refresh
```

---

## Kalshi Authentication (`data/kalshi_auth.py`)

Kalshi's trading API requires every request to be RSA-signed. Both the market data client and
the order executor use this shared helper.

**Signing scheme:**
```
timestamp_ms = current Unix time in milliseconds
message      = timestamp_ms + METHOD + /path   (query string excluded)
signature    = RSA-PSS(SHA-256, MAX_LENGTH salt) applied to message
```

**Required headers on every authenticated request:**
```
KALSHI-ACCESS-KEY          → KALSHI_API_KEY from .env
KALSHI-ACCESS-TIMESTAMP    → timestamp_ms used in the signature
KALSHI-ACCESS-SIGNATURE    → base64(signature)
```

The RSA private key (PEM format) is stored at the path set in `KALSHI_PRIVATE_KEY_PATH`.
Recommended location: `~/.kalshi/private_key.pem` — outside the project directory and off git.

> **Note:** Kalshi uses **PSS padding** (not PKCS1v15). Using the wrong padding returns
> `INCORRECT_API_KEY_SIGNATURE` (401) even if the key and message are correct.

---

## Kalshi Market Structure

Kalshi sports markets are fetched by **series ticker** (not by category).
The `close_time` field is the **settlement deadline** (~2 weeks after the game), NOT the game time.
The actual game date is encoded in the event_ticker (e.g. `26APR14` = April 14, 2026).
Game times are sourced from The Odds API `commence_time` field (always accurate).

### Game-Winner (H2H) Series

| Sport | Odds API Key | Kalshi Series |
|---|---|---|
| NFL | `americanfootball_nfl` | `KXNFLGAME` |
| NCAAF | `americanfootball_ncaaf` | `KXNCAAFGAME` |
| NBA | `basketball_nba` | `KXNBAGAME` |
| NCAAB | `basketball_ncaab` | `KXNCAABGAME` |
| MLB | `baseball_mlb` | `KXMLBGAME` |
| NHL | `icehockey_nhl` | `KXNHLGAME` |
| MLS | `soccer_usa_mls` | `KXMLSGAME` |
| EPL | `soccer_epl` | `KXEPLGAME` |
| UCL | `soccer_uefa_champs_league` | `KXUCLGAME` |

Each game produces markets keyed by `yes_sub_title` (e.g. `"New York"`, `"Atlanta"`, `"Tie"`).
Prices are dollar-denominated strings (`yes_bid_dollars`, `yes_ask_dollars`).
**Edge is calculated using the ASK price** (actual fill cost), not the mid.

**Soccer 3-way markets:** EPL, UCL, and MLS each have three Kalshi markets per game (home / away / tie).
The matcher attaches the TIE market as a separate `MatchedEvent` with `kalshi_outcome="tie"`.
The draw probability is extracted from the sportsbook's 3-way h2h odds.

### Kalshi Market URL Format

```
https://kalshi.com/markets/{series_lower}/{slug}/{event_ticker_lower}
```

Known slugs:
- KXNBAGAME → `nba-game`
- KXMLBGAME → `mlb-game`
- KXNHLGAME → `nhl-game`
- KXEPLGAME → `epl-game`
- KXUCLGAME → `uefa-champions-league-game`
- KXMLSGAME → `mls-game`
- KXNFLGAME → `nfl-game`

### Cross-Team Validation (Matching)

The matcher validates **both** teams before accepting a match:
- `yes_team` must fuzzy-match one team (home or away)
- `no_team` (parsed from the market title) must fuzzy-match the other team
- This prevents cross-game mismatches (e.g. Orlando Magic matched to a different game's Orlando market)

---

## Execution Layer (`execution/kalshi_executor.py`)

Places a **market order** via `POST /trade-api/v2/portfolio/orders` (RSA-signed).

Contract math:
```
price_cents  = round(market_price * 100)
count        = floor(stake_dollars / (price_cents / 100))
buy_max_cost = ceil(stake_dollars * 100)   # cents — hard spending cap against slippage
```

Side resolution (`trade_executor.resolve_side()`):
- `Outcome.DRAW` → always `"yes"` (TIE market YES = draw occurs)
- `Outcome.HOME` → `kalshi_outcome` value from MatchedEvent
- `Outcome.AWAY` → opposite of `kalshi_outcome`

---

## Auto-Settlement (`execution/auto_settle.py`)

After each scan and on every dashboard refresh, open positions are checked against Kalshi:

```
GET /markets/{ticker} → market.result
  "yes"  → won if side == "yes", lost if side == "no"
  "no"   → won if side == "no",  lost if side == "yes"
  "void" → P&L = 0 (market cancelled, stake returned)
  ""     → still open, skip
```

P&L formula:
```
Win:  stake × (1 − entry_price) / entry_price
Loss: −stake
Void: 0
```

---

## Web Dashboard (`dashboard_server.py`)

```bash
python3 dashboard_server.py --paper    # paper mode
python3 dashboard_server.py            # live mode
```

Accessible at `http://localhost:5000` or `http://<mac-ip>:5000` on the same WiFi (phone-friendly).
Auto-refreshes every 60 seconds. All times displayed in **Pacific Time (PT)**.

**Panels:**
- Summary cards: Total P&L, Win Rate, ROI, Open Positions, Total Staked
- Bankroll over time (line chart with "at risk" overlay)
- Cumulative P&L chart
- Performance by sport
- Open Positions: Bet On, Opponent, Sport, Game Time (PT), Stake, Entry Price, Edge, Books, Spread, Potential Win, Status
- Settled Positions: P&L, WIN/LOSS badge
- Recent Value Detections

**Manual settle (terminal only):**
```bash
python3 dashboard.py --settle 5 --won
python3 dashboard.py --settle 5 --lost
```
Auto-settle handles this automatically once Kalshi publishes results.

---

## Core Logic

### De-Vigging (`odds_converter.py`)
`consensus_stats(bookmakers_data, outcome_name)` returns `(mean_prob, bookmaker_count, std_dev)`.
Uses the multiplicative method to remove the margin from each bookmaker's odds, then averages
across all books. `std_dev` measures how much books disagree — low = reliable signal.

### Opportunity Quality Filters (`value_detector.py`)
Three hard filters applied before any opportunity is ranked:
1. **Bookmaker count** ≥ 5 — consensus needs enough books to be reliable
2. **Kalshi spread** ≤ 5¢ — ensures the quoted price is actually fillable
3. **Kalshi volume** ≥ $500 — illiquid markets have unreliable prices

**Edge is computed using the ASK price** (what you actually pay to buy), not the bid-ask midpoint.
This avoids overstating edge by half the spread on every trade.

### Composite Scoring (`main.py`)
After Kelly is calculated, opportunities are ranked by:
```
score = edge × full_kelly_fraction × book_confidence × agreement
book_confidence = min(bookmaker_count / 10, 1.0)
agreement       = max(0, 1 − consensus_std × 10)
```
The top-scoring opportunities (up to `MAX_DAILY_ALERTS`) are selected — not just the first 5 by raw edge.

### Bet Sizing (`kelly_calculator.py`)
Full Kelly: `f* = (b·p − q) / b` where `b` = net decimal odds, `p` = consensus prob, `q` = 1 − p.

Bet size = `f* × KELLY_FRACTION × bankroll`, capped by:
- `MAX_BET_DOLLARS` — hard dollar cap (default $100)
- `MAX_PCT_BANKROLL` — max % of bankroll per bet (default 5%)

`KELLY_FRACTION` defaults to **0.25** (quarter-Kelly).

**Note on low-probability bets:** Kelly naturally produces smaller stakes for underdogs at equal edge
(the denominator `1 - market_price` is larger for low prices). A `MIN_CONSENSUS_PROB` filter
(not yet implemented) could further protect against high-variance longshots.

### Season Filtering (`odds_fetcher.py`)
Sports are automatically skipped when out of season to conserve Odds API credits:

| Sport | Season |
|---|---|
| NFL | Sep – Feb |
| NCAAF | Aug – Jan |
| NBA | Oct – Jun |
| NCAAB | Nov – Apr |
| MLB | Mar – Oct |
| NHL | Oct – Jun |
| MLS | Feb – Nov |
| EPL | Aug – May |
| UCL | Sep – May |

A 1-second delay between Odds API sport fetches prevents 429 rate-limit errors.

### Market Matching (`market_matcher.py`)
- **Sport-gated:** NBA events only match `KXNBAGAME` markets, MLS only `KXMLSGAME`, etc.
- **Two-team cross-validation:** Both `yes_team` (from `yes_sub_title`) and `no_team` (parsed
  from market title) must fuzzy-match the sportsbook event's two teams. Prevents cross-game mismatches.
- `rapidfuzz` (partial_ratio, token_sort_ratio, token_set_ratio) matches team names.
- Known abbreviations expanded before comparison (LA, NY, GS, KC, OKC, NO, TB, NE, SF).
- TIE markets matched via shared `event_ticker` after team-winner match is found.

---

## Database Schema (SQLite)

```sql
opportunities
  id, detected_at, sport, home_team, away_team, team_name,
  platform, consensus_prob, market_price, edge, market_url, alerted

alerts
  id, opportunity_id → opportunities, alerted_at, recommended_bet, bankroll_at_time

positions
  id, entered_at, sport, home_team, away_team, team_name,
  platform, stake, market_price, status (open/closed), is_paper,
  order_id, execution_status (pending/submitted/paper/failed),
  pnl, settled_at,           -- auto-populated by auto_settle
  market_ticker, side,       -- used by auto_settle to check Kalshi result
  edge, bookmaker_count, consensus_std, kalshi_spread,  -- quality signals at entry
  commence_time              -- game start time from Odds API (UTC ISO 8601)

bankroll_log
  id, log_date (unique), bankroll, total_at_risk
```

---

## Configuration (`config.py` / `.env`)

| Parameter | Default | Description |
|---|---|---|
| `ODDS_API_KEY` | — | The Odds API key (required) |
| `KALSHI_API_KEY` | — | Kalshi API key UUID (required) |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA private key PEM file |
| `BANKROLL` | 1000 | Starting bankroll in dollars |
| `POLL_INTERVAL_SECONDS` | 900 | Scan frequency (15 min default, env-overridable) |
| `KELLY_FRACTION` | 0.25 | Fraction of full Kelly to use |
| `MIN_EDGE_THRESHOLD` | 0.04 | Minimum edge (4%) to place a bet |
| `MAX_BET_DOLLARS` | 100 | Hard dollar cap per bet |
| `MAX_PCT_BANKROLL` | 0.05 | Max 5% of bankroll per single bet |
| `MAX_TOTAL_EXPOSURE_PCT` | 0.30 | Max 30% of bankroll deployed at once |
| `MAX_SPORT_EXPOSURE_PCT` | 0.15 | Max 15% of bankroll in one sport |
| `MAX_DAILY_ALERTS` | 5 | Max orders placed per day |
| `FUZZY_MATCH_THRESHOLD` | 80 | Minimum rapidfuzz score for event matching |
| `MIN_BOOKMAKER_COUNT` | 5 | Min books required for consensus reliability |
| `MAX_KALSHI_SPREAD` | 0.05 | Max Kalshi bid-ask spread (5¢) |
| `MIN_KALSHI_VOLUME` | 500 | Min Kalshi market volume ($) |

---

## Sports Monitored

| Sport | Odds API Key | In Season |
|---|---|---|
| NFL | `americanfootball_nfl` | Sep – Feb |
| NCAAF | `americanfootball_ncaaf` | Aug – Jan |
| NBA | `basketball_nba` | Oct – Jun |
| NCAAB | `basketball_ncaab` | Nov – Apr |
| MLB | `baseball_mlb` | Mar – Oct |
| NHL | `icehockey_nhl` | Oct – Jun |
| MLS | `soccer_usa_mls` | Feb – Nov |
| EPL | `soccer_epl` | Aug – May |
| UCL | `soccer_uefa_champs_league` | Sep – May |

---

## Planned Next Feature: Totals, Spreads, BTTS

A plan exists to add non-h2h bet types. Key decisions:

**Kalshi series confirmed:**
- Totals: `KXMLBTOTAL` (MLB, liquid ~$50K vol), `KXNBATOTAL`, `KXNHLTOTAL`, `KXEPLTOTAL`, `KXUCLTOTAL`, `KXMLSTOTAL`
- Spreads: `KXMLBSPREAD`, `KXNBASPREAD`, `KXNHLSPREAD`
- BTTS: `KXMLSBTTS`, `KXEPLBTTS`, `KXUCLBTTS` (all currently illiquid — EPL/UCL spreads 90–94¢)

**Only MLB totals are liquid today.** All others are infrastructure-only until Kalshi volume develops.

**Architecture:** `BetType` enum (H2H/TOTALS/SPREAD/BTTS), multi-series `_SPORT_TO_SERIES`,
non-h2h matching via shared event_ticker suffix, `market_key` param on `consensus_stats()`,
`bet_type`/`threshold` columns in DB, sport-specific Odds API market requests to control credit usage.

**11 files to modify:** config.py, kalshi_client.py, odds_fetcher.py, value_detector.py,
odds_converter.py, market_matcher.py, db.py, main.py, trade_executor.py, alert_manager.py,
dashboard_server.py.

---

## Pre-Flight Check

```bash
python3 test_connections.py
```

Tests:
1. The Odds API — confirms key valid, shows quota remaining
2. Kalshi private key — confirms PEM file parses correctly
3. Kalshi auth (read) — confirms RSA signing works against live API
4. Kalshi balance — confirms portfolio access

---

## Key Design Decisions

- **Kalshi only for execution.** All orders go through Kalshi (legal in California). Sportsbooks are pricing references only.
- **Fully automated, no approval step.** Single pipeline: detect → size → check risk → place order.
- **Quarter-Kelly sizing.** 0.25× Kelly reduces variance dramatically while preserving most edge.
- **Paper trading before live.** `--paper` runs the full pipeline including correlation/exposure enforcement — identical to live mode except no orders are placed.
- **Composite scoring over raw edge.** High-edge bets backed by few books or with wide spreads are ranked below lower-edge bets with stronger consensus.
- **Hard quality filters.** Markets with < 5 books, > 5¢ spread, or < $500 volume are eliminated before scoring — not just ranked lower.
- **ASK price for edge calculation.** The fill price (ask) is used rather than the mid, so displayed edge reflects what you actually pay.
- **Two-team cross-validation in matcher.** Both teams in a Kalshi market must match the sportsbook event — prevents cross-game mismatches that inflate apparent edge.
- **Sport-gated matching.** The matcher enforces sport boundaries to prevent cross-sport ticker mismatches.
- **Soccer 3-way handling.** EPL, UCL, and MLS regular season games can end in a draw. The bot de-vigs 3-way h2h odds and bets the TIE market when draw has edge.
- **Auto-settle.** Positions close automatically once Kalshi publishes a result. No manual tracking needed.
- **API credit conservation.** Off-season sports are skipped. Scans skipped entirely (auto-settle only) when daily cap or exposure limit reached. 1s delay between sport fetches prevents 429 errors.
- **Pacific Time display.** All game times and timestamps shown in PT throughout dashboard and terminal.
- **RSA-PSS not PKCS1v15.** Kalshi requires PSS padding. PKCS1v15 returns 401 even with a valid key.
- **Kalshi API base URL:** `https://api.elections.kalshi.com/trade-api/v2`
- **Series-based market fetch.** Kalshi removed category filtering. Markets fetched per series ticker with `series_ticker` param.
- **Kalshi close_time ≠ game time.** `close_time` is the settlement deadline (~2 weeks post-game). Game time comes from Odds API `commence_time`.
