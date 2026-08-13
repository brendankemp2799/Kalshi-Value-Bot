"""
MLB clock-decay / Poisson-divergence totals backtest.

PRE-REGISTERED: research/hypotheses/2026-08-13-mlb-clock-decay-totals.md
Rules, parameters, the lambda-estimation split and the pass bar were all fixed before
any MLB number was computed. Six rules only -- the soccer study tested fifteen, and
that multiple-testing burden is what made its single raw-CI winner unresolvable.

DESIGN CHOICES THAT DIFFER FROM THE SOCCER VERSION
--------------------------------------------------
1. lambda is estimated OUT OF SAMPLE (June games) and applied to the test period
   (July-August). The soccer study fitted lambda on the same matches it predicted,
   which flattered every POIS-* rule.
2. The "main line" per game is the ladder line nearest a reference total derived from
   the JUNE games only -- never from the game being traded, and never from outcomes in
   the test period.
3. End-of-inning wallclock is read straight from StatsAPI `about.endTime`, not inferred.

ENTRY / EXIT
------------
Buying UNDER = buying NO on "Over <line>", so cost = 1 - yes_bid at the 1-minute candle
covering the end of the inning, plus the Kalshi taker fee. Held to settlement.

Free data only: MLB StatsAPI + Kalshi. Never touches the Odds API.

Run:
    python3 research/mlb_latency_insensitive.py --lambda-dates <june...> \
        --test-dates <july/aug...> --out research/findings/mlb_clock_decay.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data.kalshi_client import KalshiClient
from mlb_ingame_state import GameState, game_states

SERIES_GAME = "KXMLBGAME"
SERIES_TOTAL = "KXMLBTOTAL"
INNINGS = [5, 6, 7]
POIS_EDGE = 0.05
REGULATION = 9


def taker_fee_cents(p: float) -> float:
    return config.KALSHI_TAKER_FEE_RATE_ESTIMATE * p * (1 - p) * 100


def _pois_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)


def p_over(line: float, scored: int, lam_rem: float) -> float:
    need = math.floor(line - scored) + 1
    if need <= 0:
        return 1.0
    return 1.0 - sum(_pois_pmf(k, lam_rem) for k in range(need))


def _candle_bid_ask(candles: list[dict], target_ts: float, tol: int = 300):
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


class MLBBook:
    """Resolves an MLB game to its Kalshi totals ladder and caches candlesticks."""

    def __init__(self, kc: KalshiClient):
        self.kc = kc
        self._settled: dict[str, list[dict]] = {}
        self._candles: dict[str, list[dict]] = {}

    def _window(self, g: GameState):
        mn = int(g.start.timestamp()) - 3600
        return mn, mn + 10 * 3600

    def ladder(self, g: GameState):
        """(suffix, {line: (ticker, result)}) for this game's totals ladder.

        The suffix comes from the KXMLBGAME markets, which carry team names in
        yes_sub_title; KXMLBTOTAL titles do too, but matching on the 3-way game series
        reuses the pattern already proven in the soccer study."""
        from core.market_matcher import _team_score

        # Cache the settled ladder PER DATE, not per game: ~13 games share a slate, and
        # keying on each game's own start time refetched the same markets once per game.
        key = g.date
        if key not in self._settled:
            day = datetime.fromisoformat(g.date).replace(tzinfo=g.start.tzinfo)
            mn = int(day.timestamp()) - 6 * 3600      # cover the whole slate + late games
            mx = mn + 42 * 3600
            out = []
            for series in (SERIES_GAME, SERIES_TOTAL):
                try:
                    out += self.kc.fetch_settled_markets_in_window(series, mn, mx)
                except Exception as e:
                    print(f"  markets {series}: {e}", file=sys.stderr)
            self._settled[key] = out
        settled = self._settled[key]

        best, bs = None, 0
        for m in settled:
            if not m["ticker"].startswith(SERIES_GAME):
                continue
            for team in (g.home_team, g.away_team):
                s = _team_score(team, m.get("yes_sub_title", ""))
                if s >= config.FUZZY_MATCH_THRESHOLD and s > bs:
                    best, bs = m, s
        if best is None:
            return None, {}
        suffix = best["ticker"].split("-")[1]

        rungs: dict[float, tuple[str, str]] = {}
        for m in settled:
            if not m["ticker"].startswith(SERIES_TOTAL):
                continue
            if m["ticker"].split("-")[1] != suffix:
                continue
            fs = m.get("floor_strike")
            if fs is None or m.get("result") not in ("yes", "no"):
                continue
            rungs[float(fs)] = (m["ticker"], m["result"])
        return suffix, rungs

    def candles(self, g: GameState, ticker: str) -> list[dict]:
        if ticker not in self._candles:
            mn, mx = self._window(g)
            try:
                self._candles[ticker] = self.kc.fetch_candlesticks(
                    SERIES_TOTAL, ticker, mn, mx, period_interval=1
                ).get("candlesticks", [])
            except Exception:
                self._candles[ticker] = []
        return self._candles[ticker]


def _mk(cost, won, fair, mkt, g, line, inning):
    fee = taker_fee_cents(cost)
    return {
        "game_pk": g.game_pk, "date": g.date,
        "matchup": f"{g.away_team} @ {g.home_team}",
        "line": line, "inning": inning, "extras": bool(g.went_extras),
        "cost": round(cost, 4), "won": bool(won),
        "fair": round(fair, 4), "mkt": round(mkt, 4),
        "pnl_c": round((100 if won else 0) - cost * 100 - fee, 3),
    }


def run(lambda_dates, test_dates, out_path):
    kc = KalshiClient()

    # ---- Estimate lambda + reference line on the LAMBDA period only ----
    lam_games: list[GameState] = []
    for d in lambda_dates:
        lam_games += game_states(d)
    if not lam_games:
        print("no games in lambda period", file=sys.stderr)
        return
    lam_total = sum(g.final_total for g in lam_games) / len(lam_games)
    ref_line = median([g.final_total for g in lam_games]) + 0.5
    print(f"\nLAMBDA PERIOD (out of sample): {len(lam_games)} games over "
          f"{len(lambda_dates)} dates")
    print(f"  mean final total = {lam_total:.2f} runs -> lambda")
    print(f"  median final total = {ref_line - 0.5:.1f} -> reference line {ref_line:.1f}")

    # ---- Evaluate rules on the TEST period only ----
    book = MLBBook(kc)
    trades = defaultdict(list)
    n_games = 0
    skipped = {"no_ladder": 0, "no_line": 0, "no_price": 0}

    for d in test_dates:
        for g in game_states(d):
            n_games += 1
            suffix, rungs = book.ladder(g)
            if not rungs:
                skipped["no_ladder"] += 1
                continue

            # PRE-REGISTERED line selection: the ladder rung whose YES price at first
            # pitch is closest to 0.50 -- the market's own centre, requiring no judgement
            # call and using no outcome information. Only rungs within +-3 of the
            # June-derived reference are probed, purely to bound the number of
            # candlestick fetches; the choice among them is made on price alone.
            cands = sorted(rungs, key=lambda L: abs(L - ref_line))[:5]
            best_line, best_gap = None, 1.0
            for L in cands:
                tk, _ = rungs[L]
                b, a = _candle_bid_ask(book.candles(g, tk), g.start.timestamp(), tol=900)
                if b is None:
                    continue
                mid = (b + a) / 2
                if abs(mid - 0.50) < best_gap:
                    best_line, best_gap = L, abs(mid - 0.50)
            if best_line is None:
                skipped["no_line"] += 1
                continue
            line = best_line
            ticker, result = rungs[line]

            candles = book.candles(g, ticker)
            for inn in INNINGS:
                st = g.state_at(inn)
                if st is None:
                    continue
                scored, when = st
                bid, ask = _candle_bid_ask(candles, when.timestamp())
                if bid is None:
                    skipped["no_price"] += 1
                    continue
                cost = 1 - bid                      # buying NO = UNDER
                if not (0.02 < cost < 0.98):
                    continue
                won = result == "no"                # UNDER wins when "over" settles no
                rem_innings = max(0, REGULATION - inn)
                lam_rem = lam_total * rem_innings / REGULATION
                fair_over = p_over(line, scored, lam_rem)
                fair_under = 1 - fair_over
                mkt_under = cost

                # Rule 1: on pace to finish under
                projected = scored * REGULATION / inn
                if projected < line:
                    trades[f"CD-UNDER@{inn}"].append(
                        _mk(cost, won, fair_under, mkt_under, g, line, inn))

                # Rule 2: Poisson divergence
                if fair_under - mkt_under >= POIS_EDGE:
                    trades[f"POIS-UNDER@{inn}"].append(
                        _mk(cost, won, fair_under, mkt_under, g, line, inn))

    print(f"\nTEST PERIOD: {n_games} games over {len(test_dates)} dates")
    print(f"skipped: {skipped}")
    print(f"\n{'rule':16}{'n':>6}{'win%':>8}{'mean P&L':>11}{'ROI':>9}"
          f"{'avg cost':>10}{'avg line':>10}{'extras%':>9}")
    for rule in sorted(trades):
        t = trades[rule]
        pnl = [x["pnl_c"] for x in t]
        cost = sum(x["cost"] for x in t) * 100
        wins = sum(1 for x in t if x["won"])
        print(f"{rule:16}{len(t):>6}{wins/len(t):>8.0%}{sum(pnl)/len(pnl):>+11.2f}"
              f"{sum(pnl)/cost:>+9.1%}{sum(x['cost'] for x in t)/len(t):>10.2f}"
              f"{sum(x['line'] for x in t)/len(t):>10.1f}"
              f"{sum(1 for x in t if x['extras'])/len(t):>9.0%}")

    if out_path:
        with open(out_path, "w") as f:
            json.dump({"generated_at": datetime.utcnow().isoformat() + "Z",
                       "lam_total": lam_total, "ref_line": ref_line,
                       "n_lambda_games": len(lam_games), "n_test_games": n_games,
                       "skipped": skipped, "trades": dict(trades)}, f, indent=1)
        print(f"\nWrote {out_path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lambda-dates", nargs="+", required=True)
    p.add_argument("--test-dates", nargs="+", required=True)
    p.add_argument("--out")
    a = p.parse_args()
    run(a.lambda_dates, a.test_dates, a.out)


if __name__ == "__main__":
    main()
