"""
group203 (item 5, api-gateway cold start): right after a restart three /scan/universe?cached=true calls each waited the
full 12 s movers deadline (the startup warm-up pass was still running) and then returned without movers anyway.
Until a movers pass has produced a list in this process, the wait is now SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S (5 s);
afterwards it is the normal SCAN_UNIVERSE_MOVERS_DEADLINE_S.

Run from services/api-gateway:
    python3 -m pytest tests/test_group203_cold_movers_deadline.py -q
"""
from __future__ import annotations
import asyncio, os, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import main as gw


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", raising=False)
    monkeypatch.delenv("MOMENTUM_MOVERS_SINGLE_FLIGHT", raising=False)
    monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_MOVERS_EVER_READY", False)
    monkeypatch.setattr(gw, "SCAN_UNIVERSE_MOVERS_DEADLINE_S", 12.0)
    gw._mm_flight = None
    yield
    gw._mm_flight = None


class TestDeadlineChoice:
    def test_cold_uses_the_shorter_deadline(self):
        assert gw._movers_deadline_now() == 5.0

    def test_warm_uses_the_full_deadline(self, monkeypatch):
        monkeypatch.setattr(gw, "_MOVERS_EVER_READY", True)
        assert gw._movers_deadline_now() == 12.0

    def test_cold_never_longer_than_the_full_deadline(self, monkeypatch):
        monkeypatch.setattr(gw, "SCAN_UNIVERSE_MOVERS_DEADLINE_S", 2.0)
        assert gw._movers_deadline_now() == 2.0

    def test_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", "0")
        assert gw._movers_deadline_now() == 12.0

    @pytest.mark.parametrize("raw,expected", [("", 5.0), ("x", 5.0), ("-3", 5.0), ("nan", 5.0), ("8", 8.0)])
    def test_env_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", raw)
        assert gw._scan_universe_cold_movers_deadline() == expected


class TestReadyFlag:
    def test_note_ready_only_for_non_empty_lists(self):
        for bad in (None, [], "x", {"a": 1}):
            gw._note_movers_ready(bad)
            assert gw._MOVERS_EVER_READY is False
        gw._note_movers_ready(["AAA"])
        assert gw._MOVERS_EVER_READY is True

    def test_computed_pass_sets_flag(self, monkeypatch):
        monkeypatch.setattr(gw, "_compute_momentum_movers", lambda: ["AAA"])
        assert gw._get_momentum_movers() == ["AAA"]
        assert gw._MOVERS_EVER_READY is True

    def test_empty_pass_does_not_set_flag(self, monkeypatch):
        monkeypatch.setattr(gw, "_compute_momentum_movers", lambda: [])
        gw._get_momentum_movers()
        assert gw._MOVERS_EVER_READY is False

    def test_cache_hit_sets_flag(self, monkeypatch):
        monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: ["CACHED"])
        assert gw._get_momentum_movers() == ["CACHED"]
        assert gw._MOVERS_EVER_READY is True

    def test_non_single_flight_path_sets_flag(self, monkeypatch):
        monkeypatch.setenv("MOMENTUM_MOVERS_SINGLE_FLIGHT", "0")
        monkeypatch.setattr(gw, "_compute_momentum_movers", lambda: ["AAA"])
        gw._get_momentum_movers()
        assert gw._MOVERS_EVER_READY is True


class TestColdCallerIsNotHeld:
    def _slow(self, monkeypatch, release, result=("LATE",)):
        def slow():
            release.wait(5)
            return list(result)
        monkeypatch.setattr(gw, "_get_momentum_movers", slow)

    def test_cold_caller_returns_after_the_short_deadline(self, monkeypatch):
        monkeypatch.setenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", "0.1")
        release = threading.Event()
        self._slow(monkeypatch, release)

        async def go():
            t0 = time.monotonic()
            out = await gw._movers_with_deadline()
            took = time.monotonic() - t0
            release.set()
            await asyncio.sleep(0.2)
            return out, took

        out, took = asyncio.run(go())
        assert out == ([], True) and took < 2.0     # full deadline is 12 s

    def test_warm_caller_still_waits_the_full_deadline(self, monkeypatch):
        monkeypatch.setattr(gw, "_MOVERS_EVER_READY", True)
        monkeypatch.setattr(gw, "SCAN_UNIVERSE_MOVERS_DEADLINE_S", 1.0)
        monkeypatch.setenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", "0.05")

        def medium():
            time.sleep(0.3)
            return ["ARRIVED"]

        monkeypatch.setattr(gw, "_get_momentum_movers", medium)
        assert asyncio.run(gw._movers_with_deadline()) == (["ARRIVED"], False)

    def test_cold_caller_gets_movers_that_are_ready_in_time(self, monkeypatch):
        monkeypatch.setattr(gw, "_get_momentum_movers", lambda: ["FAST"])
        assert asyncio.run(gw._movers_with_deadline()) == (["FAST"], False)

    def test_warning_names_the_deadline_actually_used(self, monkeypatch, caplog):
        monkeypatch.setenv("SCAN_UNIVERSE_COLD_MOVERS_DEADLINE_S", "0.1")
        release = threading.Event()
        self._slow(monkeypatch, release)

        async def go():
            with caplog.at_level("WARNING", logger=gw.logger.name):
                await gw._movers_with_deadline()
            release.set()
            await asyncio.sleep(0.2)

        asyncio.run(go())
        assert "momentum movers not ready within 0s" in caplog.text
