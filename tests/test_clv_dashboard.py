"""storage/db.py::get_positions_for_clv_analytics and the /clv dashboard route.

Added 2026-08-25 alongside core/clv_analytics.py -- these pin the DB fetch (only
closed positions, right shape for compute_row) and that the Flask route renders
without error on both an empty DB and a populated one.
"""
from __future__ import annotations

import pytest

import storage.db as db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return db


def _add(fresh_db, status_via_settle=None, **overrides):
    kwargs = dict(
        sport="baseball_mlb", home_team="LAD", away_team="SF", team_name="Dodgers",
        platform="Kalshi", stake=10.0, market_price=0.45, is_paper=False,
        order_id="o1", execution_status="submitted", market_ticker="KXTEST-1",
        side="yes", bet_type="h2h", commence_time="2026-08-20T20:00:00+00:00",
        consensus_prob=0.47,
    )
    kwargs.update(overrides)
    pos_id = fresh_db.add_position(**kwargs)
    if status_via_settle:
        fresh_db.settle_position(pos_id, status_via_settle)
    return pos_id


def test_only_closed_positions_are_returned(fresh_db):
    _add(fresh_db, status_via_settle=None)              # stays open
    _add(fresh_db, status_via_settle="won", market_ticker="KXTEST-2")
    rows = fresh_db.get_positions_for_clv_analytics()
    assert len(rows) == 1


def test_returned_rows_carry_every_field_compute_row_needs(fresh_db):
    _add(fresh_db, status_via_settle="won")
    fresh_db.set_closing_lines(1, kalshi_close_price=0.55, consensus_close_prob=0.50)
    rows = fresh_db.get_positions_for_clv_analytics()
    row = rows[0]
    for field in ("sport", "bet_type", "team_name", "entered_at", "commence_time",
                  "market_price", "kalshi_close_price", "consensus_prob",
                  "consensus_close_prob", "pnl", "stake"):
        assert field in row


def test_paper_and_live_positions_are_kept_separate(fresh_db):
    _add(fresh_db, status_via_settle="won", is_paper=True, market_ticker="KXTEST-P")
    _add(fresh_db, status_via_settle="won", is_paper=False, market_ticker="KXTEST-L")
    assert len(fresh_db.get_positions_for_clv_analytics(is_paper=False)) == 1
    assert len(fresh_db.get_positions_for_clv_analytics(is_paper=True)) == 1


def test_rows_are_oldest_first(fresh_db):
    import time
    _add(fresh_db, status_via_settle="won", market_ticker="KXTEST-1")
    time.sleep(0.01)
    _add(fresh_db, status_via_settle="won", market_ticker="KXTEST-2")
    rows = fresh_db.get_positions_for_clv_analytics()
    assert rows[0]["entered_at"] <= rows[1]["entered_at"]


def test_no_closed_positions_returns_empty_list(fresh_db):
    assert fresh_db.get_positions_for_clv_analytics() == []


# ── CLV & TTE analytics, now on the homepage (2026-08-25) ─────────────────────────
#
# The standalone /clv page was folded into "/" so everything lives on the first
# page (replacing the old P&L cards/charts/tables there) -- /clv itself is kept
# only as a redirect for old links/bookmarks.

def test_clv_redirects_to_the_homepage(fresh_db):
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/clv")
        assert r.status_code == 302
        assert r.headers["Location"] == "/"


def test_homepage_renders_clv_content_on_an_empty_db(fresh_db):
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert b"CLV" in r.data
        assert b"Calibration" in r.data


def test_homepage_renders_clv_content_with_real_data(fresh_db):
    _add(fresh_db, status_via_settle="won")
    fresh_db.set_closing_lines(1, kalshi_close_price=0.55, consensus_close_prob=0.50)
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        assert b"Dodgers" in r.data


# ── Timeframe selector (2026-08-30) ────────────────────────────────────────────────

def _backdate(fresh_db, pos_id: int, entered_at: str) -> None:
    with fresh_db.get_connection() as conn:
        conn.execute("UPDATE positions SET entered_at = ? WHERE id = ?", (entered_at, pos_id))


def test_default_timeframe_is_all_and_unfiltered(fresh_db):
    pos_id = _add(fresh_db, status_via_settle="won")
    _backdate(fresh_db, pos_id, "2020-01-01T00:00:00")
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/")
        assert b"Dodgers" in r.data


def test_today_timeframe_excludes_a_bet_entered_long_ago(fresh_db):
    pos_id = _add(fresh_db, status_via_settle="won")
    _backdate(fresh_db, pos_id, "2020-01-01T00:00:00")
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/?timeframe=today")
        assert r.status_code == 200
        assert b"Dodgers" not in r.data


def test_today_timeframe_keeps_a_bet_entered_just_now(fresh_db):
    _add(fresh_db, status_via_settle="won")  # entered_at defaults to now
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/?timeframe=today")
        assert b"Dodgers" in r.data


def test_unrecognized_timeframe_falls_back_to_all_time(fresh_db):
    pos_id = _add(fresh_db, status_via_settle="won")
    _backdate(fresh_db, pos_id, "2020-01-01T00:00:00")
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/?timeframe=nonsense")
        assert r.status_code == 200
        assert b"Dodgers" in r.data  # not silently filtered out by a bad param


def test_selected_timeframe_button_is_marked_active(fresh_db):
    import re

    _add(fresh_db, status_via_settle="won")
    import dashboard_server as ds
    ds.app.config["TESTING"] = True
    with ds.app.test_client() as c:
        r = c.get("/?timeframe=week")
        html = r.get_data(as_text=True)
        # The "7D" button must carry the active class; the others must not.
        assert re.search(r'class="timeframe-btn active"\s+href="/\?timeframe=week">7D</a>', html)
        assert re.search(r'class="timeframe-btn"\s+href="/\?timeframe=today">Today</a>', html)
