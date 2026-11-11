"""E418: single-stock Analyse (GET /stock/{symbol}). (1) The fundamentals write-back to the Neon data feed (`save_stock_feed`) ran on the event loop;
it now runs in a worker thread. (2) The enrichment tasks outlived a cancelled request; they are now cancelled with it."""
from __future__ import annotations

import asyncio
import threading

from test_main_stock_scan_routes import Env, FakeClient, FakeResp, _full_decide, env, gw, stock  # noqa: F401


def _bare(env, **over):
    raw = {"decision": "HOLD", "close": 100.0, "support": 90.0, "resistance": 110.0,
           "technical_score": 50, "fundamental_score": 50, "reasons": {}}
    raw.update(over)
    env.passthrough()
    env.decide("TCS", raw)


def test_the_fundamental_writeback_runs_off_the_event_loop(env):
    threads = []
    saved = env._save

    def save(sym, row):
        threads.append(threading.current_thread())
        return saved(sym, row)

    env.mp.setattr(__import__("data_feed"), "save_stock_feed", save)
    _bare(env, reasons={"fundamental": ["data unavailable"]}, fundamental_score=None)
    env.client.routes[gw.FUNDAMENTAL_URL + "/analyze/TCS"] = FakeResp(200, {"fundamental_score": 66, "metrics": {"pe": 20}})
    out = stock("TCS")
    assert out["fundamental_score"] == 66 and env.saved and env.saved[0][0] == "TCS"
    assert threads and all(t is not threading.main_thread() for t in threads)      # was the main thread (the event loop)


class _SlowClient(FakeClient):
    """decide answers at once; every enrichment GET blocks until cancelled and records that it was."""

    def __init__(self):
        super().__init__()
        self.started, self.cancelled = [], []

    async def get(self, url, **kw):
        if "/decide/" in url:
            return await super().get(url, **kw)
        self.started.append(url)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(url)
            raise


def test_cancelling_the_request_cancels_the_enrichment_tasks(env):
    env.client = _SlowClient()
    _bare(env)                                  # news / events / prediction have no data -> three enrichment GETs
    client = env.client

    async def go():
        task = asyncio.ensure_future(gw.get_stock_decision("TCS"))
        for _ in range(200):
            if len(client.started) == 3:
                break
            await asyncio.sleep(0)
        assert len(client.started) == 3
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert sorted(client.cancelled) == sorted(client.started) and len(client.cancelled) == 3
