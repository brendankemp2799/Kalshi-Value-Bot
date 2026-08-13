"""
Backtest of the two LATENCY-INSENSITIVE in-game MLS strategies.

WHY THESE ARE DIFFERENT FROM EVERYTHING TESTED SO FAR
-----------------------------------------------------
Both prior live-betting attempts failed on timing:
  - lead-change momentum: real edge, but ESPN publishes ~31s after the market moves,
    and the edge cannot fund a faster feed at this bankroll.
  - tape-only burst detection: 0 of 34 configurations profitable, because a tape
    detector fires *because* the price already moved.

These two do not react to an event at all. Their premise is that the market misprices
a **known, publicly visible state** -- the score and the clock. A data feed that is 30s
late is perfectly adequate, so 1-minute candlesticks suffice and there is no race.

STRATEGIES
----------
CLOCK DECAY -- pure state rules:
  CD-UNDER : score is 0-0 at minute M  -> buy UNDER 2.5 (NO on "Over 2.5")
  CD-TIE   : score is level at minute M -> buy TIE

POISSON DIVERGENCE -- a real fair value from (score, minutes remaining):
  Remaining goals ~ Poisson(lambda * remaining/90). For totals that gives an exact
  P(over N); for the 3-way it gives P(tie) via a Skellam-style convolution over the
  two teams' independent remaining-goal distributions. Bet whichever side Kalshi
  disagrees with by more than a threshold.

  lambda is ESTIMATED FROM THE SAMPLE ITSELF and reported, rather than assumed -- and
  see the caveat in the output about that being in-sample.

ENTRY / EXIT
------------
Entry at the 1-minute candle's yes_ask (to buy YES) or 1 - yes_bid (to buy NO), plus
the Kalshi taker fee. Held to settlement, which is what a small account would do --
so P&L is realized, not a mark.

Free data only: ESPN site API + Kalshi. Never touches the Odds API.

Run:
    python3 research/mls_latency_insensitive.py --dates $(python3 -c "...") --out ...
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data.kalshi_client import KalshiClient
from mls_ingame_state import (SOCCER_LEAGUES, MatchState, discover_dates,
                              match_states)

def taker_fee_cents(p: float) -> float:
    return config.KALSHI_TAKER_FEE_RATE_ESTIMATE * p * (1 - p) * 100


# ── Poisson helpers (no scipy in requirements.txt -- keep it stdlib) ───────────

def _pois_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def p_over(line: float, scored: int, lam_rem: float) -> float:
    """P(final total > line) given goals already scored and remaining-goal intensity."""
    need = math.floor(line - scored) + 1          # remaining goals needed to clear
    if need <= 0:
        return 1.0
    return 1.0 - sum(_pois_pmf(k, lam_rem) for k in range(need))


def p_tie(home_now: int, away_now: int, lam_h: float, lam_a: float, kmax: int = 12) -> float:
    """P(final scores level). Final tie <=> H_rem - A_rem == away_now - home_now."""
    d = away_now - home_now
    total = 0.0
    for k in range(0, kmax + 1):
        hk = k + d
        if hk < 0:
            continue
        total += _pois_pmf(hk, lam_h) * _pois_pmf(k, lam_a)
    return total


# ── Kalshi price lookup ───────────────────────────────────────────────────────

def _candle_prices(candles: list[dict], target_ts: float, tol: int = 240):
    """(yes_bid, yes_ask) from the first candle ending at/after target."""
    best = None
    for c in candles:
        ts = c.get("end_period_ts")
        if ts is None or ts < target_ts or ts > target_ts + tol:
            continue
        if best is None or ts < best[0]:
            bid = (c.get("yes_bid") or {}).get("close_dollars")
            ask = (c.get("yes_ask") or {}).get("close_dollars")
            if bid not in (None, "") and ask not in (None, ""):
                best = (ts, float(bid), float(ask))
    return (best[1], best[2]) if best else (None, None)


class MarketBook:
    """Resolves a match to its Kalshi tickers and caches their candlesticks.

    League-aware: each soccer league has its own Kalshi GAME/TOTAL series pair
    (see SOCCER_LEAGUES in research/mls_ingame_state.py)."""

    def __init__(self, kc: KalshiClient, game_series: str, total_series: str):
        self.kc = kc
        self.game_series = game_series
        self.total_series = total_series
        self._settled: dict[str, list[dict]] = {}
        self._candles: dict[str, list[dict]] = {}

    def _window(self, ms: MatchState):
        mn = int(ms.kickoff.timestamp()) - 3600
        return mn, mn + 9 * 3600

    def suffix_and_results(self, ms: MatchState):
        """Find the event suffix (e.g. '26AUG01PORSEA') from the 3-way markets, and
        the settled result of each market for this event. Totals titles carry no team
        names ('Will over X goals be scored?'), so the suffix must come from the
        KXMLSGAME tickers -- the same suffix-keying trick core/market_matcher.py uses."""
        from core.market_matcher import _team_score

        mn, mx = self._window(ms)
        key = f"{mn}"
        if key not in self._settled:
            out = []
            for series in (self.game_series, self.total_series):
                try:
                    out += self.kc.fetch_settled_markets_in_window(series, mn, mx)
                except Exception as e:
                    print(f"  markets {series}: {e}", file=sys.stderr)
            self._settled[key] = out
        settled = self._settled[key]

        best, bs = None, 0
        for m in settled:
            if not m["ticker"].startswith(self.game_series):
                continue
            for team in (ms.home_team, ms.away_team):
                s = _team_score(team, m.get("yes_sub_title", ""))
                if s >= config.FUZZY_MATCH_THRESHOLD and s > bs:
                    best, bs = m, s
        if best is None:
            return None, {}
        suffix = best["ticker"].split("-")[1]
        results = {m["ticker"]: m.get("result") for m in settled
                   if m["ticker"].split("-")[1] == suffix}
        return suffix, results

    def candles(self, ms: MatchState, ticker: str) -> list[dict]:
        if ticker not in self._candles:
            mn, mx = self._window(ms)
            series = ticker.split("-")[0]
            try:
                self._candles[ticker] = self.kc.fetch_candlesticks(
                    series, ticker, mn, mx, period_interval=1
                ).get("candlesticks", [])
            except Exception:
                self._candles[ticker] = []
        return self._candles[ticker]

    def price_at(self, ms: MatchState, ticker: str, when: datetime):
        return _candle_prices(self.candles(ms, ticker), when.timestamp())


# ── Backtest ──────────────────────────────────────────────────────────────────

def run(dates, minutes_under, minutes_tie, line, pois_edge, out_path, leagues):
    kc = KalshiClient()

    # (MatchState, MarketBook) pairs -- each league has its own Kalshi series pair.
    pairs: list[tuple[MatchState, MarketBook]] = []
    per_league: dict[str, int] = {}
    for lkey in leagues:
        slug, gser, tser = SOCCER_LEAGUES[lkey]
        book = MarketBook(kc, gser, tser)
        cnt = 0
        for d in dates:
            for ms in match_states(d, slug):
                pairs.append((ms, book))
                cnt += 1
        per_league[lkey] = cnt
        print(f"  {lkey:12} {slug:22} {cnt:4} matches", file=sys.stderr)

    all_matches = [m for m, _ in pairs]
    if not all_matches:
        print("no matches")
        return

    # Estimate goal intensities from the sample (reported; see in-sample caveat below)
    n = len(all_matches)
    lam_home = sum(m.final_home for m in all_matches) / n
    lam_away = sum(m.final_away for m in all_matches) / n
    lam_total = lam_home + lam_away
    print(f"\nSample: {n} matches over {len(dates)} dates, "
          f"{len(leagues)} leagues {per_league}")
    print(f"Goal intensity (full match): home {lam_home:.2f}  away {lam_away:.2f}  "
          f"total {lam_total:.2f}")

    trades = defaultdict(list)

    for ms, book in pairs:
        suffix, results = book.suffix_and_results(ms)
        if not suffix:
            continue

        over_ticker = f"{book.total_series}-{suffix}-{math.ceil(line)}"
        tie_ticker = f"{book.game_series}-{suffix}-TIE"

        # ---- CD-UNDER + Poisson on the totals market ----
        for minute in minutes_under:
            sc = ms.score_at(minute)
            wc = ms.wallclock_at(minute)
            if sc is None or wc is None:
                continue
            scored = sc[0] + sc[1]
            bid, ask = book.price_at(ms, over_ticker, wc)
            if bid is None:
                continue
            res = results.get(over_ticker)
            if res not in ("yes", "no"):
                continue
            rem = max(0.0, 90.0 - minute)
            lam_rem = lam_total * rem / 90.0
            fair_over = p_over(line, scored, lam_rem)

            # CD-UNDER: fires only when still 0-0
            if scored == 0:
                cost = (1 - bid) + 0.0
                if 0.02 < cost < 0.98:
                    won = res == "no"
                    trades[f"CD-UNDER@{minute}'"].append(
                        _mk(cost, won, fair=1 - fair_over, mkt=1 - bid, ms=ms))

            # Poisson divergence on the same market, any score
            mkt_over = ask                      # cost to buy YES(over)
            mkt_under = 1 - bid                 # cost to buy NO(under)
            if fair_over - mkt_over > pois_edge and 0.02 < mkt_over < 0.98:
                trades[f"POIS-OVER@{minute}'"].append(
                    _mk(mkt_over, res == "yes", fair=fair_over, mkt=mkt_over, ms=ms))
            elif (1 - fair_over) - mkt_under > pois_edge and 0.02 < mkt_under < 0.98:
                trades[f"POIS-UNDER@{minute}'"].append(
                    _mk(mkt_under, res == "no", fair=1 - fair_over, mkt=mkt_under, ms=ms))

        # ---- CD-TIE + Poisson on the tie market ----
        for minute in minutes_tie:
            sc = ms.score_at(minute)
            wc = ms.wallclock_at(minute)
            if sc is None or wc is None:
                continue
            bid, ask = book.price_at(ms, tie_ticker, wc)
            if bid is None:
                continue
            res = results.get(tie_ticker)
            if res not in ("yes", "no"):
                continue
            rem = max(0.0, 90.0 - minute)
            fair = p_tie(sc[0], sc[1], lam_home * rem / 90.0, lam_away * rem / 90.0)

            if sc[0] == sc[1] and 0.02 < ask < 0.98:
                trades[f"CD-TIE@{minute}'"].append(
                    _mk(ask, res == "yes", fair=fair, mkt=ask, ms=ms))

            if fair - ask > pois_edge and 0.02 < ask < 0.98:
                trades[f"POIS-TIE@{minute}'"].append(
                    _mk(ask, res == "yes", fair=fair, mkt=ask, ms=ms))

    _report(trades)
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"generated_at": datetime.utcnow().isoformat() + "Z",
                       "lam_home": lam_home, "lam_away": lam_away,
                       "n_matches": n, "trades": {k: v for k, v in trades.items()}},
                      f, indent=1)
        print(f"\nWrote {out_path}")


def _mk(cost, won, fair, mkt, ms):
    fee = taker_fee_cents(cost)
    return {
        "match": f"{ms.away_team} @ {ms.home_team}", "date": ms.date,
        # league + inferred are what let research/analyze_rules.py check that a pooled
        # edge is not an artifact of one segment, or of matches whose clock anchors were
        # inferred rather than ESPN-stamped.
        "league": ms.league, "inferred": bool(ms.anchors_inferred),
        "cost": round(cost, 4), "won": bool(won),
        "fair": round(fair, 4), "mkt": round(mkt, 4),
        "pnl_c": round((100 if won else 0) - cost * 100 - fee, 3),
    }


def _report(trades):
    print(f"\n{'='*94}")
    print(f"{'rule':22}{'n':>5}{'win%':>8}{'mean P&L':>11}{'median':>9}"
          f"{'total $':>10}{'ROI':>9}{'avg cost':>10}{'avg fair-mkt':>13}")
    print("=" * 94)
    for rule in sorted(trades):
        t = trades[rule]
        if not t:
            continue
        pnl = [x["pnl_c"] for x in t]
        wins = sum(1 for x in t if x["won"])
        cost = sum(x["cost"] for x in t) * 100
        edge = sum(x["fair"] - x["mkt"] for x in t) / len(t)
        print(f"{rule:22}{len(t):>5}{wins/len(t):>8.0%}{sum(pnl)/len(pnl):>+11.2f}"
              f"{median(pnl):>+9.2f}{sum(pnl)/100:>+10.2f}"
              f"{sum(pnl)/cost:>+9.1%}{sum(x['cost'] for x in t)/len(t):>10.2f}"
              f"{edge:>+13.3f}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dates", nargs="+")
    p.add_argument("--discover-dates", nargs=2, metavar=("START", "END"))
    p.add_argument("--minutes-under", type=float, nargs="+", default=[45, 55, 65, 75])
    p.add_argument("--minutes-tie", type=float, nargs="+", default=[65, 75, 80])
    p.add_argument("--line", type=float, default=2.5)
    p.add_argument("--pois-edge", type=float, default=0.05)
    p.add_argument("--out")
    p.add_argument("--leagues", nargs="+", default=list(SOCCER_LEAGUES),
                   choices=list(SOCCER_LEAGUES))
    args = p.parse_args()

    if args.discover_dates:
        print(" ".join(discover_dates(*args.discover_dates)))
        return
    if not args.dates:
        p.error("--dates required (or --discover-dates)")
    run(args.dates, args.minutes_under, args.minutes_tie, args.line,
        args.pois_edge, args.out, args.leagues)


if __name__ == "__main__":
    main()
