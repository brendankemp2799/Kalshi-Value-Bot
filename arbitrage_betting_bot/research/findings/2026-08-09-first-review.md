---
date: 2026-08-09
triggered_by: weekly
n_settled_live: 0
n_settled_since_last: n/a (no prior snapshot — this is the first review)
---

## Summary snapshot

`python3 research/metrics.py` against the local `storage/betting_bot.db`:

```json
{
  "generated_at": "2026-08-09T18:41:35.236179+00:00",
  "is_paper": false,
  "n_settled": 0,
  "n_open": 0,
  "roi_pct": null,
  "win_rate_pct": null,
  "sharpe": {"value": null, "n": 0},
  "max_drawdown": {"value": 0.0, "n_snapshots": 5},
  "by_sport": [],
  "by_bet_type": [],
  "by_strategy": [],
  "by_fill_type": [],
  "by_close_reason": [],
  "edge_calibration": [
    {"edge_bucket": "1.5-3%", "n": 0, "roi_pct": null, "win_rate_pct": null},
    {"edge_bucket": "3-5%", "n": 0, "roi_pct": null, "win_rate_pct": null},
    {"edge_bucket": "5-8%", "n": 0, "roi_pct": null, "win_rate_pct": null},
    {"edge_bucket": "8%+", "n": 0, "roi_pct": null, "win_rate_pct": null}
  ],
  "fees_and_fills": {"n": 0}
}
```

Querying `positions` directly (read-only, to sanity-check the zero) confirms: this
local DB has **9 total positions, all `is_paper = 1`** (7 settled paper trades + 2 open
paper trades, mostly dated 2026-07-18 through 2026-08-08, plus 2 `test`-sport rows from
2026-07-23). There are **zero `is_paper = 0` rows at all** — not "zero settled," zero
rows of any status.

## Anomalies / flags

**This is an environment/data-access issue, not a trading finding — flagging it clearly
so it isn't mistaken for "the bot stopped trading."**

The ground truth for this project states 38 settled *live* trades exist (18 MLB, 20
MLS) over ~3 weeks. `research/README.md` explains why this run can't see them: "the
production DB already lives [on the droplet, 167.172.148.64]," not on this laptop
checkout. This laptop's `storage/betting_bot.db` is a local/dev copy that only ever
had paper-mode runs against it. The README also notes explicitly that nobody has yet
added the cron entries that would run this research layer *on* the droplet — so as of
today, every invocation of `research/metrics.py` from this machine is structurally
blind to the real live-trade history, regardless of how "weekly" this review is.

Net effect: `n_settled=0` for live, `n_settled=7` for paper (too small and not
representative of live strategy performance — paper mode predates/differs from the
current live config — to treat as a proxy). No ROI, win-rate, sharpe, by-sport,
by-bet-type, or edge-calibration numbers are computable from data actually reachable
from here this run.

**Action item (not something this agent can do — it's an infra/ops step, out of
`research/` scope):** this review needs to run where `storage/betting_bot.db` actually
has the live trade history — i.e. on the droplet per `research/README.md`'s scheduling
section — or against a synced copy of the droplet's DB. Until then, weekly
Performance Analyst runs from this machine will keep reporting "no data" rather than
anything about the 38-trade live track record (H2H vs. totals, MLB vs. MLS, the
1.5-3% edge bucket, etc.) that's supposedly already known.

## New hypotheses this run

none — no live-trade data was reachable from this environment this run, so nothing
falsifiable could be derived. The known findings already on record (H2H underperforming
totals, MLB underperforming MLS, un-armed trailing stops as the historical dominant
loss source, the 1.5-3% edge bucket carrying volume while net negative) are unchanged
by this run because this run had no new data to compare them against — they should be
re-checked next time this script runs against the real production DB.
