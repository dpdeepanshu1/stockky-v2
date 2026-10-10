"""group302: /surprise/scan serves the last saved result while the market is closed, and /data-feed/status tells one
consistent story (no boot-heal "Last success", no phantom elapsed/ETA, one count)."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def _run(coro):
    return asyncio.run(coro)


class _Engine:
    def __init__(self, last=None, ts=0.0, durable=None):
        self._last_result = last
        self._last_scan_ts = ts
        self._durable = durable
        self.loads = 0
        self.scans = 0

    def _load_last_result_stale_from_durable_cache(self):
        self.loads += 1
        if self._durable is not None:
            self._last_result, self._last_scan_ts = self._durable

    async def scan(self, **kw):
        self.scans += 1
        return {"stocks": [], "live": True}


RESULT = {"count": 3, "stocks": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}], "quotes_ok": 900}


# ── closed-market serve ──────────────────────────────────────────────────────

def test_closed_serves_saved_result_flagged(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    eng = _Engine(RESULT, time.time() - 3600)
    out = _run(gw._surprise_closed_market_result(eng, None))
    assert out["market_closed"] is True and out["from_cache"] is True and out["market_phase"] == "closed"
    assert 3500 < out["cache_age_sec"] < 3700 and out["count"] == 3
    assert "Refresh Scan" in out["message"]
    assert RESULT.get("market_closed") is None  # the saved result itself is not mutated


def test_holiday_serves_too(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "holiday")
    out = _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), None))
    assert out["market_phase"] == "holiday"


@pytest.mark.parametrize("phase", ["open", "preopen", "post"])
def test_not_closed_returns_none(monkeypatch, phase):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: phase)
    assert _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), None)) is None


def test_limit_truncates(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    out = _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), 2))
    assert [s["symbol"] for s in out["stocks"]] == ["A", "B"]
    out0 = _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), 0))
    assert out0["stocks"] == []


def test_loads_durable_copy_when_memory_is_empty(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    eng = _Engine(None, 0.0, durable=(RESULT, time.time() - 60))
    out = _run(gw._surprise_closed_market_result(eng, None))
    assert eng.loads == 1 and out["count"] == 3


def test_nothing_saved_falls_through_to_a_live_scan(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    assert _run(gw._surprise_closed_market_result(_Engine(None, 0.0), None)) is None


def test_env_switch_off(monkeypatch):
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    monkeypatch.setenv("SURPRISE_CLOSED_SERVE_LAST", "0")
    assert _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), None)) is None


def test_phase_error_never_breaks_the_scan(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(gw, "_market_session_phase_ist", boom)
    assert _run(gw._surprise_closed_market_result(_Engine(RESULT, time.time()), None)) is None


def _route(monkeypatch, eng, **kw):
    monkeypatch.setitem(__import__("sys").modules, "surprise_scanner", SimpleNamespace(surprise_engine=eng))
    monkeypatch.setattr(gw, "_get_http_client", lambda: object())
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: "closed")
    return _run(gw.api_surprise_scan(**{"force_reload": False, "symbols": None, "cached": False, "limit": None,
                                        "refresh": False, **kw}))


def test_route_serves_saved_result_without_scanning(monkeypatch):
    eng = _Engine(RESULT, time.time() - 100)
    out = _route(monkeypatch, eng)
    assert out["market_closed"] is True and eng.scans == 0


@pytest.mark.parametrize("kw", [{"refresh": True}, {"force_reload": True}, {"symbols": "TCS"}])
def test_route_runs_a_real_scan_when_asked(monkeypatch, kw):
    eng = _Engine(RESULT, time.time() - 100)
    out = _route(monkeypatch, eng, **kw)
    assert out.get("live") is True and eng.scans == 1


# ── /data-feed/status normalisation ──────────────────────────────────────────

class _Store:
    def __init__(self):
        self.meta_calls = []

    def set_meta(self, **kw):
        self.meta_calls.append(kw)


def _norm(job, meta, count=1417, last_ok=None):
    store = _Store()
    out = gw._normalize_data_feed_status(store, job, meta, count, last_ok if last_ok is not None else meta.get("last_success_at"))
    return out, store


def test_idle_job_has_no_elapsed_or_eta():
    out, _ = _norm({"status": "idle", "elapsed_sec": 675174, "estimated_remaining_sec": 99}, {})
    assert out["elapsed_sec"] == 0 and out["estimated_remaining_sec"] == 0


def test_running_job_keeps_its_progress():
    out, _ = _norm({"status": "running", "elapsed_sec": 120, "estimated_remaining_sec": 300}, {})
    assert out["elapsed_sec"] == 120 and out["estimated_remaining_sec"] == 300


def test_boot_heal_last_success_is_repaired_from_the_finish_time():
    meta = {"last_success_at": "2026-10-10T16:24:00+05:30", "last_message": "Boot heal: cleared stuck job"}
    job = {"status": "idle", "finished_at": "2026-10-10T15:01:00+05:30"}
    out, store = _norm(job, meta)
    assert out["last_success_at"] == "2026-10-10T15:01:00+05:30" == out["last_success"]
    assert out["meta"]["last_success_at"] == "2026-10-10T15:01:00+05:30"
    assert store.meta_calls == [{"last_success_at": "2026-10-10T15:01:00+05:30"}]


def test_real_last_success_is_left_alone():
    meta = {"last_success_at": "2026-10-10T15:01:00+05:30", "last_message": "Feed complete"}
    out, store = _norm({"status": "done", "finished_at": "2026-10-10T15:01:00+05:30"}, meta)
    assert out["last_success_at"] == "2026-10-10T15:01:00+05:30" and store.meta_calls == []


def test_counts_follow_the_feed():
    out, _ = _norm({"status": "done"}, {"stock_count": 0, "last_count": 52}, count=1417)
    assert out["stocks_in_feed"] == out["last_count"] == 1417
    assert out["meta"]["stock_count"] == out["meta"]["last_count"] == 1417


def test_partial_clears_when_the_run_finished():
    out, _ = _norm({"status": "done", "processed": 1417, "total": 1417}, {"partial": True})
    assert out["meta"]["partial"] is False


def test_partial_kept_when_run_stopped_short():
    out, _ = _norm({"status": "stopped", "processed": 400, "total": 1417}, {"partial": True})
    assert out["meta"]["partial"] is True


def test_switch_off_returns_raw_values(monkeypatch):
    monkeypatch.setenv("DATA_FEED_STATUS_NORMALIZE", "0")
    out, _ = _norm({"status": "idle", "elapsed_sec": 5}, {"stock_count": 0}, count=1417)
    assert out["elapsed_sec"] == 5 and out["meta"]["stock_count"] == 0 and out["stocks_in_feed"] == 1417
