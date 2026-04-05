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
├── requirements.txt            # requests, dotenv, rapidfuzz, schedule, rich, cryptography, flask
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
│   └── alert_manager.py        # Rich terminal output with [PAPER] / [DRY RUN] tags
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
                                          Includes TIE markets for 3-way sports (MLS)

2. Match
   market_matcher.match_events()
   → sport-gated: NBA events only match NBA markets, etc.
   → rapidfuzz token matching on yes_team field (threshold: 80/100)
   → TIE markets attached separately for MLS events
   → list[MatchedEvent]

3. Detect Value + Hard Filters
   value_detector.detect_value()
   → consensus_stats() → (mean_prob, bookmaker_count, std_dev)
   → Hard filters (skip if any fail):
       - bookmaker_count < MIN_BOOKMAKER_COUNT (5)
       - Kalshi spread > MAX_KALSHI_SPREAD (5¢)
       - Kalshi volume < MIN_KALSHI_VOLUME ($500)
   → edge = consensus_prob − kalshi_price
   → keep if edge ≥ MIN_EDGE_THRESHOLD (4%)
   → MLS Draw handled via 3-way de-vig against TIE market YES price
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
   e. Print alert (terminal)
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

Kalshi sports markets are fetched by **series ticker** (not by category):

| Sport | Odds API Key | Kalshi Series |
|---|---|---|
| NFL | `americanfootball_nfl` | `KXNFLGAME` |
| NCAAF | `americanfootball_ncaaf` | `KXNCAAFGAME` |
| NBA | `basketball_nba` | `KXNBAGAME` |
| NCAAB | `basketball_ncaab` | `KXNCAABGAME` |
| MLB | `baseball_mlb` | `KXMLBGAME` |
| NHL | `icehockey_nhl` | `KXNHLGAME` |
| MLS | `soccer_usa_mls` | `KXMLSGAME` |

Each game produces markets keyed by `yes_sub_title` (e.g. `"New York"`, `"Atlanta"`, `"Tie"`).
Prices are dollar-denominated strings (`yes_bid_dollars`, `yes_ask_dollars`). The mid of bid/ask
is used as the market price.

**MLS 3-way markets:** Each MLS game has three Kalshi markets (home / away / tie). The matcher
attaches the TIE market as a separate `MatchedEvent` with `kalshi_outcome="tie"`. The draw
probability is extracted from the sportsbook's 3-way h2h odds and compared against the TIE
market's YES price.

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
Auto-refreshes every 60 seconds.

**Panels:**
- Summary cards: Total P&L, Win Rate, ROI, Open Positions, Total Staked
- Bankroll over time (line chart with "at risk" overlay)
- Cumulative P&L chart
- Performance by sport
- Open Positions: Bet On, Opponent, Sport, Game Time, Stake, Entry Price, Edge, Books, Spread, Potential Win, Status
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

### Market Matching (`market_matcher.py`)
- **Sport-gated:** NBA events only match `KXNBAGAME` markets, MLS only `KXMLSGAME`, etc.
  Prevents cross-sport mismatches.
- `yes_team` field on each Kalshi market identifies who wins if YES resolves.
- `rapidfuzz` (partial_ratio, token_sort_ratio, token_set_ratio) matches team names.
- Known abbreviations expanded before comparison (LA, NY, GS, KC, OKC, etc.).

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
  commence_time              -- game start time from Odds API

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
- **Sport-gated matching.** The matcher enforces sport boundaries to prevent cross-sport ticker mismatches (e.g., an MLS team being matched to an NBA market).
- **MLS 3-way handling.** MLS regular season games can end in a draw. The bot correctly de-vigs the 3-way h2h odds and compares the draw probability against Kalshi's TIE market.
- **Auto-settle.** Positions close automatically once Kalshi publishes a result. No manual tracking needed.
- **API credit conservation.** Off-season sports are skipped. Scans are skipped entirely (auto-settle only) when daily cap or exposure limit is already reached.
- **RSA-PSS not PKCS1v15.** Kalshi requires PSS padding. PKCS1v15 returns 401 even with a valid key.
- **Kalshi API base URL:** `https://api.elections.kalshi.com/trade-api/v2` (migrated from `trading-api.kalshi.com`).
- **Series-based market fetch.** Kalshi removed category filtering from `/markets`. Markets are now fetched per series ticker (e.g. `KXNBAGAME`) with `series_ticker` param.
