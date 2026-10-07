"""tests/test_group222_boot_warm_preopen.py — the boot quote sweep is skipped for early pre-open boots (group 222).

2026-10-07 boot log, 08:37 IST ("preopen"): the surprise warm still swept ~1,000 symbols through GET /quote because only
"closed"/"holiday" skipped it. A sweep finishing more than ~5 min before 09:15 cannot serve the first cycle (the cached
fast path honours 220 s), so an early pre-open boot now restores the saved result and skips; a boot inside the last
SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC (default 300) still warms.
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

import surprise_scanner


@pytest.fixture
def env(monkeypatch):
    e = types.SimpleNamespace(scans=[], phase="preopen", secs=1500.0, restored={"count": 3})

    class Engine:
        _last_result = None

        async def scan(self, client=None, market_data_url=None, cached=False):
            e.scans.append(cached)

        def _load_last_result_from_durable_cache(self):
            self._last_result = e.restored

    monkeypatch.setattr(surprise_scanner, "surprise_engine", Engine())
    monkeypatch.setattr(gw, "_get_http_client", lambda: object())
    monkeypatch.setattr(gw, "_surprise_boot_warm_delay_sec", lambda: 0)
    monkeypatch.setattr(gw, "_market_session_phase_ist", lambda: e.phase)
    monkeypatch.setattr(gw, "_seconds_to_market_open_ist", lambda: e.secs)
    monkeypatch.delenv("SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC", raising=False)
    return e


def _warm():
    async def go():
        await gw._warm_surprise_scan_cache()
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    asyncio.run(go())


class TestSkipReason:
    @pytest.mark.parametrize("phase,secs,expected", [
        ("closed", 99999, "closed"), ("holiday", 0, "closed"),
        ("preopen", 1500, "not open yet"),            # 08:50 - 25 min to go
        ("preopen", 301, "not open yet"),             # just outside the lead
        ("preopen", 300, None),                       # exactly the lead: still warms (not strictly earlier)
        ("preopen", 60, None),                        # 09:14: warm so the first cycle is served
        ("open", 0, None), ("post", 0, None),
    ])
    def test_matrix(self, env, phase, secs, expected):
        env.phase, env.secs = phase, secs
        assert gw._surprise_boot_warm_skip_reason() == expected

    def test_lead_zero_restores_the_old_behaviour(self, env, monkeypatch):
        monkeypatch.setenv("SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC", "0")
        assert gw._surprise_boot_warm_skip_reason() is None
        env.phase = "closed"
        assert gw._surprise_boot_warm_skip_reason() == "closed"

    @pytest.mark.parametrize("raw,lead", [("", 300.0), ("abc", 300.0), ("-5", 0.0), ("120", 120.0)])
    def test_lead_parsing(self, monkeypatch, raw, lead):
        monkeypatch.setenv("SURPRISE_BOOT_WARM_PREOPEN_LEAD_SEC", raw)
        assert gw._surprise_boot_warm_preopen_lead_sec() == lead

    def test_a_clock_error_never_raises_and_does_not_skip(self, env, monkeypatch):
        def boom():
            raise RuntimeError("clock")
        monkeypatch.setattr(gw, "_seconds_to_market_open_ist", boom)
        assert gw._surprise_boot_warm_skip_reason() is None

    def test_seconds_to_open_is_a_number(self):
        assert isinstance(gw._seconds_to_market_open_ist(), float)


class TestWarmHook:
    def test_early_preopen_boot_restores_and_skips(self, env, caplog):
        with caplog.at_level("INFO"):
            _warm()
        assert env.scans == []
        assert "market not open yet" in caplog.text and "skipped the boot quote sweep" in caplog.text

    def test_early_preopen_boot_with_nothing_saved_still_warms(self, env):
        env.restored = None
        _warm()
        assert env.scans == [True]

    def test_late_preopen_boot_warms_as_before(self, env):
        env.secs = 120.0
        _warm()
        assert env.scans == [True]

    def test_open_market_boot_warms_as_before(self, env):
        env.phase = "open"
        _warm()
        assert env.scans == [True]

    def test_closed_message_is_unchanged_in_substance(self, env, caplog):
        env.phase = "closed"
        with caplog.at_level("INFO"):
            _warm()
        assert "market closed" in caplog.text and "restored the last surprise/scan result" in caplog.text
