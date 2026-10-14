"""group356 (14 Oct 10:38 IST restart log): the quality gate used to give up on a busy analysis service, send a SECOND event request,
and throw away the answer that arrived a few seconds later (V2RETAIL and EMBDL lost as EVENT_CHECK_UNAVAILABLE). The event call is
now ONE in-flight request per symbol that the gate waits on; a late answer is kept for the next cycle."""
import asyncio

import pytest

import config
from screening import quality_gate as qg


@pytest.fixture()
def gate(monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_SHARED_FETCH", True)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True)
    monkeypatch.setattr(config, "QUALITY_GATE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_RETRY_TIMEOUT_S", 0.15)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_BG_TIMEOUT_S", 5.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_LATE_TTL_S", 300.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S", 900.0)
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_MEMORY_H", 24.0)
    qg.reset_runtime_state()
    qg._event_fail_since[0] = None
    yield qg
    qg.reset_runtime_state()
    qg._event_fail_since[0] = None


def _fakes(monkeypatch, *, delay=0.0, ok=True, calls=None):
    async def _ft(client, symbol, timeout=None):
        return (60.0, 55.0, 5000.0)

    async def _ev(client, symbol, timeout=None):
        if calls is not None:
            calls.append((symbol, timeout))
        await asyncio.sleep(delay)
        if not ok:
            qg._event_failed.add(symbol)
            return None, None, False
        qg._earnings_seen.pop(symbol, None)
        return True, 70.0, False

    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)


def test_fast_answer_is_used_directly(gate, monkeypatch):
    calls = []
    _fakes(monkeypatch, delay=0.0, calls=calls)
    sig = asyncio.run(gate.check("SYNCOMF"))
    assert not sig.event_unchecked and sig.passes()[0] and sig.has_positive_catalyst is True
    assert len(calls) == 1 and calls[0][1] == 5.0           # one request, with the long hard limit


def test_slow_answer_inside_the_retry_window_is_waited_for_on_the_same_request(gate, monkeypatch):
    calls = []
    _fakes(monkeypatch, delay=0.12, calls=calls)             # slower than QUALITY_GATE_TIMEOUT_S (0.05), inside 0.05 + 0.15
    sig = asyncio.run(gate.check("MCLEODRUSS"))
    assert not sig.event_unchecked and sig.passes()[0]
    assert len(calls) == 1                                   # NO second request


def test_answer_after_the_whole_window_is_kept_for_the_next_cycle(gate, monkeypatch):
    calls = []
    _fakes(monkeypatch, delay=0.4, calls=calls)

    async def two_cycles():
        first = await gate.check("V2RETAIL")                  # gives up after 0.05 + 0.15 s: unchecked this cycle
        await asyncio.sleep(0.4)                             # the request finishes meanwhile
        second = await gate.check("V2RETAIL")                # next cycle: uses the kept answer
        return first, second

    first, second = asyncio.run(two_cycles())
    assert first.event_unchecked and first.passes()[1].startswith("EVENT_CHECK_UNAVAILABLE")
    assert not second.event_unchecked and second.passes()[0] and second.has_positive_catalyst is True
    assert len(calls) == 1                                   # the second cycle did not send another request


def test_concurrent_checks_of_one_symbol_share_one_request(gate, monkeypatch):
    calls = []
    _fakes(monkeypatch, delay=0.1, calls=calls)

    async def both():
        return await asyncio.gather(gate.check("EMBDL"), gate.check("EMBDL"))

    a, b = asyncio.run(both())
    assert not a.event_unchecked and not b.event_unchecked
    assert len(calls) == 1


def test_a_failed_request_is_unchecked_and_not_kept(gate, monkeypatch):
    _fakes(monkeypatch, delay=0.0, ok=False)
    sig = asyncio.run(gate.check("RAMRAT"))
    assert sig.event_unchecked and sig.passes()[1].startswith("EVENT_CHECK_UNAVAILABLE")
    assert "RAMRAT" not in gate._event_late


def test_earnings_date_and_negative_news_from_a_late_answer_are_not_lost(gate, monkeypatch):
    async def _ft(client, symbol, timeout=None):
        return (60.0, 55.0, 5000.0)

    async def _ev(client, symbol, timeout=None):
        await asyncio.sleep(0.4)
        gate._earnings_seen[symbol] = config_today()
        gate._news_seen[symbol] = "NEWS_NEGATIVE:regulatory_action on record - not a long"
        return True, 10.0, False

    from tz_utils import ist_today_str
    config_today = ist_today_str
    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)

    async def two():
        await gate.check("XYZ")
        await asyncio.sleep(0.4)
        return await gate.check("XYZ")

    sig = asyncio.run(two())
    assert sig.earnings_date == ist_today_str() and sig.negative_news.startswith("NEWS_NEGATIVE")
    assert not sig.passes()[0]


def test_stale_late_answer_is_ignored(gate, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_LATE_TTL_S", 0.01)
    _fakes(monkeypatch, delay=0.0)
    gate._event_late["OLD"] = (gate._time.monotonic() - 5.0, True, 99.0, False, None, None)
    assert gate._take_late_event("OLD") is None


def test_fail_block_off_stays_quick_and_fail_open(gate, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", False)
    calls = []
    _fakes(monkeypatch, delay=0.3, calls=calls)

    async def timed():
        t0 = asyncio.get_running_loop().time()
        sig = await gate.check("QUICK")
        return sig, asyncio.get_running_loop().time() - t0

    sig, took = asyncio.run(timed())
    assert not sig.event_unchecked and sig.passes()[0]
    assert took < 0.2                                        # no retry wait when the gate is fail-open


def test_switch_off_uses_the_group344_path(gate, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_SHARED_FETCH", False)
    seen = []

    async def _ft(client, symbol, timeout=None):
        return (60.0, 55.0, 5000.0)

    async def _ev(client, symbol, timeout=None):
        seen.append(timeout)
        return True, 70.0, False

    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)
    sig = asyncio.run(gate.check("OLDWAY"))
    assert sig.passes()[0] and seen == [None]               # the old first call: no explicit timeout


# ---------------------------------------------------------------- group356b: fundamental / technical
def _ft_fakes(monkeypatch, *, delay=0.0, calls=None, values=(60.0, 55.0, 5000.0)):
    async def _ft(client, symbol, timeout=None):
        if calls is not None:
            calls.append((symbol, timeout))
        await asyncio.sleep(delay)
        return values

    async def _ev(client, symbol, timeout=None):
        return True, 70.0, False

    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)


def test_fund_tech_fast_answer_is_used_with_one_request(gate, monkeypatch):
    calls = []
    _ft_fakes(monkeypatch, calls=calls)
    sig = asyncio.run(gate.check("AAA"))
    assert (sig.fundamental_score, sig.technical_score, sig.market_cap_cr) == (60.0, 55.0, 5000.0)
    assert len(calls) == 1 and calls[0][1] == 5.0


def test_no_cache_symbol_waits_the_retry_window_for_a_slow_fund_tech(gate, monkeypatch):
    calls = []
    _ft_fakes(monkeypatch, delay=0.12, calls=calls)            # slower than 0.05, inside 0.05 + 0.15
    sig = asyncio.run(gate.check("NEWCO", cached=None))
    assert sig.fundamental_score == 60.0 and sig.technical_score == 55.0
    assert len(calls) == 1


def test_cached_symbol_stays_quick_and_uses_the_cache(gate, monkeypatch):
    _ft_fakes(monkeypatch, delay=0.4)
    cached = qg.QualitySignal(symbol="OLDCO", fundamental_score=70.0, technical_score=65.0, market_cap_cr=900.0)

    async def timed():
        t0 = asyncio.get_running_loop().time()
        sig = await gate.check("OLDCO", cached=cached)
        return sig, asyncio.get_running_loop().time() - t0

    sig, took = asyncio.run(timed())
    assert sig.fundamental_score == 70.0 and sig.technical_score == 65.0      # cache fallback
    assert took < 0.3                                                         # did not wait out the retry window


def test_late_fund_tech_answer_is_kept_for_the_next_cycle(gate, monkeypatch):
    calls = []
    _ft_fakes(monkeypatch, delay=0.4, calls=calls)
    cached = qg.QualitySignal(symbol="LATE", fundamental_score=70.0, technical_score=65.0, market_cap_cr=900.0)

    async def two():
        first = await gate.check("LATE", cached=cached)
        await asyncio.sleep(0.4)
        second = await gate.check("LATE", cached=cached)
        return first, second

    first, second = asyncio.run(two())
    assert first.fundamental_score == 70.0                                     # cache this cycle
    assert (second.fundamental_score, second.technical_score) == (60.0, 55.0)  # live late answer next cycle
    assert len(calls) == 1


def test_fund_tech_that_never_answers_stays_fail_open(gate, monkeypatch):
    async def _ft(client, symbol, timeout=None):
        return None, None, None

    async def _ev(client, symbol, timeout=None):
        return True, 70.0, False

    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)
    sig = asyncio.run(gate.check("DARK", cached=None))
    assert sig.passes()[0] and "floor check skipped" in sig.passes()[1]
    assert "DARK" not in gate._ft_late
