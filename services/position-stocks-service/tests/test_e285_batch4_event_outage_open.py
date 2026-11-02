"""E285 batch 4: event-check outage at the open. Pre-open warm-up, half size instead of EVENT_CHECK_UNAVAILABLE, never fail open on a known negative event."""
import asyncio
import types
from datetime import datetime, timezone

import config
from screening import quality_gate as qg


def _cfg(monkeypatch, **kw):
    base = dict(QUALITY_GATE_EVENT_FAIL_BLOCK=True, QUALITY_GATE_EVENT_RETRY_TIMEOUT_S=0.0, QUALITY_GATE_EVENT_SHARED_FETCH=False, QUALITY_GATE_RESULT_TTL_S=0.0,
                QUALITY_GATE_EVENT_MEMORY_H=24.0, QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S=900.0, QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE=True, QUALITY_GATE_TYPED_NEG_MEMORY_H=2.0,
                TYPED_EVENT_ENABLED=True, NEWS_NEGATIVE_BLOCK_ENABLED=True, QUALITY_GATE_PREOPEN_WARM=True, QUALITY_GATE_PREOPEN_WARM_START_IST="08:45", QUALITY_GATE_PREOPEN_WARM_CHUNK=2)
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setattr(config, k, v, raising=False)
    qg.reset_runtime_state()
    qg._event_fail_since[0] = None


def _fakes(monkeypatch, *, event_ok, typed=None):
    async def _ft(client, symbol, timeout=None):
        return (60.0, 55.0, 5000.0)

    async def _ev(client, symbol, timeout=None):
        if event_ok:
            return True, 70.0, False
        qg._event_failed.add(symbol)
        return None, None, False

    async def _typed(symbol):
        return typed
    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)
    monkeypatch.setattr(qg, "_fetch_typed_event", _typed)


def test_outage_with_nothing_remembered_goes_on_at_half_size_flag(monkeypatch):
    _cfg(monkeypatch)
    _fakes(monkeypatch, event_ok=False)
    sig = asyncio.run(qg.check("RELIANCE"))
    assert sig.event_unchecked and sig.event_unverified and sig.passes()[0]          # no longer EVENT_CHECK_UNAVAILABLE; entry.py sizes it at half


def test_switch_off_refuses_as_before(monkeypatch):
    _cfg(monkeypatch, QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE=False)
    _fakes(monkeypatch, event_ok=False)
    sig = asyncio.run(qg.check("RELIANCE"))
    ok, why = sig.passes()
    assert not ok and why.startswith("EVENT_CHECK_UNAVAILABLE")


def test_remembered_negative_news_still_refuses_during_the_outage(monkeypatch):
    _cfg(monkeypatch)
    qg._event_memory["LATENTVIEW"] = (__import__("time").time(), None, "NEWS_NEGATIVE:regulatory action")
    _fakes(monkeypatch, event_ok=False)
    sig = asyncio.run(qg.check("LATENTVIEW"))
    ok, why = sig.passes()
    assert not ok and why.startswith("NEWS_NEGATIVE") and not sig.event_unverified


def test_negative_typed_event_seen_earlier_still_vetoes_when_the_service_fails(monkeypatch):
    _cfg(monkeypatch)
    neg = {"type": "earnings_miss", "polarity": -1, "strength": 0.8, "age_min": 20.0, "announced_at": "x"}
    _fakes(monkeypatch, event_ok=True, typed=neg)
    assert not asyncio.run(qg.check("ABC")).passes()[0]                              # seen while the service worked
    _fakes(monkeypatch, event_ok=False, typed=None)                                  # now the service is down and returns nothing
    ok, why = asyncio.run(qg.check("ABC")).passes()
    assert not ok and why.startswith("NEG_EVENT")


def test_typed_negative_memory_expires_and_does_not_apply_when_the_service_works(monkeypatch):
    _cfg(monkeypatch)
    qg._typed_neg_seen["ABC"] = (__import__("time").time() - 3 * 3600, {"type": "x", "polarity": -1, "strength": 0.9, "age_min": 1.0, "announced_at": "x"})
    _fakes(monkeypatch, event_ok=False, typed=None)
    assert asyncio.run(qg.check("ABC")).passes()[0]                                  # older than 2 h: forgotten
    qg._typed_neg_seen["ABC"] = (__import__("time").time(), {"type": "x", "polarity": -1, "strength": 0.9, "age_min": 1.0, "announced_at": "x"})
    _fakes(monkeypatch, event_ok=True, typed=None)                                   # service works and says no event now: the old one is not reused
    assert asyncio.run(qg.check("ABC")).passes()[0]


def test_preopen_window_and_chunked_warm_up(monkeypatch):
    _cfg(monkeypatch, QUALITY_GATE_EVENT_SHARED_FETCH=True)
    started = []
    monkeypatch.setattr(qg, "prewarm_events", lambda syms: started.append(list(syms)) or len(syms))
    monkeypatch.setattr(qg, "is_nse_holiday", lambda n=None: False)
    ist = lambda h, m: datetime(2026, 11, 3, h, m, tzinfo=timezone.utc).astimezone(qg.ist_now().tzinfo)       # Tuesday
    def at(h, m):
        from datetime import timedelta
        return datetime(2026, 11, 3, h, m, tzinfo=qg.ist_now().tzinfo)
    assert not qg.preopen_warm_due(at(8, 30)) and qg.preopen_warm_due(at(8, 50)) and not qg.preopen_warm_due(at(9, 15))
    syms = ["A", "B", "C", "D", "E"]
    assert qg.preopen_warm_step(lambda: syms, at(8, 50)) == 2 and started[-1] == ["A", "B"]
    assert qg.preopen_warm_step(lambda: syms, at(8, 51)) == 2 and started[-1] == ["C", "D"]
    assert qg.preopen_warm_step(lambda: syms, at(8, 52)) == 1 and started[-1] == ["E"]
    assert not qg.preopen_warm_due(at(8, 53))                                         # list done for the day: no more work
    assert qg.preopen_warm_step(lambda: syms, at(8, 53)) == 0


def test_warm_symbols_universe_only_is_deduplicated_and_capped(monkeypatch):
    _cfg(monkeypatch, QUALITY_GATE_PREOPEN_WARM_MOVERS=0, QUALITY_GATE_PREOPEN_WARM_MAX=3)
    from screening import preopen
    monkeypatch.setattr(preopen, "_universe", lambda: ["A", "B", "A", "C", "D"])
    assert qg.preopen_warm_symbols(None) == ["A", "B"]                               # cap applies to the universe slice (3 names, one duplicate)


def test_entry_applies_the_half_size_penalty_and_main_calls_the_warm_up():
    e = open("orders/entry.py").read()
    assert e.index('_penalties["event_unchecked"]') < e.index("_pen_early = dict(_penalties)")
    m = open("main.py").read()
    assert "quality_gate.preopen_warm_step" in m and m.index("preopen_warm_step") < m.index("premarket plan failed")
