# Autonomous research layer (Phase 1)

A separate, read-only analysis system that mines the production bot's own trading data
for improvement hypotheses, investigates them with real backtests, and subjects every
finding to adversarial review — without incurring any Anthropic API/token cost.

**This never modifies the production trading system.** No file under `execution/`,
`core/`, `main.py`, `config.py`, or `storage/db.py` is ever touched by anything here.
Everything reads `storage/betting_bot.db` read-only.

## Why no API cost

This entire system runs through the `claude` CLI authenticated via your Claude Pro
subscription (`claude auth status` → `authMethod: "claude.ai"`), not an API key.

Auth for unattended/cron use: run `claude setup-token` once, interactively, on the
machine that will run these scripts — completing that login persists a credential at
`~/.claude/.credentials.json`, which cron picks up automatically (same user, same
`$HOME`). **Note:** `setup-token` also prints a separate portable
`CLAUDE_CODE_OAUTH_TOKEN` value explicitly meant for exactly this headless use case —
in testing (2026-08-09) that value consistently returned `401 Invalid bearer token`
here across multiple regenerations and a clean recreation, with root cause
unresolved, while the persisted login credential worked immediately and reliably. So
this system relies on the persisted-login path, not the token env var — see
`research/_load_token.sh` for the (optional, currently unnecessary) fallback if a
future Claude Code version fixes the token path and you want to use it instead.

No `ANTHROPIC_API_KEY` is set or needed anywhere in this system. Usage still draws from
your Pro plan's shared usage pool (the same one your interactive sessions use) — see
"Cadence" below for why this is deliberately lightweight.

## The pieces

- **`metrics.py`** — deterministic Python. All counting/averaging/backtesting math
  happens here or in scripts like it, never by an LLM reasoning over raw rows. Run it
  directly any time: `python3 research/metrics.py`.
- **`.claude/agents/performance-analyst.md`** — reviews `metrics.py` output, writes
  falsifiable hypotheses to `hypotheses/`. Never modifies code.
- **`.claude/agents/quant-research.md`** — picks up one open hypothesis, runs a real
  backtest against real data, writes a full experiment record to `experiments/`.
- **`.claude/agents/skeptic.md`** — adversarially reviews one experiment, either marks
  it `passed` or moves it to `failed_ideas/` with a documented reason.
- **`hypotheses/`, `experiments/`, `findings/`, `failed_ideas/`** — the permanent,
  git-trackable record. `_TEMPLATE.md` in each shows the expected structure.

No Engineering Agent and no PR automation exist yet — that's explicitly out of scope
for Phase 1. Nothing here can change what the live bot does.

## Cadence — and why it's not nightly

38 settled live trades accumulated over ~3 weeks ≈ 1.8/day. A nightly LLM pass would
mostly find nothing new while still drawing from the same Pro-plan usage pool every
single day, indefinitely. Instead:

1. **Daily, free, zero-LLM**: `python3 research/metrics.py --check-thresholds` — pure
   Python/SQLite, no `claude` call at all. Compares today's numbers to the last
   reviewed snapshot and writes `findings/TRIGGER_<date>.md` only if something crosses
   a real threshold (8+ new settled trades since last review, or a 15%+ drawdown —
   both configurable at the top of `metrics.py`).
2. **Weekly, or immediately if a TRIGGER file exists**: `./research/run_weekly.sh` —
   the Performance Analyst pass. This is the only thing that runs on a real LLM
   schedule by default.
3. **On-demand only**: `./research/run_deep.sh` — Quant Research → Skeptic over open
   hypotheses (capped at 3 per run via `MAX_HYPOTHESES`). Not calendar-scheduled at
   all in Phase 1; run it manually, or from the weekly script when something's worth
   chasing.

## Running it manually

```bash
cd arbitrage_betting_bot
python3 research/metrics.py                    # just print the current numbers
python3 research/metrics.py --check-thresholds  # the free daily check
./research/run_weekly.sh                        # Performance Analyst pass
./research/run_deep.sh                          # Quant Research + Skeptic on open hypotheses
```

## Scheduling it for real (not done automatically — you decide when)

This should run on the droplet (167.172.148.64), not a laptop that sleeps overnight —
the production DB already lives there and it's already always-on. Already done as of
2026-08-09: `claude` CLI installed (`curl -fsSL https://claude.ai/install.sh | bash`),
logged in via `claude setup-token`, `~/.local/bin` added to PATH, and
`./research/run_weekly.sh` verified end-to-end against real production data under a
fully wiped cron-equivalent environment (`env -i HOME=/root PATH=/usr/bin:/bin`) —
this is not theoretical, it produced a real hypothesis file from live data.

Then a crontab entry (`crontab -e` on the droplet):
```cron
# Free daily threshold check, 6:47am server time (off the hour, low collision risk)
47 6 * * * cd /opt/arbitrage-bot/arbitrage_betting_bot && python3 research/metrics.py --check-thresholds >> research/findings/threshold_check.log 2>&1

# Weekly Performance Analyst, Sunday 7:15am server time
15 7 * * 0 cd /opt/arbitrage-bot/arbitrage_betting_bot && ./research/run_weekly.sh >> research/findings/cron.log 2>&1
```

Nobody has added these yet — this is intentionally left for you to do explicitly once
you've watched a few manual runs and are comfortable with what they produce.
