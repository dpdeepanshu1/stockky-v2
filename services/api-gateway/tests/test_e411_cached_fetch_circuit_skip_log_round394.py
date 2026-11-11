"""E411: an open circuit on fundamental / event / prediction logs one summary warning per window (like news since round 200), not one warning per symbol."""
from __future__ import annotations

import asyncio

from test_main_analyze_symbol import gw, log  # noqa: F401


def _wire(monkeypatch):
    gw._circuit_skip_log.clear()
    monkeypatch.delenv("NEWS_CIRCUIT_LOG_EVERY_S", raising=False)
    seen = {"warning": [], "debug": []}
    monkeypatch.setattr(gw.logger, "warning", lambda *a, **k: seen["warning"].append(a))
    monkeypatch.setattr(gw.logger, "debug", lambda *a, **k: seen["debug"].append(a))

    async def boom(*a, **k):
        raise gw.CircuitOpenError("x", 30)

    monkeypatch.setattr(gw, "_cb_get", boom)

    class Store:
        def get_symbol(self, s):
            return None

    monkeypatch.setattr(gw, "_feed_store", lambda: Store())
    monkeypatch.setattr(gw, "_redis_get", lambda k: None)
    monkeypatch.setattr(gw, "try_refresh_lock", lambda *a, **k: True)
    monkeypatch.setattr(gw, "release_refresh_lock", lambda *a, **k: None)
    return seen


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_fundamental_circuit_open_logs_one_warning_and_returns_the_failure_default(monkeypatch):
    seen = _wire(monkeypatch)
    outs = [_run(gw._fetch_fundamental_cached(s, None)) for s in ("A", "B", "C")]
    assert outs == [({}, True)] * 3
    assert len(seen["warning"]) == 1 and len(seen["debug"]) >= 2


def test_events_circuit_open_logs_one_warning_and_returns_none(monkeypatch):
    seen = _wire(monkeypatch)
    assert [_run(gw._fetch_events_cached(s, None)) for s in ("A", "B", "C")] == [None] * 3
    assert len(seen["warning"]) == 1


def test_prediction_circuit_open_logs_one_warning_and_returns_none_pair(monkeypatch):
    seen = _wire(monkeypatch)
    assert [_run(gw._fetch_prediction_cached(s, None)) for s in ("A", "B", "C")] == [(None, None)] * 3
    assert len(seen["warning"]) == 1


def test_a_real_error_still_warns_every_time(monkeypatch):
    seen = _wire(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr(gw, "_cb_get", boom)
    _run(gw._fetch_prediction_cached("A", None)); _run(gw._fetch_prediction_cached("B", None))
    assert len(seen["warning"]) == 2
