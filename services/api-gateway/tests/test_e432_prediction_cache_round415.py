"""E432 (round 415): `_fetch_prediction_cached` keeps a successful /predict answer for PREDICTION_CACHE_TTL_SEC (default 60) per symbol."""
from __future__ import annotations

import asyncio
import types

import httpx

from test_main_scoring import gw, Resp  # noqa: F401


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _wire(monkeypatch, *, ttl=None):
    env = types.SimpleNamespace(calls=[], resp=None, exc=None)

    async def fake(client, name, url, timeout=5.0, **k):
        env.calls.append(url.rsplit("/", 1)[-1])
        if env.exc is not None:
            raise env.exc
        return env.resp

    monkeypatch.setattr(gw, "_cb_get", fake)
    if ttl is None:
        monkeypatch.delenv("PREDICTION_CACHE_TTL_SEC", raising=False)
    else:
        monkeypatch.setenv("PREDICTION_CACHE_TTL_SEC", str(ttl))
    clock = types.SimpleNamespace(t=1000.0)
    monkeypatch.setattr(gw.time, "monotonic", lambda: clock.t)
    return env, clock


def test_second_call_inside_the_window_makes_no_upstream_call(monkeypatch):
    env, clock = _wire(monkeypatch)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 63.5, "note": "n"})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (63.5, "n")
    clock.t += 59.0
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 1.0, "note": "changed"})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (63.5, "n")
    assert env.calls == ["TCS"]


def test_after_the_window_it_asks_again(monkeypatch):
    env, clock = _wire(monkeypatch)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 63.5, "note": "n"})
    _run(gw._fetch_prediction_cached("TCS", None))
    clock.t += 60.0
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 70.0, "note": "m"})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (70.0, "m")
    assert env.calls == ["TCS", "TCS"]


def test_each_symbol_has_its_own_entry(monkeypatch):
    env, _ = _wire(monkeypatch)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 10.0, "note": "a"})
    _run(gw._fetch_prediction_cached("AAA", None))
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 20.0, "note": "b"})
    assert _run(gw._fetch_prediction_cached("BBB", None)) == (20.0, "b")
    assert _run(gw._fetch_prediction_cached("AAA", None)) == (10.0, "a")
    assert env.calls == ["AAA", "BBB"]


def test_model_not_loaded_is_not_kept_so_a_loaded_model_is_seen_at_once(monkeypatch):
    env, _ = _wire(monkeypatch)
    env.resp = Resp(200, {"model_loaded": False, "prediction_score": 1})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (None, None)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 55.0, "note": "up"})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (55.0, "up")
    assert env.calls == ["TCS", "TCS"]


def test_errors_non_200_and_open_circuit_are_never_kept(monkeypatch):
    env, _ = _wire(monkeypatch)
    for exc in (httpx.PoolTimeout("p"), gw.CircuitOpenError("x", 30)):
        env.exc = exc
        assert _run(gw._fetch_prediction_cached("TCS", None)) == (None, None)
    env.exc = None
    env.resp = Resp(500, {"model_loaded": True, "prediction_score": 9})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (None, None)
    assert "TCS" not in gw._prediction_cache
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 9.0, "note": "ok"})
    assert _run(gw._fetch_prediction_cached("TCS", None)) == (9.0, "ok")
    assert env.calls == ["TCS"] * 4


def test_ttl_zero_turns_the_cache_off(monkeypatch):
    env, _ = _wire(monkeypatch, ttl=0)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 5.0, "note": "x"})
    _run(gw._fetch_prediction_cached("TCS", None)); _run(gw._fetch_prediction_cached("TCS", None))
    assert env.calls == ["TCS", "TCS"] and gw._prediction_cache == {}


def test_ttl_parsing(monkeypatch):
    for raw, want in (("", 60.0), ("30", 30.0), ("junk", 60.0), ("-5", 0.0), ("0", 0.0), ("2.5", 2.5)):
        monkeypatch.setenv("PREDICTION_CACHE_TTL_SEC", raw)
        assert gw._prediction_cache_ttl() == want, raw


def test_cache_is_bounded(monkeypatch):
    env, clock = _wire(monkeypatch)
    monkeypatch.setattr(gw, "_PREDICTION_CACHE_MAX", 3)
    env.resp = Resp(200, {"model_loaded": True, "prediction_score": 1.0, "note": "n"})
    for s in ("A", "B", "C"):
        _run(gw._fetch_prediction_cached(s, None))
    clock.t += 61.0                                   # A, B, C expired: a new symbol clears them
    _run(gw._fetch_prediction_cached("D", None))
    assert list(gw._prediction_cache) == ["D"]
    for s in ("E", "F"):
        _run(gw._fetch_prediction_cached(s, None))
    _run(gw._fetch_prediction_cached("G", None))      # full of live entries: the oldest insert goes
    assert list(gw._prediction_cache) == ["E", "F", "G"]
