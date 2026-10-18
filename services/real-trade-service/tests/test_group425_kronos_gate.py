"""group 425: entry_engine/kronos_gate.py - pure verdicts, no-data = no opinion, mode parsing, Gate 5.6 target rule, cached fail-open fetch.
No pytest fixtures: runs under pytest and under a plain loop (config is patched and restored by _cfg)."""
import asyncio
import contextlib
import json
import sys
import types

import config
from entry_engine import kronos_gate as kg


@contextlib.contextmanager
def _cfg(**kw):
    old = {k: getattr(config, k) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(config, k, v)


def _fc(**kw):
    f = {"symbol": "ABC", "n_paths": 10, "horizon": 5, "p_up": 0.6, "q10_ret_pct": -3.0, "q50_ret_pct": 1.0, "q90_ret_pct": 5.0,
         "mean_ret_pct": 1.0, "p_target_first": 0.3, "p_stop_first": 0.2, "ev_pct": 1.2, "stop_pct": 3.0, "target_pct": 6.0}
    f.update(kw)
    return f


def test_mode_parsing_defaults_off_and_typos_are_off():
    assert config._kg_mode("KRONOS_NOT_SET_XYZ", "off") == "off"
    assert config._kg_mode("KRONOS_NOT_SET_XYZ", "enforce") == "enforce"
    assert config._kg_mode("KRONOS_NOT_SET_XYZ", "enfroce") == "off"
    with _cfg(KRONOS_GATE_MODE="enforce", KRONOS_GATE_DEMO_MODE="log"):
        assert kg.mode_for("REAL") == "enforce" and kg.mode_for("DEMO") == "log" and kg.mode_for("real") == "enforce"
    with _cfg(KRONOS_GATE_MODE="bogus", KRONOS_GATE_DEMO_MODE="off"):
        assert kg.mode_for("REAL") == "off" and kg.mode_for("DEMO") == "off"


def test_good_forecast_passes():
    with _cfg(KRONOS_MIN_EDGE_X_COST=1.0, KRONOS_MIN_P_TARGET=0.10):
        v = kg.evaluate(_fc(), 0.35)
    assert v["opinion"] == "ok" and v["would_reject"] is False and v["reasons"] == [] and v["tag"] is None


def test_ev_below_cost_rejects():
    with _cfg(KRONOS_MIN_EDGE_X_COST=1.0, KRONOS_MIN_P_TARGET=0.10):
        v = kg.evaluate(_fc(ev_pct=0.2), 0.35)
    assert v["would_reject"] and v["tag"] == "kronos" and v["reason"].startswith("KRONOS:") and "round-trip cost" in v["reason"]


def test_low_p_target_rejects_and_both_reasons_can_stack():
    with _cfg(KRONOS_MIN_EDGE_X_COST=1.0, KRONOS_MIN_P_TARGET=0.10):
        v = kg.evaluate(_fc(p_target_first=0.0), 0.35)
        assert v["would_reject"] and len(v["reasons"]) == 1 and "reach the target" in v["reasons"][0]
        v = kg.evaluate(_fc(p_target_first=0.0, ev_pct=-1.0), 0.35)
        assert len(v["reasons"]) == 2


def test_zero_thresholds_disable_each_check():
    with _cfg(KRONOS_MIN_EDGE_X_COST=0.0, KRONOS_MIN_P_TARGET=0.0):
        v = kg.evaluate(_fc(ev_pct=-5.0, p_target_first=0.0), 0.35)
    assert v["opinion"] == "ok" and v["would_reject"] is False


def test_missing_or_garbage_forecast_is_no_opinion_and_never_raises():
    for bad in (None, {}, "x", 5, {"ev_pct": "nan"}, {"ev_pct": 1.0}, _fc(n_paths=1), _fc(ev_pct=float("inf")), _fc(p_target_first=None)):
        v = kg.evaluate(bad, 0.35)
        assert v["opinion"] == "no_opinion" and v["would_reject"] is False and v["no_data"]
    assert kg.evaluate(_fc(), None)["opinion"] == "ok"          # an unknown cost counts as 0, never a crash


def test_effective_target_only_changes_in_enforce_with_an_opinion():
    ok = kg.evaluate(_fc(ev_pct=2.0), 0.35)
    assert kg.effective_target_pct(6.0, ok, "enforce") == 2.0          # forecast EV is smaller than the fixed target
    assert kg.effective_target_pct(1.5, ok, "enforce") == 1.5          # never raised above the fixed target
    assert kg.effective_target_pct(6.0, ok, "log") == 6.0
    assert kg.effective_target_pct(6.0, ok, "off") == 6.0
    assert kg.effective_target_pct(6.0, None, "enforce") == 6.0
    assert kg.effective_target_pct(6.0, kg.evaluate(None, 0.3), "enforce") == 6.0
    assert kg.effective_target_pct(6.0, kg.evaluate(_fc(ev_pct=-1.0), 0.3), "enforce") == 6.0   # non-positive EV: Gate 4f's job
    assert kg.effective_target_pct("junk", ok, "enforce") == "junk"


def test_cost_pct_is_positive_and_falls_back():
    assert 0.0 < kg.cost_pct_for(250.0) < 5.0
    assert kg.cost_pct_for(None) == 0.35 and kg.cost_pct_for(-3) == 0.35


def test_snapshot_is_json_and_bounded():
    v = kg.evaluate(_fc(), 0.35)
    doc = json.loads(kg.snapshot(_fc(), v, "log", False))
    assert doc["gate_mode"] == "log" and doc["enforced"] is False and doc["forecast"]["ev_pct"] == 1.2
    assert len(kg.snapshot(_fc(symbol="x" * 10000), v, "log", False)) <= 3000
    assert kg.snapshot(None, kg.evaluate(None, 0.3), "log", False).startswith("{")


def test_record_and_stats_count_without_raising():
    kg.reset()
    ok = kg.evaluate(_fc(ev_pct=0.1), 0.35)
    kg.record("abc", "DEMO", ok, "enforce", True, 100.0)
    kg.record("abc", "DEMO", kg.evaluate(None, 0.3), "log", False, 100.0)
    kg.record(None, None, {}, "log", False)           # garbage in: still no exception
    st = kg.stats()
    assert st["evaluated"] == 3 and st["ok"] == 1 and st["no_opinion"] == 2 and st["would_reject"] == 1 and st["enforced"] == 1
    assert st["recent"][-1]["symbol"] == "ABC" and st["config"]["KRONOS_GATE_MODE"] in ("off", "log", "enforce")


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


def _fake_httpx(handler):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            return handler(url, json, timeout)
    mod = types.ModuleType("httpx")
    mod.AsyncClient = _Client
    return mod


def _fetch(handler, *args):
    old = sys.modules.get("httpx")
    sys.modules["httpx"] = _fake_httpx(handler)
    try:
        return asyncio.run(kg.fetch_forecast(*args))
    finally:
        if old is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = old


def test_fetch_success_is_cached_per_symbol_and_sends_the_right_body():
    kg.reset()
    calls = []

    def h(url, body, timeout):
        calls.append((url, body, timeout))
        return _Resp(200, _fc())
    with _cfg(KRONOS_URL="http://k:1", KRONOS_TIMEOUT_S=4.0, KRONOS_CACHE_TTL_S=600.0, KRONOS_FAIL_TTL_S=60.0, KRONOS_HORIZON_BARS=5, KRONOS_PATHS=10):
        a = _fetch(h, "abc", 100.0, 3.0, 6.0)
        b = _fetch(h, "ABC", 100.5, 3.0, 6.0)
    assert a == b and a["ev_pct"] == 1.2 and len(calls) == 1
    assert calls[0][0] == "http://k:1/forecast" and calls[0][2] == 4.0
    assert calls[0][1] == {"symbol": "ABC", "ref_price": 100.0, "stop_pct": 3.0, "target_pct": 6.0, "horizon": 5, "n_paths": 10}
    assert kg.stats()["fetch_ok"] == 1 and kg.stats()["fetch_cache_hit"] == 1


def test_fetch_failures_are_none_and_cached_briefly():
    kg.reset()
    n = {"c": 0}

    def boom(url, body, timeout):
        n["c"] += 1
        raise RuntimeError("worker down")
    with _cfg(KRONOS_FAIL_TTL_S=60.0):
        assert _fetch(boom, "AAA", 100.0, 3.0, 6.0) is None
        assert _fetch(boom, "AAA", 100.0, 3.0, 6.0) is None          # served from the failure cache: no second call
    assert n["c"] == 1 and kg.stats()["fetch_fail"] == 1
    kg.reset()
    assert _fetch(lambda u, b, t: _Resp(422, {"detail": "only 30 completed bars"}), "BBB", 100.0, 3.0, 6.0) is None
    kg.reset()
    assert _fetch(lambda u, b, t: _Resp(200, {"nothing": 1}), "CCC", 100.0, 3.0, 6.0) is None
    kg.reset()
    assert _fetch(lambda u, b, t: _Resp(200, _fc()), "", 100.0, 3.0, 6.0) is None           # bad inputs never call the worker
    assert _fetch(lambda u, b, t: _Resp(200, _fc()), "X", 0.0, 3.0, 6.0) is None
    assert _fetch(lambda u, b, t: _Resp(200, _fc()), "X", 100.0, -1.0, 6.0) is None
