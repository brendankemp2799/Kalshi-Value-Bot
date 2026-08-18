"""Ambiguous team matches must be RECORDED, not just refused.

Refusing an ambiguous match (see tests/test_team_disambiguation.py) prevents the bad
outcome -- position #930 bought the opposite team -- but it does not recover the good
one. Every refusal is a market we could have priced and didn't, so each is logged for
a later matcher fix rather than silently dropped.

The thing most likely to go wrong here is VOLUME: the same fixture is rescanned every
cycle, so a naive insert turns one ambiguous game into ~100 rows a day and the backlog
becomes unreadable. These tests pin the dedup behaviour.
"""
from __future__ import annotations

import sqlite3

import pytest

import storage.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A real SQLite file, so the UPSERT and its UNIQUE constraint are exercised for
    real rather than mocked -- the dedup IS the behaviour under test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


CASE = dict(
    context="spread_covering_team",
    kalshi_ticker="KXMLBSPREAD-26AUG182005CWSCHC-CWS2",
    kalshi_name="Chicago WS",
    home_team="Chicago Cubs",
    away_team="Chicago White Sox",
    sport="baseball_mlb",
    bet_type="spread",
    home_score=25,
    away_score=25,
)


def test_first_sighting_is_recorded(fresh_db):
    db.log_ambiguous_match(**CASE)
    rows = db.get_ambiguous_matches()
    assert len(rows) == 1
    r = rows[0]
    assert r["kalshi_name"] == "Chicago WS"
    assert r["home_team"] == "Chicago Cubs"
    assert r["away_team"] == "Chicago White Sox"
    assert r["occurrences"] == 1
    assert r["resolved"] == 0
    assert r["home_score"] == 25 and r["away_score"] == 25


def test_rescans_increment_rather_than_duplicate(fresh_db):
    """THE volume guard. 100 scans of one fixture must be 1 row, not 100."""
    for _ in range(100):
        db.log_ambiguous_match(**CASE)
    rows = db.get_ambiguous_matches()
    assert len(rows) == 1, "rescans created duplicate rows"
    assert rows[0]["occurrences"] == 100


def test_first_seen_is_preserved_while_last_seen_advances(fresh_db):
    db.log_ambiguous_match(**CASE)
    first = db.get_ambiguous_matches()[0]
    db.log_ambiguous_match(**CASE)
    second = db.get_ambiguous_matches()[0]
    assert second["first_seen"] == first["first_seen"], "first_seen must not move"
    assert second["last_seen"] >= first["last_seen"]


def test_distinct_cases_are_kept_apart(fresh_db):
    db.log_ambiguous_match(**CASE)
    db.log_ambiguous_match(**{**CASE, "kalshi_name": "Chicago Cubs"})
    db.log_ambiguous_match(**{**CASE, "kalshi_ticker": "KXMLBSPREAD-OTHER-CWS2"})
    db.log_ambiguous_match(**{**CASE, "context": "h2h_yes_team"})
    assert len(db.get_ambiguous_matches()) == 4


def test_resolved_cases_drop_out_of_the_open_list(fresh_db):
    db.log_ambiguous_match(**CASE)
    rid = db.get_ambiguous_matches()[0]["id"]
    db.resolve_ambiguous_match(rid)
    assert db.get_ambiguous_matches() == []
    assert len(db.get_ambiguous_matches(include_resolved=True)) == 1


def test_logging_never_raises_on_a_broken_database(fresh_db, monkeypatch):
    """Diagnostics sit on the live scan path. A logging failure must not kill a scan."""
    def boom():
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "get_connection", boom)
    db.log_ambiguous_match(**CASE)   # must not raise


# ── the report the daily check reads ────────────────────────────────────────────

def test_report_is_empty_and_well_formed_when_nothing_is_outstanding(fresh_db):
    import research.metrics as m
    r = m.ambiguous_match_report()
    assert r["open_count"] == 0
    assert r["cases"] == []


def test_report_surfaces_the_case_with_its_scores(fresh_db):
    import research.metrics as m
    for _ in range(3):
        db.log_ambiguous_match(**CASE)
    r = m.ambiguous_match_report()
    assert r["open_count"] == 1
    assert r["total_occurrences"] == 3, "must count sightings, not rows"
    c = r["cases"][0]
    assert c["kalshi_name"] == "Chicago WS"
    assert c["matchup"] == "Chicago White Sox @ Chicago Cubs"
    assert c["scores"] == [25, 25], "the scores are what make it fixable"


def test_report_ranks_the_most_frequent_case_first(fresh_db):
    import research.metrics as m
    db.log_ambiguous_match(**CASE)
    for _ in range(5):
        db.log_ambiguous_match(**{**CASE, "kalshi_name": "NY Sox"})
    assert m.ambiguous_match_report()["cases"][0]["kalshi_name"] == "NY Sox"


def test_status_file_is_written_and_names_the_case(fresh_db, tmp_path, monkeypatch):
    import research.metrics as m
    out = tmp_path / "AMBIGUOUS_MATCHES.md"
    monkeypatch.setattr(m, "AMBIGUOUS_STATUS_PATH", out)
    db.log_ambiguous_match(**CASE)
    m._write_ambiguous_status(m.ambiguous_match_report())
    text = out.read_text()
    assert "Chicago WS" in text
    assert "Chicago White Sox @ Chicago Cubs" in text
    assert "1 unresolved" in text


def test_status_file_says_so_when_the_backlog_is_empty(fresh_db, tmp_path, monkeypatch):
    """An empty file must not read as 'the matcher is verified correct'."""
    import research.metrics as m
    out = tmp_path / "AMBIGUOUS_MATCHES.md"
    monkeypatch.setattr(m, "AMBIGUOUS_STATUS_PATH", out)
    m._write_ambiguous_status(m.ambiguous_match_report())
    text = out.read_text()
    assert "None outstanding" in text
    assert "not proof" in text, "empty must not be reported as proof of correctness"
