"""tests/test_group221_prev_close_gate.py — scalp market gate: previous-close backstop (group 221).

The existing gate compares Nifty with TODAY'S OPEN, so a market that gapped down 1.2% and has been flat since
reads 0.0% and is let through. The backstop also blocks when the gateway's `nifty_vs_prev_close.change_pct`
is at or below MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT. It fails open: missing key, stale / fallback payload,
fetch failure, switch off.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from screening import trade_gates


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _gate_cfg(monkeypatch):
    for k, v in dict(MARKET_GATE_ENABLED=True, MARKET_GATE_MIN_NIFTY_CHANGE_PCT=-0.10,
                     MARKET_GATE_CACHE_TTL_S=120.0, MARKET_GATE_PREV_CLOSE_ENABLED=True,
                     MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT=-0.75).items():
        monkeypatch.setattr(config, k, v)
    trade_gates._market_cache.update({"pct": None, "ts": 0.0, "prev_pct": None})
    yield
    trade_gates._market_cache.update({"pct": None, "ts": 0.0, "prev_pct": None})


def _patch_fetch(monkeypatch, pct, prev=None, counter=None):
    """Stand-in for the gateway fetch: sets the cache's prev_pct the way the real one does."""
    async def f():
        if counter is not None:
            counter.append(1)
        trade_gates._market_cache["prev_pct"] = prev
        return pct
    monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", f)


# ── defaults ─────────────────────────────────────────────────────────────────
def test_config_defaults_are_pinned(monkeypatch):
    for name in ("MARKET_GATE_PREV_CLOSE_ENABLED", "MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT"):
        monkeypatch.delenv(name, raising=False)
    import importlib
    cfg = importlib.reload(config)
    try:
        assert cfg.MARKET_GATE_PREV_CLOSE_ENABLED is True
        assert cfg.MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT == -0.75
    finally:
        importlib.reload(config)


# ── _prev_close_pct (payload parsing) ────────────────────────────────────────
@pytest.mark.parametrize("body,expected", [
    ({"nifty_vs_prev_close": {"prev_close": 100.0, "change_pct": -1.2}}, -1.2),
    ({"nifty_vs_prev_close": {"change_pct": "0.4"}}, 0.4),
    ({"nifty_vs_prev_close": {"change_pct": 0}}, 0.0),
    ({}, None),                                                        # older gateway: key missing
    ({"nifty_vs_prev_close": None}, None),
    ({"nifty_vs_prev_close": {}}, None),
    ({"nifty_vs_prev_close": {"change_pct": None}}, None),
    ({"nifty_vs_prev_close": {"change_pct": "abc"}}, None),
    ({"nifty_vs_prev_close": {"change_pct": float("nan")}}, None),
    ({"nifty_vs_prev_close": {"change_pct": float("inf")}}, None),
    ({"nifty_vs_prev_close": "oops"}, None),
    ({"stale": True, "nifty_vs_prev_close": {"change_pct": -3.0}}, None),    # yesterday's copy must not gate today
    ({"fallback": True, "nifty_vs_prev_close": {"change_pct": -3.0}}, None),
    ([], None),
    (None, None),
    ("text", None),
])
def test_prev_close_pct_parsing(body, expected):
    assert trade_gates._prev_close_pct(body) == expected


# ── the gate ─────────────────────────────────────────────────────────────────
def test_gap_down_flat_since_open_is_now_blocked(monkeypatch):
    _patch_fetch(monkeypatch, pct=0.0, prev=-1.2)             # the hole this closes
    reason = _run(trade_gates.market_gate_reject())
    assert reason.startswith("MARKET_WEAK") and "vs prev close" in reason
    assert "-1.20%" in reason and "-0.75%" in reason


def test_boundary_is_inclusive_like_the_open_check(monkeypatch):
    _patch_fetch(monkeypatch, pct=0.2, prev=-0.75)
    assert _run(trade_gates.market_gate_reject()) is not None
    trade_gates._market_cache.update({"ts": 0.0})
    _patch_fetch(monkeypatch, pct=0.2, prev=-0.74)
    assert _run(trade_gates.market_gate_reject()) is None


def test_open_based_reason_still_comes_first(monkeypatch):
    _patch_fetch(monkeypatch, pct=-0.35, prev=-1.5)
    reason = _run(trade_gates.market_gate_reject())
    assert "vs day open" in reason and "prev close" not in reason


def test_healthy_market_is_allowed(monkeypatch):
    _patch_fetch(monkeypatch, pct=0.3, prev=0.5)
    assert _run(trade_gates.market_gate_reject()) is None


def test_no_prev_close_value_fails_open(monkeypatch):
    _patch_fetch(monkeypatch, pct=0.0, prev=None)
    assert _run(trade_gates.market_gate_reject()) is None


def test_prev_close_only_blocks_even_when_open_change_is_missing(monkeypatch):
    _patch_fetch(monkeypatch, pct=None, prev=-2.0)
    assert "vs prev close" in _run(trade_gates.market_gate_reject())


def test_switch_off_restores_the_old_gate(monkeypatch):
    monkeypatch.setattr(config, "MARKET_GATE_PREV_CLOSE_ENABLED", False)
    _patch_fetch(monkeypatch, pct=0.0, prev=-3.0)
    assert _run(trade_gates.market_gate_reject()) is None


def test_whole_gate_off_wins(monkeypatch):
    monkeypatch.setattr(config, "MARKET_GATE_ENABLED", False)
    _patch_fetch(monkeypatch, pct=-2.0, prev=-3.0)
    assert _run(trade_gates.market_gate_reject()) is None


def test_threshold_is_env_tunable(monkeypatch):
    monkeypatch.setattr(config, "MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT", -1.5)
    _patch_fetch(monkeypatch, pct=0.0, prev=-1.2)
    assert _run(trade_gates.market_gate_reject()) is None
    trade_gates._market_cache.update({"ts": 0.0})
    _patch_fetch(monkeypatch, pct=0.0, prev=-1.6)
    assert _run(trade_gates.market_gate_reject()) is not None


def test_cache_serves_both_values_without_refetching(monkeypatch):
    calls = []
    _patch_fetch(monkeypatch, pct=0.0, prev=-1.2, counter=calls)
    _run(trade_gates.market_gate_reject()); _run(trade_gates.market_gate_reject())
    assert len(calls) == 1 and trade_gates.last_nifty_prev_close_pct() == -1.2


def test_a_refetch_without_the_value_clears_the_old_one(monkeypatch):
    _patch_fetch(monkeypatch, pct=0.0, prev=-1.2)
    assert _run(trade_gates.market_gate_reject()) is not None
    trade_gates._market_cache["ts"] = 0.0                       # TTL expired
    async def failing():                                        # gateway down: returns None, sets nothing
        return None
    monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", failing)
    assert _run(trade_gates.market_gate_reject()) is None       # not stuck on the old -1.2
    assert trade_gates.last_nifty_prev_close_pct() is None


# ── the real fetch, with a fake gateway ──────────────────────────────────────
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def _fake_client(monkeypatch, resp=None, exc=None):
    class C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, timeout=None):
            if exc:
                raise exc
            return resp
    monkeypatch.setattr(trade_gates.httpx, "AsyncClient", lambda *a, **k: C())


def test_fetch_reads_both_values_from_the_gateway_body(monkeypatch):
    _fake_client(monkeypatch, _Resp(200, {"nifty": {"change_pct": 0.05},
                                          "nifty_vs_prev_close": {"prev_close": 100.0, "change_pct": -1.2}}))
    assert _run(trade_gates._fetch_nifty_change_pct()) == 0.05
    assert trade_gates.last_nifty_prev_close_pct() == -1.2


def test_fetch_of_an_older_gateway_body_leaves_prev_none(monkeypatch):
    _fake_client(monkeypatch, _Resp(200, {"nifty": {"change_pct": 0.05}}))
    assert _run(trade_gates._fetch_nifty_change_pct()) == 0.05
    assert trade_gates.last_nifty_prev_close_pct() is None


def test_fetch_of_a_stale_body_ignores_the_prev_close_block(monkeypatch):
    _fake_client(monkeypatch, _Resp(200, {"stale": True, "nifty": {"change_pct": 0.05},
                                          "nifty_vs_prev_close": {"change_pct": -3.0}}))
    _run(trade_gates._fetch_nifty_change_pct())
    assert trade_gates.last_nifty_prev_close_pct() is None


@pytest.mark.parametrize("resp,exc", [(_Resp(503, {}), None), (None, RuntimeError("boom"))])
def test_fetch_failures_return_none_and_do_not_raise(monkeypatch, resp, exc):
    _fake_client(monkeypatch, resp=resp, exc=exc)
    assert _run(trade_gates._fetch_nifty_change_pct()) is None
    assert trade_gates.last_nifty_prev_close_pct() is None


def test_end_to_end_through_the_real_fetch(monkeypatch):
    _fake_client(monkeypatch, _Resp(200, {"nifty": {"change_pct": 0.0},
                                          "nifty_vs_prev_close": {"change_pct": -1.2}}))
    assert "vs prev close" in _run(trade_gates.market_gate_reject())
