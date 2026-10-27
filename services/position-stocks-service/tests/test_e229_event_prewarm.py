"""E229: quality_gate.prewarm_events starts one shared event request per symbol without waiting, skips in-flight / freshly answered ones."""
import asyncio
import time
from unittest import mock

from screening import quality_gate as qg


def test_prewarm_starts_skips_and_never_raises():
    calls = []

    async def fake_worker(sym):
        calls.append(sym)
        await asyncio.sleep(0)
        return False, (True, 0.5, False), None, None

    async def run():
        qg.clear_cache() if hasattr(qg, "clear_cache") else None
        qg._event_inflight.clear(); qg._event_late.clear()
        with mock.patch.object(qg, "_event_worker", fake_worker):
            assert qg.prewarm_events(["AAA", "BBB"]) == 2
            assert qg.prewarm_events(["AAA", "BBB"]) == 0          # already in flight
            await asyncio.sleep(0.05)
            assert qg.prewarm_events(["AAA"]) == 0                  # fresh late answer kept
            qg._event_late["CCC"] = (time.monotonic() - 10_000, True, 0.1, False, None, None)
            assert qg.prewarm_events(["CCC"]) == 1                  # stale answer -> warm again
            await asyncio.sleep(0.05)
        assert sorted(set(calls)) == ["AAA", "BBB", "CCC"]
        assert qg.prewarm_events(None) == 0                        # bad input never raises
    asyncio.run(run())


def test_prewarm_without_loop_is_a_noop():
    assert qg.prewarm_events(["ZZZ"]) == 0
