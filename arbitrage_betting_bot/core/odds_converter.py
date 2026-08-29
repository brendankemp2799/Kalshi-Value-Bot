"""
Utility functions for converting between odds formats and implied probabilities.

American odds examples:
  +150  →  40.0%  implied probability
  -150  →  60.0%  implied probability
"""
from __future__ import annotations

# Sportsbook abbreviations → full city/region name.
# Some books (especially DraftKings) abbreviate team names in their spread/
# totals outcome data even though The Odds API provides full names at the
# event level (e.g. event.home_team = "Tampa Bay Rays" but DraftKings spread
# outcome = "TB Rays").  Normalizing both sides before comparison fixes this.
_ABBREV: dict[str, str] = {
    # Multi-letter city codes
    "TB":  "tampa bay",
    "LA":  "los angeles",
    "NY":  "new york",
    "GS":  "golden state",
    "KC":  "kansas city",
    "SF":  "san francisco",
    "NE":  "new england",
    "NO":  "new orleans",
    "OKC": "oklahoma city",
    "LV":  "las vegas",
    "SD":  "san diego",
    # MLB three-letter codes
    "NYY": "new york",   "NYM": "new york",
    "BOS": "boston",     "TOR": "toronto",
    "BAL": "baltimore",  "TB":  "tampa bay",
    "CLE": "cleveland",  "DET": "detroit",
    "CWS": "chicago",    "CHW": "chicago",
    "MIN": "minnesota",  "KCR": "kansas city",
    "HOU": "houston",    "TEX": "texas",
    "OAK": "oakland",    "SEA": "seattle",
    "LAA": "los angeles","ATH": "athletics",
    "NYY": "new york",   "BOS": "boston",
    "ATL": "atlanta",    "MIA": "miami",
    "NYM": "new york",   "PHI": "philadelphia",
    "WSH": "washington", "WAS": "washington",
    "CHC": "chicago",    "MIL": "milwaukee",
    "STL": "st. louis",  "PIT": "pittsburgh",
    "CIN": "cincinnati", "COL": "colorado",
    "ARI": "arizona",    "SF":  "san francisco",
    "LAD": "los angeles","SDP": "san diego",
    # NBA/NHL three-letter codes
    "LAL": "los angeles", "LAC": "los angeles",
    "GSW": "golden state","PHX": "phoenix",
    "PHL": "philadelphia","PHI": "philadelphia",
    "IND": "indiana",     "MIL": "milwaukee",
    "CLE": "cleveland",   "DET": "detroit",
    "CHI": "chicago",     "ATL": "atlanta",
    "MIA": "miami",       "ORL": "orlando",
    "WAS": "washington",  "BKN": "brooklyn",
    "TOR": "toronto",     "BOS": "boston",
    "SAS": "san antonio", "DAL": "dallas",
    "DEN": "denver",      "POR": "portland",
    "SAC": "sacramento",  "MEM": "memphis",
    "NOP": "new orleans", "OKC": "oklahoma city",
    "UTA": "utah",        "MIN": "minnesota",
    # NFL
    "NE":  "new england", "NO":  "new orleans",
    "KC":  "kansas city", "LV":  "las vegas",
    "LAR": "los angeles", "LAC": "los angeles",
    "JAX": "jacksonville","JAC": "jacksonville",
    "TEN": "tennessee",   "IND": "indianapolis",
    "CIN": "cincinnati",  "PIT": "pittsburgh",
    "BAL": "baltimore",   "CLE": "cleveland",
    "DAL": "dallas",      "NYG": "new york",
    "PHI": "philadelphia","WAS": "washington",
    "CHI": "chicago",     "DET": "detroit",
    "GB":  "green bay",   "MIN": "minnesota",
    "ATL": "atlanta",     "CAR": "carolina",
    "TB":  "tampa bay",   "ARI": "arizona",
    "SEA": "seattle",     "SF":  "san francisco",
    "HOU": "houston",     "BUF": "buffalo",
}


import re as _re
import unicodedata as _unicodedata

# Whole-name nicknames that the punctuation strip below would otherwise mangle
# into unusable single-letter tokens. Kalshi spread subtitles write the Oakland/
# Sacramento Athletics as "A's wins by X.Y runs or more" -- stripping the
# apostrophe splits that into ["A", "s"], and _sb_team_scores filters out
# single-character words entirely, so the team scored 0 against BOTH sides of
# every matchup (not just a same-city collision) and every A's spread market
# was silently skipped as "ambiguous". Checked as a whole string before the
# punctuation strip, since splitting on the apostrophe is exactly what breaks it.
_NICKNAME_ALIASES: dict[str, str] = {
    "a's": "athletics",
}


def _norm_team(name: str) -> str:
    """
    Normalize a team name for comparison:
      - Strip punctuation ("St." → "St", "St. Louis" → "St Louis")
      - Expand common abbreviations ("TB" → "tampa bay", "MIN" → "minnesota")
      - Lowercase everything
    e.g. "TB Rays" → "tampa bay rays", "St. Louis Cardinals" → "st louis cardinals"
    """
    # Fold accents first: Kalshi writes "Ronald Acuna Jr." where The Odds API writes
    # "Ronald Acuña Jr.". The participant filter below compares these for EXACT
    # equality, so without folding every accented player -- and "Atletico Madrid" --
    # silently finds no consensus and is never priced. \w keeps "ñ" as a word char,
    # so the punctuation strip on the next line does not do this for us.
    name = "".join(c for c in _unicodedata.normalize("NFKD", name or "")
                   if not _unicodedata.combining(c))
    nickname = _NICKNAME_ALIASES.get(name.strip().lower())
    if nickname is not None:
        return nickname
    # Remove punctuation except hyphens inside words
    cleaned = _re.sub(r"[^\w\s-]", " ", name)
    words = cleaned.strip().split()
    out = []
    for w in words:
        out.append(_ABBREV.get(w.upper(), w).lower())
    return " ".join(out)


def _names_match(a: str, b: str) -> bool:
    """
    Return True if two team name strings refer to the same team.

    Used for spread/totals market lookups where sportsbooks may use
    shortened or abbreviated forms of team names:
      "Pittsburgh"        matches "Pittsburgh Pirates"   (city-only)
      "TB Rays"           matches "Tampa Bay Rays"       (abbreviation)
      "St Louis Blues"    matches "St. Louis Blues"      (punctuation)
      "Los Angeles"       matches "Los Angeles Clippers" (city-only)

    Does NOT match two different teams that share only a city name:
      "Los Angeles Angels" ≠ "Los Angeles Dodgers"  (different mascots)
      "New York Yankees"   ≠ "New York Mets"        (different mascots)

    This is safe when combined with a point-value filter (spread markets
    have distinct point values per side, so we can't accidentally match
    the wrong team even with permissive name matching).
    """
    na, nb = _norm_team(a), _norm_team(b)
    if na == nb:
        return True
    # Substring containment handles city-only vs full name:
    #   "pittsburgh" in "pittsburgh pirates" ✓
    #   "los angeles" in "los angeles dodgers" ✓  (city-only lookup)
    # But we must NOT match "los angeles angels" ⊂ "los angeles dodgers"
    # because neither is a substring of the other — that falls through to
    # the word-overlap check below, which we guard more carefully.
    if na in nb or nb in na:
        return True
    # Shared significant words (>3 chars) as a last resort.
    # Guard: only match if one side has NO unshared significant words —
    # i.e. one name is a strict subset of the other (city-only vs full name).
    # "pittsburgh" vs "pittsburgh pirates": a_words={"pittsburgh"},
    #   b_words={"pittsburgh","pirates"}, shared={"pittsburgh"},
    #   a has no unshared words → match ✓
    # "los angeles angels" vs "los angeles dodgers": shared={"angeles"},
    #   a_unshared={"angels"}, b_unshared={"dodgers"} → both have unshared → no match ✓
    a_words = {w for w in na.split() if len(w) > 3}
    b_words = {w for w in nb.split() if len(w) > 3}
    if a_words and b_words:
        shared = a_words & b_words
        if shared:
            a_unshared = a_words - shared
            b_unshared = b_words - shared
            # Only match if one side is fully contained in the other
            if not a_unshared or not b_unshared:
                return True
    return False


# Sharpness weights by Odds API book key (US region).
#
# Pinnacle is NOT available in the Odds API "us" region — omitted.
# Sharpest books actually available in "us":
#   LowVig / BetOnline: reduced-vig / offshore, attract sharp money
#   DraftKings / FanDuel: dominant US retail, highly accurate due to volume
#   BetMGM / Caesars / BetRivers: solid mid-tier retail
#   Bovada / MyBookie: softest offshore lines
#   Barstool rebranded to ESPN Bet (espnbet) in late 2023; kept at low weight
#   for any historical data that still carries the old key.
#
# Any book not listed falls back to DEFAULT_BOOK_WEIGHT.
BOOK_WEIGHTS: dict[str, float] = {
    # ── Sharpest global books ─────────────────────────────────────────────────
    "pinnacle":       1.0,   # gold standard — tightest vig, most accurate line
    # Betting exchanges (prices set by market, not a book — highly efficient)
    "betfair_ex_eu":  0.9,
    "betfair_ex_uk":  0.9,
    "matchbook":      0.85,
    "smarkets":       0.85,
    # Sharp-leaning US/offshore
    "lowvig":         0.9,
    "betonlineag":    0.8,
    "betanysports":   0.7,   # reduced vig, sharp-leaning
    "marathonbet":    0.75,  # sharp EU offshore book
    "betus":          0.7,
    # ── Large US retail ───────────────────────────────────────────────────────
    "draftkings":     0.7,
    "fanduel":        0.7,
    "espnbet":        0.65,
    # ── Mid-tier US retail ────────────────────────────────────────────────────
    "betmgm":         0.55,
    "caesars":        0.55,
    "williamhill_us": 0.55,
    "williamhill":    0.55,
    "betrivers":      0.5,
    "unibet_us":      0.5,
    "superbook":      0.5,
    "wynnbet":        0.45,
    "betway":         0.45,
    # ── EU / UK sportsbooks ───────────────────────────────────────────────────
    "winamax_fr":     0.65,  # large French book, competitive pricing
    "winamax_de":     0.65,
    "coolbet":        0.6,
    "betsson":        0.55,
    "betclic_fr":     0.5,
    "betfair_sb_uk":  0.5,   # Betfair sportsbook (not the exchange — softer)
    "coral":          0.5,
    "ladbrokes_uk":   0.5,
    "paddypower":     0.5,
    "tipico_de":      0.5,
    "unibet_uk":      0.5,
    "unibet_fr":      0.5,
    "unibet_se":      0.5,
    "unibet_nl":      0.5,
    "betvictor":      0.5,
    "sport888":       0.5,
    "nordicbet":      0.5,
    "betano_uk":      0.5,
    "leovegas":       0.45,
    "leovegas_se":    0.45,
    "everygame":      0.45,
    "casumo":         0.4,
    "grosvenor":      0.4,
    "livescorebet":   0.4,
    "virginbet":      0.4,
    # ── Softer offshore ───────────────────────────────────────────────────────
    "bovada":         0.4,
    "gtbets":         0.35,
    "pointsbet":      0.35,
    "mybookieag":     0.3,
    "onexbet":        0.3,   # operates outside licensing norms — low trust
    "barstool":       0.3,   # defunct / rebranded to espnbet
    "fliff":          0.25,
}
DEFAULT_BOOK_WEIGHT: float = 0.5  # fallback for any unlisted book


def american_to_prob(odds: int) -> float:
    """Convert American odds to raw implied probability (includes vig)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    else:
        abs_odds = abs(odds)
        return abs_odds / (abs_odds + 100.0)


def prob_to_american(p: float) -> int:
    """Convert probability (0-1) to American odds integer."""
    if p <= 0 or p >= 1:
        raise ValueError(f"Probability must be between 0 and 1, got {p}")
    if p < 0.5:
        return round(100.0 / p - 100.0)
    else:
        return round(-(p * 100.0) / (1.0 - p))


def remove_vig(probs: list[float]) -> list[float]:
    """
    Normalize a list of raw implied probabilities so they sum to 1.
    This removes the bookmaker's vig (overround).

    Example: [0.5263, 0.5263]  →  [0.5, 0.5]
    """
    total = sum(probs)
    if total == 0:
        raise ValueError("Probabilities sum to zero")
    return [p / total for p in probs]


def _shin_z(raw_probs: list[float], overround: float) -> float:
    """
    Solve for Shin's (1992/1993) implied insider-trading proportion z via
    bisection. Fair probabilities under z are:
        p_i(z) = (sqrt(z**2 + 4*(1-z)*raw_i**2/O) - z) / (2*(1-z))
    sum_i p_i(z) is 1 at z=0 (it's sqrt(O)*O/O = sqrt(O) > 1 for O>1) and
    strictly decreasing in z, crossing 1 at the z we want (0 <= z < 0.5).
    """
    def total(z: float) -> float:
        return sum(
            ((z ** 2 + 4 * (1 - z) * p ** 2 / overround) ** 0.5 - z) / (2 * (1 - z))
            for p in raw_probs
        )

    lo, hi = 0.0, 0.4999
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if total(mid) > 1.0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def remove_vig_shin(probs: list[float]) -> list[float]:
    """
    De-vig via Shin's method, which corrects for favorite-longshot bias:
    proportional normalization (remove_vig) overstates the fair probability
    of favorites and understates it for longshots, because bookmaker margin
    isn't spread evenly across outcomes. Shin's method models the overround
    as coming from a proportion `z` of informed ("insider") money and solves
    for the fair probabilities implied by that model.

    Falls back to proportional normalization when the market has no overround
    (sum <= 1) — a degenerate case Shin's formula isn't defined for.
    """
    overround = sum(probs)
    if overround <= 1.0:
        return remove_vig(probs)
    z = _shin_z(probs, overround)
    return [
        ((z ** 2 + 4 * (1 - z) * p ** 2 / overround) ** 0.5 - z) / (2 * (1 - z))
        for p in probs
    ]


def _devig(probs: list[float]) -> list[float]:
    """Dispatch to the configured de-vig method (config.DEVIG_METHOD)."""
    import config
    if config.DEVIG_METHOD == "shin":
        return remove_vig_shin(probs)
    return remove_vig(probs)


def consensus_stats(
    bookmakers_data: list[dict],
    outcome_name: str,
    market_key: str = "h2h",
    point: float | None = None,
    participant: str | None = None,
) -> tuple[float | None, int, float]:
    """
    Compute de-vigged consensus probability plus quality metrics.

    Args:
        bookmakers_data — The Odds API bookmakers list for an event
        outcome_name    — The outcome to measure (team name, "Over", "Under",
                          "Draw", "Yes", "No")
        market_key      — Odds API market type: "h2h", "totals", "spreads", "btts"
        point           — For totals/spreads: filter to markets with this line
                          value (within ±0.26 tolerance). None = accept any line.
        participant     — For PLAYER props: the player the line belongs to. Player
                          markets put "Over"/"Under" in `name` and the player in
                          `description`, and several players share one market, often
                          at the SAME line. Without this filter the de-vig would pair
                          one player's Over against another player's Under.

    Returns:
        (weighted_mean, bookmaker_count, weighted_std_dev)
        weighted_mean   — sharpness-weighted de-vigged probability
                          (None if no book carries it)
        bookmaker_count — number of books with odds for this outcome
        weighted_std_dev— weighted std dev of per-book probs (0 = perfect agreement)

    Note: for "totals" and "spreads", the corresponding "alternate_totals" /
    "alternate_spreads" markets are also checked so that non-main-line Kalshi
    markets (e.g. Over 9.5 when the main line is 8.5) can find consensus. The three
    MLB player-prop markets get the same treatment (2026-08-24): Pinnacle only ever
    populates the featured rung, so config.PROP_ALTERNATE_MARKETS buys the rest of
    the ladder from a second book (config.PROP_ALTERNATE_BOOKMAKERS) and merges it
    in under the "_alternate" market key, exactly like alternate_totals.
    """
    # Build the set of Odds API market keys to search
    _ALTERNATE: dict[str, str] = {
        "totals":  "alternate_totals",
        "spreads": "alternate_spreads",
        "pitcher_strikeouts":  "pitcher_strikeouts_alternate",
        "batter_home_runs":    "batter_home_runs_alternate",
        "batter_total_bases":  "batter_total_bases_alternate",
    }
    keys_to_check = {market_key, _ALTERNATE.get(market_key, market_key)}

    weighted_probs: list[tuple[float, float]] = []  # (weight, de_vigged_prob)

    for book in bookmakers_data:
        book_key = book.get("key", "")
        weight = BOOK_WEIGHTS.get(book_key, DEFAULT_BOOK_WEIGHT)

        for market in book.get("markets", []):
            if market.get("key") not in keys_to_check:
                continue
            outcomes = market.get("outcomes", [])

            # Player markets carry several players in ONE market, frequently at the
            # same line. Narrow to this player before anything else, or both the
            # target lookup and the de-vig pairing below can cross players.
            if participant:
                want = _norm_team(participant)
                outcomes = [o for o in outcomes
                            if _norm_team(o.get("description", "")) == want]
                if not outcomes:
                    continue

            # For spreads/totals: exact point match + fuzzy name match.
            # _names_match handles city-only ("Pittsburgh" → "Pittsburgh Pirates"),
            # abbreviations ("TB Rays" → "Tampa Bay Rays"), and punctuation
            # differences ("St. Louis" → "St Louis").
            # Using fuzzy name matching is safe here because the point value
            # provides a second filter — two outcomes in the same spread market
            # always have opposite-sign points, so we can't match the wrong team.
            #
            # For H2H (point is None): use exact normalized match to avoid
            # false positives between same-city teams (e.g. Cubs vs White Sox).
            if point is not None:
                target = next(
                    (o for o in outcomes
                     if _names_match(o.get("name", ""), outcome_name)
                     and o.get("point") is not None
                     and abs(float(o["point"]) - point) <= 0.01),
                    None,
                )
            else:
                norm_target = _norm_team(outcome_name)
                target = next(
                    (o for o in outcomes
                     if _norm_team(o.get("name", "")) == norm_target),
                    None,
                )

            if target is None:
                continue

            # De-vig against the outcomes that form ONE market with `target`, not
            # against every outcome the book returned under this key.
            #
            # A featured `totals` market has exactly two outcomes (Over/Under at one
            # line) so the distinction never mattered. `alternate_totals` carries the
            # whole ladder -- ~28 outcomes across 14 lines -- and normalising across
            # all of them treats mutually compatible bets as if they were exclusive
            # alternatives. Measured on live data before this fix: Over 1.5 priced at
            # -4500 (implied 0.978) came back as 0.1316, and an Over/Under pair summed
            # to 0.65 instead of 1.0. Latent until now only because alternate markets
            # were never actually fetched (the bulk endpoint 422s on them).
            #
            # Pairing is by ABSOLUTE line value, which is correct for both shapes:
            #   totals  -> Over 8.5 / Under 8.5   (same point)
            #   spreads -> Team A -1.5 / Team B +1.5   (negated point)
            # H2H (point is None) keeps whole-market de-vigging, which is right: its
            # 2- and 3-way outcomes ARE the complete exclusive set.
            if point is not None and target.get("point") is not None:
                tp = abs(float(target["point"]))
                siblings = [
                    o for o in outcomes
                    if o.get("point") is not None
                    and abs(abs(float(o["point"])) - tp) <= 0.01
                ]
            else:
                siblings = outcomes

            if target not in siblings:      # defensive; target always matches itself
                siblings = outcomes

            # A de-vig needs the opposing side to normalize against. One outcome
            # alone isn't "no vig to remove" -- remove_vig([p]) is p/p, which is 1.0
            # for ANY price, a fabricated certainty rather than a real probability.
            # Verified live 2026-08-24: DraftKings' alternate player-prop ladder
            # sends Over-only, no Under at any point, for every rung. Skipping a
            # one-sided quote (rather than "de-vigging" it into 1.0) is the same
            # no-consensus-over-a-guess rule this function already applies when no
            # book quotes the point at all.
            if len(siblings) < 2:
                continue

            probs = [american_to_prob(o["price"]) for o in siblings]
            no_vig = _devig(probs)
            weighted_probs.append((weight, no_vig[siblings.index(target)]))

    if not weighted_probs:
        return None, 0, 0.0

    total_weight = sum(w for w, _ in weighted_probs)
    mean = sum(w * p for w, p in weighted_probs) / total_weight
    variance = sum(w * (p - mean) ** 2 for w, p in weighted_probs) / total_weight
    std_dev = variance ** 0.5
    return mean, len(weighted_probs), round(std_dev, 6)


def _raw_prob_at_point(
    bookmakers_data: list[dict], book_key: str, market_key: str,
    point: float, participant: str, name: str = "Over",
) -> float | None:
    """Raw (still-vigged) implied probability for one book/market/point/player.

    Deliberately does NOT de-vig -- there is usually only one outcome (see
    scaled_alternate_prob), and remove_vig([p]) would fabricate a 1.0 certainty
    (see test_odds_converter.py::test_a_lone_outcome_is_skipped_not_devigged...).
    This returns the raw number precisely so the caller can decide what to do
    with a one-sided price instead of silently guessing.
    """
    for book in bookmakers_data:
        if book.get("key") != book_key:
            continue
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            for o in market.get("outcomes", []):
                if o.get("name") != name:
                    continue
                if _norm_team(o.get("description", "")) != _norm_team(participant):
                    continue
                if o.get("point") is None or abs(float(o["point"]) - point) > 0.01:
                    continue
                return american_to_prob(o["price"])
    return None


def scaled_alternate_diagnostics(
    bookmakers_data: list[dict],
    market_key: str,
    point: float,
    participant: str,
) -> dict | None:
    """
    Estimate a probability for a Kalshi rung that only a one-sided alternate ladder
    quotes, by anchoring the ladder's raw price against a book that prices both sides
    -- and return every intermediate number, not just the final estimate, so a caller
    can log the full calibration chain for later validation (added 2026-08-24 for the
    shadow-mode logging this exists to feed -- see storage/db.py::dk_scaled_shadow_log
    and dashboard_server.py's /dk-scaled page).

    THE PROBLEM. Verified live 2026-08-24: every book carrying pitcher_strikeouts_
    alternate / batter_home_runs_alternate / batter_total_bases_alternate (DraftKings,
    FanDuel, BetOnline, William Hill, Bovada, Fanatics) sends the Over leg only, never
    a matching Under, at any point. consensus_stats() correctly refuses to price these
    (see the len(siblings) < 2 check above) rather than fabricate a de-vig off one
    number -- but that leaves every rung besides Pinnacle's single featured line
    unpriced, which is the 224-of-227 no_consensus problem this exists to shrink.

    THE APPROACH. Pinnacle prices its own featured rung on both sides, so its fair
    probability there is trustworthy. Where the alternate book (config.
    PROP_ALTERNATE_BOOKMAKERS) ALSO quotes that exact same point, the ratio between
    Pinnacle's fair number and the alternate book's raw number is a measured
    calibration factor for that specific player/game -- not an assumed flat vig
    percentage. Applying that same ratio to the alternate book's raw price at the
    TARGET rung gives an estimated fair probability there.

    This is a single calibration point, not a reconstructed distribution: it assumes
    the alternate book's vig-to-raw relationship is roughly stable across nearby
    rungs, which weakens the further the target point sits from the anchor (vig is
    not perfectly uniform across a ladder -- the same reason config.DEVIG_METHOD is
    Shin's, not proportional, for the two-sided markets). Untested in production, so
    every estimate this produces carries book_count=1 and std_dev=0.04.

    0.04, not kelly_calculator's actual max-discount threshold of 0.05: quality_
    check()'s shared high_uncertainty_std is ALSO 0.04, checked with a strict '>',
    and high_uncertainty_min_books=4 (this always reports book_count=1) -- 0.05 would
    have hard-REJECTED every scaled estimate before Kelly ever saw it, silently
    turning this whole function into dead code. 0.04 is deliberately the largest
    value that still clears that gate, which lands one Kelly discount step short of
    the true maximum (uncertainty_factor 0.6, not 0.5) -- a real, if imperfect,
    proxy for "size this down until it's validated."

    Returns a dict with every intermediate value (anchor_point, distance,
    anchor_fair_prob, anchor_raw_prob, target_raw_prob, ratio, scaled_prob), or None
    if no anchor or no target-point quote exists. See scaled_alternate_prob() below
    for the (estimated_prob, book_count, std_dev) shape core/value_detector.py
    actually prices against.
    """
    import config
    anchor_book = "pinnacle"
    alt_book = getattr(config, "PROP_ALTERNATE_BOOKMAKERS", "draftkings")
    alt_market_key = f"{market_key}_alternate"

    # 1. Pinnacle's own quoted point for this participant (may differ from `point`,
    #    the Kalshi threshold we're actually trying to price).
    anchor_point = None
    for book in bookmakers_data:
        if book.get("key") != anchor_book:
            continue
        for market in book.get("markets", []):
            if market.get("key") != market_key:
                continue
            for o in market.get("outcomes", []):
                if (o.get("point") is not None
                        and _norm_team(o.get("description", "")) == _norm_team(participant)):
                    anchor_point = float(o["point"])
                    break
            if anchor_point is not None:
                break
        if anchor_point is not None:
            break
    if anchor_point is None:
        return None

    if abs(point - anchor_point) <= 0.01:
        # This IS Pinnacle's own point -- consensus_stats() already prices it
        # directly and correctly; scaling here would be redundant, not helpful.
        return None

    # 2. Pinnacle's de-vigged fair probability at ITS point (reuses the existing,
    #    correct two-sided de-vig -- Pinnacle always quotes both sides).
    anchor_fair, _, _ = consensus_stats(
        bookmakers_data, "Over", market_key=market_key,
        point=anchor_point, participant=participant)
    if anchor_fair is None:
        return None

    # 3. The alternate book's raw price at that SAME point -- the calibration ratio.
    anchor_raw = _raw_prob_at_point(bookmakers_data, alt_book, alt_market_key,
                                    anchor_point, participant)
    if not anchor_raw:
        return None
    ratio = anchor_fair / anchor_raw

    # 4. The alternate book's raw price at the TARGET point -- what we actually want.
    target_raw = _raw_prob_at_point(bookmakers_data, alt_book, alt_market_key,
                                    point, participant)
    if target_raw is None:
        return None

    scaled = max(0.0, min(1.0, target_raw * ratio))
    return {
        "anchor_point": anchor_point,
        "distance": round(point - anchor_point, 3),
        "anchor_fair_prob": anchor_fair,
        "anchor_raw_prob": anchor_raw,
        "target_raw_prob": target_raw,
        "ratio": ratio,
        "scaled_prob": scaled,
    }


def scaled_alternate_prob(
    bookmakers_data: list[dict],
    market_key: str,
    point: float,
    participant: str,
) -> tuple[float | None, int, float]:
    """Thin wrapper around scaled_alternate_diagnostics() for callers that only need
    the (estimated_prob, book_count, std_dev) shape consensus_stats() returns, not
    the full calibration chain. See scaled_alternate_diagnostics()'s docstring for
    the method and why book_count=1 / std_dev=0.04."""
    diag = scaled_alternate_diagnostics(bookmakers_data, market_key, point, participant)
    if diag is None:
        return None, 0, 0.0
    return diag["scaled_prob"], 1, 0.04
