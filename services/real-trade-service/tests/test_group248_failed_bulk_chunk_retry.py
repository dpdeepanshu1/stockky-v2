"""group248 (2026-10-08 10:16 IST log): one /quotes/bulk chunk of a 709-symbol poll timed out and its symbols fell to 120
per-symbol GET /quote calls (AngelOne lane shed -> saturated Yahoo path -> ReadTimeout). They are now asked once more as
smaller bulk calls first."""
import asyncio
from datetime import datetime, timezone

import pytest

from market_feed import feed as f
from tests.test_group225_priority_lane_backpressure import _tick


def _run(coro):
    return asyncio.run(coro)


# ── _bulk_ticks: failed_symbols and chunk_size ──────────────────────────────

class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body, "x"

    def json(self):
        return self._body


class _Client:
    def __init__(self, behave):
        self.behave, self.calls = behave, []

    async def post(self, url, json=None, timeout=None):
        self.calls.append((list(json["symbols"]), timeout))
        return self.behave(json["symbols"])


def _ok(symbols):
    now = datetime.now(timezone.utc).isoformat()
    return _Resp(200, {"quotes": [{"symbol": s, "price": 10.0, "fetched_at": now} for s in symbols]})


def test_bulk_ticks_records_the_symbols_of_failed_chunks_and_honours_chunk_size(monkeypatch):
    monkeypatch.setattr(f, "FEED_BULK_CHUNK_SIZE", 4)

    def behave(symbols):
        if "S4" in symbols:
            raise TimeoutError("slow")
        if "S8" in symbols:
            return _Resp(500)
        return _ok(symbols)

    client = _Client(behave)
    stats = {}
    out = _run(f._bulk_ticks(client, [f"S{i}" for i in range(10)], stats=stats, schedule_atr=False))
    assert sorted(out) == ["S0", "S1", "S2", "S3"]
    assert stats["failed"] == 2
    assert sorted(stats["failed_symbols"]) == ["S4", "S5", "S6", "S7", "S8", "S9"]
    assert [len(c) for c, _t in client.calls] == [4, 4, 2]

    client2 = _Client(_ok)
    _run(f._bulk_ticks(client2, [f"S{i}" for i in range(10)], chunk_size=5, schedule_atr=False))
    assert [len(c) for c, _t in client2.calls] == [5, 5]


# ── _get_quotes_unique: retry of failed chunks ──────────────────────────────

class _Dummy:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture()
def env(monkeypatch):
    f.clear_priority_share()
    f.clear_dead_symbols()
    monkeypatch.setattr(f.httpx, "AsyncClient", _Dummy)
    monkeypatch.setattr(f, "FEED_BULK_MIN_SYMBOLS", 25)
    monkeypatch.setattr(f, "FEED_LEFTOVER_MAX", 120)
    st = {"bulk": [], "single": [], "first_answers": 10, "retry_answers": True}

    async def fake_bulk(client, symbols, *, timeout=None, max_age_s=None, stats=None, schedule_atr=True,
                        chunk_size=None):
        st["bulk"].append({"n": len(symbols), "timeout": timeout, "chunk_size": chunk_size})
        if stats is not None:
            stats.setdefault("failed", 0)
            stats.setdefault("reasons", {})
        if chunk_size is None:                                  # the first pass
            ok = list(symbols)[: st["first_answers"]]
            lost = list(symbols)[st["first_answers"]:]
            if lost and stats is not None:
                stats["failed"] += 1
                stats.setdefault("failed_symbols", []).extend(lost)
            return {f._clean_sym(s): _tick(f._clean_sym(s), 77.0, "bulk(t)") for s in ok}
        if not st["retry_answers"]:
            if stats is not None:
                stats["failed"] = stats.get("failed", 0) + 1
            return {}
        return {f._clean_sym(s): _tick(f._clean_sym(s), 78.0, "bulk(retry)") for s in symbols}

    async def fake_get_quote(client, symbol, **kw):
        st["single"].append(symbol)
        return _tick(symbol, 50.0, "single")

    monkeypatch.setattr(f, "_bulk_ticks", fake_bulk)
    monkeypatch.setattr(f, "get_quote", fake_get_quote)
    yield st
    f.clear_priority_share()


def _syms(n=40):
    return [f"S{i}" for i in range(n)]


def test_symbols_of_a_failed_chunk_are_recovered_by_a_smaller_bulk_call(env, caplog):
    with caplog.at_level("INFO"):
        out = _run(f.get_quotes(_syms()))
    assert len(out) == 40 and env["single"] == []
    assert out["S39"].source == "bulk(retry)" and out["S0"].source == "bulk(t)"
    assert [b["n"] for b in env["bulk"]] == [40, 30]
    assert env["bulk"][1]["chunk_size"] == f.FEED_BULK_RETRY_CHUNK_SIZE
    assert env["bulk"][1]["timeout"] == f.FEED_BULK_RETRY_TIMEOUT_S
    assert "30 symbol(s) of failed bulk chunk(s) asked again in smaller bulk calls: priced 30" in caplog.text
    assert "chunk(s) failed" not in caplog.text.split("bulk-first priced")[-1]


def test_a_retry_that_fails_again_leaves_the_symbols_to_the_per_symbol_path(env, caplog):
    env["retry_answers"] = False
    with caplog.at_level("INFO"):
        out = _run(f.get_quotes(_syms()))
    assert len(out) == 40 and len(env["single"]) == 30
    assert "1 call(s) failed again" in caplog.text


def test_no_retry_when_the_first_pass_answered_nothing(env):
    env["first_answers"] = 0
    out = _run(f.get_quotes(_syms()))
    assert [b["n"] for b in env["bulk"]] == [40]               # market-data looks down: no second bulk round
    assert len(out) == 40 and len(env["single"]) == 40


def test_retry_can_be_switched_off(env, monkeypatch):
    monkeypatch.setattr(f, "FEED_BULK_RETRY_FAILED", False)
    out = _run(f.get_quotes(_syms()))
    assert [b["n"] for b in env["bulk"]] == [40]
    assert len(out) == 40 and len(env["single"]) == 30


def test_nothing_is_retried_when_no_chunk_failed(env):
    env["first_answers"] = 40
    out = _run(f.get_quotes(_syms()))
    assert [b["n"] for b in env["bulk"]] == [40] and len(out) == 40 and env["single"] == []
