#!/usr/bin/env python3
"""
validate.py — Live data accuracy report.

Runs the full data pipeline once and produces a structured report with four
sections answering the key accuracy questions.

Usage:
    cd arbitrage_betting_bot
    python3 validate.py

Cost: ~6-12 Odds API credits per run.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from core.market_matcher import match_events, _team_score, _kalshi_game_date
from core.odds_converter import (
    BOOK_WEIGHTS,
    DEFAULT_BOOK_WEIGHT,
    _names_match,
    _norm_team,
    american_to_prob,
    _devig,
)
from core.value_detector import detect_value
from data.kalshi_client import KalshiClient
from data.odds_fetcher import OddsAPIClient, OddsEvent


try:
    import pytz
    _PT = pytz.timezone("America/Los_Angeles")

    def _to_pt(dt: datetime) -> str:
        return dt.astimezone(_PT).strftime("%b %d %I:%M %p PT")
except ImportError:
    def _to_pt(dt: datetime) -> str:
        return dt.strftime("%b %d %H:%M UTC")


def _sport_short(sport_key: str) -> str:
    mapping = {
        "baseball_mlb": "MLB",
        "basketball_nba": "NBA",
        "icehockey_nhl": "NHL",
        "soccer_usa_mls": "MLS",
        "soccer_epl": "EPL",
        "americanfootball_nfl": "NFL",
        "soccer_spain_la_liga": "La Liga",
        "soccer_italy_serie_a": "Serie A",
        "soccer_france_ligue_one": "Ligue 1",
        "soccer_uefa_champs_league": "UCL",
    }
    return mapping.get(sport_key, sport_key.split("_")[-1].upper()[:3])


def _book_detail(
    bookmakers_data: list[dict],
    outcome_name: str,
    market_key: str = "h2h",
    point: float | None = None,
) -> list[dict]:
    """
    Per-book breakdown mirroring consensus_stats() logic but exposing
    intermediate values: raw implied prob, de-vigged prob, weight.

    Returns list of dicts: {book, bkey, weight, american, raw_prob, de_vigged}
    """
    _ALTERNATE: dict[str, str] = {"totals": "alternate_totals", "spreads": "alternate_spreads"}
    keys_to_check = {market_key, _ALTERNATE.get(market_key, market_key)}

    rows: list[dict] = []
    for book in bookmakers_data:
        book_key = book.get("key", "")
        weight = BOOK_WEIGHTS.get(book_key, DEFAULT_BOOK_WEIGHT)
        for market in book.get("markets", []):
            if market.get("key") not in keys_to_check:
                continue
            outcomes = market.get("outcomes", [])
            if point is not None:
                target = next(
                    (
                        o for o in outcomes
                        if _names_match(o.get("name", ""), outcome_name)
                        and o.get("point") is not None
                        and abs(float(o["point"]) - point) <= 0.01
                    ),
                    None,
                )
            else:
                norm_t = _norm_team(outcome_name)
                target = next(
                    (o for o in outcomes if _norm_team(o.get("name", "")) == norm_t),
                    None,
                )
            if target is None:
                continue
            american = target["price"]
            raw_prob = american_to_prob(american)
            all_probs = [american_to_prob(o["price"]) for o in outcomes]
            no_vig = _devig(all_probs)
            idx = outcomes.index(target)
            de_vigged = no_vig[idx]
            rows.append({
                "book":      book.get("title", book_key) or book_key,
                "bkey":      book_key,
                "weight":    weight,
                "american":  american,
                "raw_prob":  raw_prob,
                "de_vigged": de_vigged,
            })
    return rows


# ── Section A ─────────────────────────────────────────────────────────────────

def _section_a(
    matched_events: list,
    odds_events: list[OddsEvent],
) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("SECTION A — GAME MATCHING AUDIT")
    lines.append("=" * 72)
    lines.append("Are the right Kalshi markets matched to the right sportsbook games?")
    lines.append("")

    flag_count = 0

    for me in matched_events:
        event = me.odds_event
        km = me.kalshi_market
        sport = _sport_short(event.sport_key)
        bet = km.bet_type.upper()
        if km.threshold is not None:
            bet += f" {km.threshold}"

        # Fuzzy scores for yes_team and no_team
        if km.yes_team and km.yes_team.lower() not in ("tie", "over", "under"):
            if me.kalshi_outcome == "yes":
                yes_score = _team_score(event.home_team, km.yes_team)
                no_score = _team_score(event.away_team, km.no_team) if km.no_team else None
            elif me.kalshi_outcome == "no":
                yes_score = _team_score(event.away_team, km.yes_team)
                no_score = _team_score(event.home_team, km.no_team) if km.no_team else None
            else:
                yes_score = 100
                no_score = None
        else:
            yes_score = 100
            no_score = None

        # Date check
        kalshi_date = _kalshi_game_date(km.event_ticker)
        date_ok = True
        if kalshi_date is not None:
            delta = abs((kalshi_date.date() - event.commence_time.date()).days)
            date_ok = delta <= 1

        flags: list[str] = []
        if yes_score < 90:
            flags.append("⚠ LOW SCORE")
        if not date_ok:
            flags.append("⚠ DATE MISMATCH")
        if no_score is not None and no_score < 80:
            flags.append("⚠ TEAM MISMATCH")
        flag_count += len(flags)

        flag_str = "  " + "  ".join(flags) if flags else ""
        lines.append(
            f"[{sport} {bet}] {event.home_team} vs {event.away_team}"
            f"  {_to_pt(event.commence_time)}{flag_str}"
        )
        score_str = f"score={yes_score}" + (f"/{no_score}" if no_score is not None else "")
        lines.append(
            f"         Kalshi: {km.event_ticker:<38} YES={km.yes_team[:14]:<14}"
            f"NO={km.no_team[:14]:<14} {score_str}"
        )

    lines.append("")
    matched_ids = {me.odds_event.event_id for me in matched_events}
    unmatched = [e for e in odds_events if e.event_id not in matched_ids]
    lines.append(f"Matched pairs:              {len(matched_events)}")
    lines.append(f"Unmatched Odds API events:  {len(unmatched)}")
    if unmatched:
        for e in unmatched:
            lines.append(
                f"  UNMATCHED: {e.home_team} vs {e.away_team}"
                f" ({_sport_short(e.sport_key)}) {_to_pt(e.commence_time)}"
            )
    lines.append(f"Match audit flags raised:   {flag_count}")
    lines.append("")
    return lines


# ── Section B ─────────────────────────────────────────────────────────────────

def _section_b(scan_log: list[dict]) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("SECTION B — CONSENSUS CALCULATION AUDIT")
    lines.append("=" * 72)
    lines.append("Is the de-vigged consensus probability correct and reasonable?")
    lines.append("(Only entries with positive edge)")
    lines.append("")

    value_entries = [e for e in scan_log if e.get("status") == "value"]
    if not value_entries:
        lines.append("No value entries — nothing to audit.")
        lines.append("")
        return lines

    for entry in value_entries:
        bookmakers_data: list[dict] = json.loads(entry.get("bookmakers_json", "[]"))
        team_name: str = entry.get("team_name", "?")
        bet_type: str = entry.get("bet_type", "h2h")
        threshold: float | None = entry.get("threshold")
        consensus: float | None = entry.get("consensus_prob")
        kalshi_price: float | None = entry.get("kalshi_price")
        edge: float | None = entry.get("edge")
        book_count: int = entry.get("bookmaker_count", 0)
        std_dev: float = entry.get("consensus_std", 0.0)
        home: str = entry.get("home_team", "?")
        away: str = entry.get("away_team", "?")
        sport: str = _sport_short(entry.get("sport", ""))

        # Resolve lookup params for _book_detail()
        if bet_type == "totals":
            market_key = "totals"
            # For "Under X" entries, we display Over data (Under = 1 - Over)
            lookup_name = "Over"
            is_under = "under" in team_name.lower()
        elif bet_type == "spread":
            market_key = "spreads"
            # Strip the " +X.Y" / " -X.Y" suffix from the label to get team name
            m = re.match(r"^(.+?)\s+[+-][\d.]+$", team_name)
            lookup_name = m.group(1) if m else team_name
            is_under = False
        else:
            market_key = "h2h"
            lookup_name = team_name
            is_under = False

        rows = _book_detail(bookmakers_data, lookup_name, market_key=market_key, point=threshold)

        direction_note = " (Under = 1 - Over; Over data shown)" if is_under else ""
        lines.append(f"[{sport}] {home} vs {away} — {bet_type.upper()} ({team_name}){direction_note}")

        if rows:
            simple_mean = sum(r["de_vigged"] for r in rows) / len(rows)
            for r in rows:
                sign = "+" if r["american"] > 0 else ""
                lines.append(
                    f"  {r['book']:<22} {sign}{r['american']:>5}  "
                    f"→ implied {r['raw_prob']*100:5.1f}%"
                    f" → de-vigged {r['de_vigged']*100:5.1f}%"
                    f"   [wt {r['weight']:.2f}]"
                )
            lines.append(f"  {'─'*66}")
            c_str = f"{consensus*100:.1f}%" if consensus is not None else "?"
            k_str = f"{kalshi_price*100:.0f}¢" if kalshi_price is not None else "?"
            e_str = f"{edge*100:+.1f}%" if edge is not None else "?"
            lines.append(
                f"  CONSENSUS: {c_str}  ({book_count} books, std_dev={std_dev:.3f})"
                f"   Kalshi ask: {k_str}   Edge: {e_str}"
            )
            if edge is not None and edge > 0.08:
                lines.append(f"  ⚠ LARGE EDGE ({edge*100:.1f}%) — verify manually")
            if std_dev > 0.03:
                lines.append(f"  ⚠ HIGH STD DEV ({std_dev:.3f}) — books disagree significantly")
            if consensus is not None and abs(consensus - simple_mean) > 0.10:
                lines.append(
                    f"  ⚠ CONSENSUS OUTLIER — weighted={consensus*100:.1f}%"
                    f" vs simple mean={simple_mean*100:.1f}%"
                )
        else:
            lines.append("  (no per-book data recoverable from this entry)")

        lines.append("")

    return lines


# ── Section C ─────────────────────────────────────────────────────────────────

def _section_c(scan_log: list[dict]) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("SECTION C — EDGE SUMMARY TABLE")
    lines.append("=" * 72)
    lines.append("All opportunities with positive edge, sorted by edge descending.")
    lines.append("")

    positive = sorted(
        [e for e in scan_log if (e.get("edge") or 0) > 0],
        key=lambda x: x["edge"],
        reverse=True,
    )

    if not positive:
        lines.append("No positive-edge opportunities found.")
        lines.append("")
        return lines

    hdr = f"{'Sport':<5} {'Game':<28} {'Bet':<24} {'Books':>5} {'Consensus':>10} {'Kalshi':>7} {'Edge':>7}"
    lines.append(hdr)
    lines.append("─" * 88)

    for e in positive:
        sport = _sport_short(e.get("sport", ""))
        home = (e.get("home_team") or "?")[:13]
        away = (e.get("away_team") or "?")[:12]
        game = f"{home} vs {away}"
        bet = (e.get("team_name") or "?")[:23]
        books = e.get("bookmaker_count", 0)
        consensus = e.get("consensus_prob")
        kalshi = e.get("kalshi_price")
        edge = e.get("edge")
        c_str = f"{consensus*100:.1f}%" if consensus is not None else "?"
        k_str = f"{kalshi*100:.0f}¢" if kalshi is not None else "?"
        e_str = f"+{edge*100:.1f}%" if edge is not None else "?"
        lines.append(
            f"{sport:<5} {game:<28} {bet:<24} {books:>5} {c_str:>10} {k_str:>7} {e_str:>7}"
        )

    lines.append("")
    return lines


# ── Section D ─────────────────────────────────────────────────────────────────

def _section_d(
    odds_events: list[OddsEvent],
    kalshi_markets: list,
    matched_events: list,
    scan_log: list[dict],
) -> list[str]:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("SECTION D — PIPELINE HEALTH")
    lines.append("=" * 72)
    lines.append("Is the pipeline operating at expected volume?")
    lines.append("")

    by_sport: dict[str, int] = {}
    for e in odds_events:
        by_sport[e.sport_key] = by_sport.get(e.sport_key, 0) + 1

    lines.append(f"Odds API events fetched:     {len(odds_events)}")
    for sport, cnt in sorted(by_sport.items()):
        lines.append(f"  {_sport_short(sport):<6} {cnt}")

    h2h_km = sum(1 for km in kalshi_markets if km.bet_type == "h2h")
    non_h2h_km = len(kalshi_markets) - h2h_km
    lines.append(f"\nKalshi markets fetched:      {len(kalshi_markets)}")
    lines.append(f"  H2H/TIE:                   {h2h_km}")
    lines.append(f"  Totals/spread/other:        {non_h2h_km}")

    me_h2h = sum(1 for me in matched_events if me.kalshi_market.bet_type == "h2h")
    me_non = len(matched_events) - me_h2h
    matched_ids = {me.odds_event.event_id for me in matched_events}
    unmatched_cnt = sum(1 for e in odds_events if e.event_id not in matched_ids)
    lines.append(f"\nMatched pairs:               {len(matched_events)}")
    lines.append(f"  H2H/TIE:                   {me_h2h}")
    lines.append(f"  Totals/spread:             {me_non}")
    lines.append(f"Unmatched Odds API events:   {unmatched_cnt}")

    status_counts: dict[str, int] = {}
    for entry in scan_log:
        s = entry.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    lines.append(f"\nCandidates evaluated:        {len(scan_log)}")
    for status, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {status:<28} {cnt}")

    lines.append("")
    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    header = "=" * 72
    print(header)
    print("ARBITRAGE BOT — DATA ACCURACY VALIDATION")
    print(f"Run at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(header)
    print("Fetching live data (~6-12 Odds API credits)...")
    print()

    odds_client = OddsAPIClient()
    kalshi_client = KalshiClient()

    print("  [1/4] Fetching Odds API events...")
    odds_events = odds_client.fetch_all_sports()
    print(f"        → {len(odds_events)} upcoming events")

    print("  [2/4] Fetching Kalshi markets...")
    kalshi_markets = kalshi_client.fetch_sports_markets()
    print(f"        → {len(kalshi_markets)} markets")

    print("  [3/4] Matching events...")
    matched_events = match_events(odds_events, kalshi_markets)
    print(f"        → {len(matched_events)} matched pairs")

    print("  [4/4] Detecting value...")
    scan_log: list[dict] = []
    detect_value(matched_events, min_edge=0.0, scan_log=scan_log)
    value_count = sum(1 for e in scan_log if e.get("status") == "value")
    print(f"        → {len(scan_log)} candidates evaluated, {value_count} with positive edge")
    print()

    report_lines: list[str] = [
        "ARBITRAGE BOT — DATA ACCURACY VALIDATION REPORT",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    report_lines += _section_a(matched_events, odds_events)
    report_lines += _section_b(scan_log)
    report_lines += _section_c(scan_log)
    report_lines += _section_d(odds_events, kalshi_markets, matched_events, scan_log)

    report = "\n".join(report_lines)

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validation_report.txt")
    with open(report_path, "w") as f:
        f.write(report)

    print(report)
    print()
    print(f"Report saved to: {report_path}")


if __name__ == "__main__":
    main()
