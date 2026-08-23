"""Order POSTs must not go out as a burst.

main.py places approved orders from a ThreadPoolExecutor sized
max_workers=len(approved_live) -- one thread per order, all POSTing at once. That was
harmless while a scan approved a handful. Props changed the shape: one MLB game
carries ~50 prop markets evaluated in the same scan, so approvals concentrate into a
single burst. On 2026-08-22 a scan approved 33 orders, 12 POSTs landed inside 600ms,
and Kalshi rejected all 12 with HTTP 429.

There is deliberately NO retry on an order POST (see data/kalshi_auth.py::session) --
a blind retry could double-place a real trade. So a rejected order is simply a lost
opportunity, which makes preventing the burst the only lever.
"""
from __future__ import annotations

import threading
import time

import pytest

import config
from execution import kalshi_executor as ke


@pytest.fixture(autouse=True)
def _reset_gate():
    ke._last_order_post_at = 0.0
    yield
    ke._last_order_post_at = 0.0


def test_consecutive_posts_are_spaced(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_ORDER_MIN_SPACING_SECONDS", 0.05)
    stamps = []
    for _ in range(5):
        ke._throttle_order_post()
        stamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.045 for g in gaps), f"posts were not spaced: {gaps}"


def test_concurrent_threads_do_not_all_fire_at_once(monkeypatch):
    """THE INCIDENT. Twelve threads calling at the same instant must come out spaced,
    not together -- holding the lock across the sleep is what guarantees it."""
    monkeypatch.setattr(config, "KALSHI_ORDER_MIN_SPACING_SECONDS", 0.02)
    stamps = []
    lock = threading.Lock()
    start = threading.Barrier(12)

    def worker():
        start.wait()
        ke._throttle_order_post()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads: t.start()
    for t in threads: t.join()

    stamps.sort()
    span = stamps[-1] - stamps[0]
    assert len(stamps) == 12
    # 12 posts at 20ms spacing cannot legitimately complete in under ~220ms.
    assert span >= 0.20, f"12 posts went out in {span*1000:.0f}ms -- still a burst"


def test_the_gate_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "KALSHI_ORDER_MIN_SPACING_SECONDS", 0.0)
    t0 = time.monotonic()
    for _ in range(50):
        ke._throttle_order_post()
    assert time.monotonic() - t0 < 0.05


def test_every_order_post_goes_through_the_gate(monkeypatch):
    """The gate lives in _place_raw_order, so entries, exits and MM quotes all inherit
    it. If someone adds a POST path that bypasses it, this fails."""
    called = []
    monkeypatch.setattr(ke, "_throttle_order_post", lambda: called.append(1))

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"order_id": "x", "fill_count": 0}

    monkeypatch.setattr(ke, "auth_headers", lambda *a, **k: {}, raising=False)
    import data.kalshi_auth as ka
    monkeypatch.setattr(ka, "auth_headers", lambda *a, **k: {})
    monkeypatch.setattr(ka, "session", lambda: type("S", (), {
        "post": lambda self, *a, **k: _Resp()})())

    ke._place_raw_order("KX-T", "yes", 0.5, 1, "good_till_canceled", "cid")
    assert called == [1], "an order POST bypassed the rate gate"
