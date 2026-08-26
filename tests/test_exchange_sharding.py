"""Kalshi shard routing on every write path.

Kalshi split its matching engine into shards on 2026-08-24 12:00 ET; new
tennis/baseball events are created on shard 3, everything else stays on shard 0.
A write that does not say which shard it means is routed to shard 0, which has
never heard of a shard-3 ticker and rejects it with 404 market_not_found. That
cost 540 rejected orders over two days before it was found, and it was invisible
because READS auto-route -- prices, balances and /markets all looked healthy
while every order died.

Measured against the live API on 2026-08-26 (these numbers are why the two
routes are asserted differently):

    CREATE, no exchange_index          -> 404 market_not_found
    CREATE, exchange_index=-1          -> 201 (on shard 0 AND shard 3)
    CANCEL, no params                  -> 404 not_found
    CANCEL, exchange_index=-1 only     -> 400 market_ticker_is_required_...
    CANCEL, exchange_index=-1 + ticker -> 200
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "arbitrage_betting_bot"
sys.path.insert(0, str(ROOT))

import execution.kalshi_executor as ke  # noqa: E402
import data.kalshi_auth as ka  # noqa: E402


class _Resp:
    ok = True
    status_code = 200
    text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return {"order_id": "x", "fill_count": 0}


@pytest.fixture
def captured(monkeypatch):
    """Capture the kwargs of the next POST/DELETE without touching the network."""
    seen = {}
    monkeypatch.setattr(ke, "_throttle_order_post", lambda: None)
    monkeypatch.setattr(ka, "auth_headers", lambda *a, **k: {})

    class _S:
        def post(self, url, **kw):
            seen["post"] = {"url": url, **kw}
            return _Resp()

        def delete(self, url, **kw):
            seen["delete"] = {"url": url, **kw}
            return _Resp()

    monkeypatch.setattr(ka, "session", lambda: _S())
    return seen


# ── create ──────────────────────────────────────────────────────────────────

def test_create_carries_auto_routing(captured):
    ke._place_raw_order("KXMLBTB-X-Y-2", "bid", 0.5, 1, "good_till_canceled", "cid")
    body = captured["post"]["json"]
    assert body["exchange_index"] == -1, (
        "order POST omitted exchange_index -- it will be routed to shard 0 and "
        "404 for any market that is not on shard 0"
    )


def test_create_still_sends_the_ticker_in_the_body(captured):
    """-1 means 'route by ticker', so the ticker has to actually be there."""
    ke._place_raw_order("KXMLBTB-X-Y-2", "bid", 0.5, 1, "good_till_canceled", "cid")
    assert captured["post"]["json"]["ticker"] == "KXMLBTB-X-Y-2"


# ── cancel ──────────────────────────────────────────────────────────────────

def test_cancel_sends_both_index_and_ticker(captured):
    """exchange_index=-1 ALONE is a 400 on cancel -- the ticker is mandatory."""
    ke._cancel_order("oid-1", "KXMLBTB-X-Y-2")
    params = captured["delete"]["params"]
    assert params["exchange_index"] == -1
    assert params["market_ticker"] == "KXMLBTB-X-Y-2", (
        "cancel without market_ticker returns 400 when exchange_index=-1; "
        "without either it returns 404 and the order is left LIVE"
    )


def test_cancel_requires_a_ticker_argument():
    """Guard the signature itself: a caller must not be able to forget the ticker."""
    with pytest.raises(TypeError):
        ke._cancel_order("oid-only")


def test_cancel_quote_forwards_the_ticker(captured):
    ke.cancel_quote("oid-2", "KXMLBHR-A-B-1")
    assert captured["delete"]["params"]["market_ticker"] == "KXMLBHR-A-B-1"


class _Bad(_Resp):
    ok = False
    status_code = 404
    text = '{"error":{"code":"not_found"}}'


@pytest.fixture
def rejecting_cancel(monkeypatch):
    class _S:
        def delete(self, url, **kw):
            return _Bad()
    monkeypatch.setattr(ka, "auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(ka, "session", lambda: _S())


def test_rejected_cancel_on_an_already_terminal_order_is_not_alarming(
        rejecting_cancel, monkeypatch, caplog):
    """Observed in production: a 900s-old GTC 404s on cancel because Kalshi already
    terminated it. Nothing leaked, so this must not scream."""
    monkeypatch.setattr(ke, "_get_order_status",
                        lambda oid, retries=0: {"status": "canceled",
                                                "remaining_count_fp": "0.00"})
    with caplog.at_level("DEBUG"):
        assert ke._cancel_order("oid-3", "KXMLBTB-X-Y-2") is True
    assert "CRITICAL" not in caplog.text
    assert "no exposure" in caplog.text


def test_rejected_cancel_on_a_STILL_LIVE_order_screams(
        rejecting_cancel, monkeypatch, caplog):
    """The case that actually matters: the order is still resting and can fill."""
    monkeypatch.setattr(ke, "_get_order_status",
                        lambda oid, retries=0: {"status": "resting",
                                                "remaining_count_fp": "3.00"})
    with caplog.at_level("DEBUG"):
        assert ke._cancel_order("oid-4", "KXMLBTB-X-Y-2") is False
    assert "STILL LIVE" in caplog.text
    assert "UNTRACKED EXPOSURE" in caplog.text


def test_rejected_cancel_with_unreadable_status_is_treated_as_live(
        rejecting_cancel, monkeypatch, caplog):
    """If we cannot establish the truth, assume exposure rather than assume safety."""
    monkeypatch.setattr(ke, "_get_order_status", lambda oid, retries=0: None)
    with caplog.at_level("DEBUG"):
        assert ke._cancel_order("oid-5", "KXMLBTB-X-Y-2") is False
    assert "possibly LIVE" in caplog.text


# ── static guard: no call site may drop the ticker ───────────────────────────

@pytest.mark.parametrize("module", ["execution/kalshi_executor.py",
                                    "execution/market_maker.py"])
def test_no_cancel_call_site_omits_the_ticker(module):
    """Parse the source and assert every cancel call passes two arguments.

    A unit test cannot catch this: the MM paths are gated off by
    ENABLE_MARKET_MAKING and would never execute in the suite, yet a one-argument
    call there would leak a live quote the moment MM is switched back on.
    """
    tree = ast.parse((ROOT / module).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("_cancel_order", "cancel_quote"):
            continue
        if len(node.args) < 2:
            offenders.append(f"{module}:{node.lineno} {node.func.id} "
                             f"({len(node.args)} arg)")
    assert not offenders, (
        "cancel call site(s) missing the ticker -- these will 404 and silently "
        f"leave live orders resting: {offenders}"
    )
