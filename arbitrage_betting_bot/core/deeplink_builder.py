"""
Resolves the best available deep link for a flagged bet's (book, outcome)
pair, from The Odds API's includeLinks/includeSids data
(data/odds_fetcher.py::fetch_event_links()).

Validated live 2026-10-13 against a real MLS event across all five
config.RETAIL_BOOKMAKERS:
  fanduel         outcome-level `addToBetslip` link              -- best case
  draftkings      outcome-level `?outcomes=` link                -- best case
  betmgm          outcome-level link, but templated with a
                  literal "{state}" placeholder the API does
                  NOT fill in -- must substitute config.USER_STATE_CODE
  williamhill_us  same {state} templating as betmgm (this is Caesars' key)
  fanatics        NO links at all, at any level (event/market/outcome all
                  None) -- falls back to a hardcoded homepage URL

Fallback chain per outcome: outcome link -> market link -> event link ->
hardcoded homepage, with "{state}" substituted wherever a URL contains it.
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config
from core.odds_converter import _norm_team, _names_match

# Confirmed live 2026-10-13: Fanatics returns no links of any kind via the
# API. Hardcoded as a last resort -- update if Fanatics starts returning real
# links (re-run the validation query in fetch_event_links()'s docstring).
_HOMEPAGE: dict[str, str] = {
    "draftkings":     "https://sportsbook.draftkings.com/",
    "fanduel":        "https://sportsbook.fanduel.com/",
    "betmgm":         f"https://sports.{config.USER_STATE_CODE}.betmgm.com/",
    "williamhill_us": f"https://sportsbook.caesars.com/us/{config.USER_STATE_CODE}/bet/",
    "fanatics":       "https://sportsbook.fanatics.com/",
    "betrivers":      f"https://{config.USER_STATE_CODE}.betrivers.com/",
    "espnbet":        "https://espnbet.com/",
}

# bet_type (as stored on flagged_bets) -> Odds API market key used by
# fetch_event_links(). "spread" is singular on our side (see
# core/retail_value_detector.py), "spreads" plural on the API's.
MARKET_KEY_FOR_BET_TYPE: dict[str, str] = {
    "h2h": "h2h",
    "totals": "totals",
    "spread": "spreads",
}


def _fill_placeholders(url: str | None, stake: float | None = None) -> str | None:
    """Substitute the template placeholders The Odds API leaves in deep links.

    The API hands these back literally -- it does not know the user's state or
    how much they intend to wager -- so an unsubstituted link is a broken
    link. Observed live 2026-11-02:

      {state}        BetMGM, Caesars, BetRivers -- state-specific subdomain
                     or path segment (config.USER_STATE_CODE)
      {wagerAmount}  BetRivers -- stake, in its "coupon" parameter
                     (coupon=<pickType>|<selectionId>|<wagerAmount>).
                     The only book observed to accept a stake in the URL at
                     all; everywhere else the amount is typed in the slip.
      {pickType}     BetRivers -- "single" vs a parlay/combo coupon. Always
                     single here: this system never recommends a parlay, and
                     each flagged bet is sized independently by Kelly.

    A stake of None (or a book with no {wagerAmount}) just leaves the slip's
    amount blank, which is the pre-existing behaviour everywhere else.
    """
    if not url:
        return url
    if "{state}" in url:
        url = url.replace("{state}", config.USER_STATE_CODE)
    if "{pickType}" in url:
        url = url.replace("{pickType}", "single")
    if "{wagerAmount}" in url:
        # Whole dollars: the coupon parameter is parsed by the book, and a
        # trailing ".00" is noise at best. Blank when unknown rather than
        # guessing an amount into someone's bet slip.
        url = url.replace("{wagerAmount}", f"{stake:.0f}" if stake else "")
    return url


def extract_links(
    bookmakers: list[dict], book_key: str, market_key: str,
    outcome_name: str, point: float | None,
) -> tuple[str | None, str | None, str | None]:
    """(outcome_link, market_link, event_link) for one book/market/outcome
    from a fetch_event_links() response. Any may be None."""
    for book in bookmakers:
        if book.get("key") != book_key:
            continue
        event_link = book.get("link")
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            market_link = market.get("link")
            for o in market.get("outcomes", []):
                if point is not None:
                    if not _names_match(o.get("name", ""), outcome_name):
                        continue
                    if o.get("point") is None or abs(float(o["point"]) - point) > 0.01:
                        continue
                else:
                    if _norm_team(o.get("name", "")) != _norm_team(outcome_name):
                        continue
                return o.get("link"), market_link, event_link
        return None, None, event_link
    return None, None, None


def best_deep_link(
    book_key: str,
    outcome_link: str | None,
    market_link: str | None,
    event_link: str | None,
    stake: float | None = None,
) -> tuple[str, str]:
    """(url, quality), quality in {"outcome", "market", "event", "homepage"}."""
    for url, quality in (
        (outcome_link, "outcome"),
        (market_link, "market"),
        (event_link, "event"),
    ):
        filled = _fill_placeholders(url, stake)
        if filled:
            return filled, quality
    return _HOMEPAGE.get(book_key, "https://www.google.com/search?q=" + book_key), "homepage"


def resolve_deep_link(
    bookmakers: list[dict], book_key: str, bet_type: str,
    outcome_name: str, point: float | None, stake: float | None = None,
) -> tuple[str, str]:
    """One-call convenience: extract + pick the best link for a flagged bet's
    (book, outcome). `bet_type` is the flagged_bets value ("h2h"/"totals"/
    "spread"), translated internally to the Odds API's market key.

    `stake` fills the {wagerAmount} placeholder where a book supports one
    (BetRivers today) so the slip arrives pre-filled with the Kelly size."""
    market_key = MARKET_KEY_FOR_BET_TYPE.get(bet_type, bet_type)
    outcome_link, market_link, event_link = extract_links(
        bookmakers, book_key, market_key, outcome_name, point,
    )
    return best_deep_link(book_key, outcome_link, market_link, event_link, stake)
