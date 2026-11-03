"""E285 round 163 (plan item 20, Angel One 404 batch).

A quote batch that answers HTTP 404 used to log one generic WARNING per batch per poll cycle (no response body) and was sent again on
the next pass. Now (angelone_batch_404.py + angelone_ws_feed._run_batch):
  * the first 404 per window is one WARNING with the response body and the failure count; the rest are DEBUG;
  * a BACKGROUND batch that 404s is not re-sent for ANGELONE_BATCH_404_BACKOFF_S; held / candidate / unlaned batches always are;
  * other failures (500, timeouts) keep the old line and are never paused.

No network, no DB, no AngelOne credentials.

Run from services/market-data-service:
    python -m pytest tests/test_e285_round163_angelone_404_batch.py -v
"""
from __future__ import annotations

import logging
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_batch_404 as b404


class _Resp:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


class _HttpErr(Exception):
    def __init__(self, code, text=""):
        super().__init__("Client error '%s' for url" % code)
        self.response = _Resp(code, text)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ANGELONE_BATCH_404_BACKOFF_S", "ANGELONE_BATCH_404_LOG_EVERY_S"):
        monkeypatch.delenv(k, raising=False)
    b404.BOX.reset()
    yield
    b404.BOX.reset()


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_is_not_found_only_for_404_and_never_raises():
    assert b404.is_not_found(_HttpErr(404)) is True
    assert b404.is_not_found(_HttpErr(403)) is False
    assert b404.is_not_found(_HttpErr(500)) is False
    assert b404.is_not_found(RuntimeError("x")) is False

    class _Weird(Exception):
        @property
        def response(self):
            raise ValueError("boom")
    assert b404.is_not_found(_Weird()) is False


def test_response_body_is_one_short_line_and_never_raises():
    e = _HttpErr(404, "line one\nline two\r\n" + "x" * 500)
    body = b404.response_body(e)
    assert "\n" not in body and "\r" not in body and len(body) <= 300 and body.startswith("line one line two")
    assert b404.response_body(RuntimeError("x")) == ""
    assert b404.response_body(_HttpErr(404, None)) == ""


def test_env_defaults_and_bad_values(monkeypatch):
    assert b404.backoff_s() == 120.0 and b404.log_every_s() == 600.0
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "abc")
    monkeypatch.setenv("ANGELONE_BATCH_404_LOG_EVERY_S", "")
    assert b404.backoff_s() == 120.0 and b404.log_every_s() == 600.0
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "0")
    assert b404.backoff_s() == 0.0
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "999999")
    assert b404.backoff_s() == 3600.0


def test_batch_key_depends_on_the_token_list():
    assert b404.batch_key(["1", "2"]) == b404.batch_key(["1", "2"])
    assert b404.batch_key(["1", "2"]) != b404.batch_key(["1", "3"])
    assert b404.batch_key([1, 2]) == b404.batch_key(["1", "2"])


# ── log throttle ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_log_once_per_window_then_again_with_the_count():
    clk = _Clock()
    box = b404.BatchBackoff(clock=clk)
    assert box.note_not_found(["1"], True) == (True, 1)          # first failure always logs
    assert box.note_not_found(["2"], True)[0] is False           # same window: suppressed
    assert box.note_not_found(["3"], False)[0] is False
    clk.t += 601
    log_now, n = box.note_not_found(["4"], True)
    assert log_now is True and n == 3                            # 2 suppressed + this one
    assert box.stats["not_found"] == 4 and box.stats["logged"] == 2


def test_log_every_zero_logs_every_failure(monkeypatch):
    monkeypatch.setenv("ANGELONE_BATCH_404_LOG_EVERY_S", "0")
    box = b404.BatchBackoff(clock=_Clock())
    assert [box.note_not_found(["1"], False)[0] for _ in range(3)] == [True, True, True]


# ── pause rules ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_background_batch_is_paused_then_released():
    clk = _Clock()
    box = b404.BatchBackoff(clock=clk)
    assert box.should_skip(["1", "2"], True) is False
    box.note_not_found(["1", "2"], True)
    assert box.should_skip(["1", "2"], True) is True
    assert box.should_skip(["1", "3"], True) is False            # a different list is sent (fail open)
    clk.t += 119
    assert box.should_skip(["1", "2"], True) is True
    clk.t += 2
    assert box.should_skip(["1", "2"], True) is False            # pause over
    assert box.stats["paused_skips"] == 2


def test_other_lanes_are_never_paused():
    box = b404.BatchBackoff(clock=_Clock())
    box.note_not_found(["1", "2"], False)                        # held / candidate / unlaned batch
    assert box.should_skip(["1", "2"], False) is False
    assert box.should_skip(["1", "2"], True) is False            # nothing was remembered for it at all


def test_backoff_zero_never_pauses(monkeypatch):
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "0")
    box = b404.BatchBackoff(clock=_Clock())
    box.note_not_found(["1"], True)
    assert box.should_skip(["1"], True) is False


def test_success_clears_the_pause():
    box = b404.BatchBackoff(clock=_Clock())
    box.note_not_found(["1"], True)
    box.note_ok(["1"])
    assert box.should_skip(["1"], True) is False and box.stats["cleared"] == 1
    box.note_ok(["9"])                                           # nothing held: harmless
    assert box.stats["cleared"] == 1


def test_memory_is_bounded():
    box = b404.BatchBackoff(clock=_Clock())
    for i in range(b404._CAP + 50):
        box.note_not_found([str(i)], True)
    assert len(box._until) <= b404._CAP
    assert box.snapshot()["paused_batches"] <= b404._CAP


# ── the feed wiring ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def feed(monkeypatch):
    mh = types.ModuleType("market_hours")
    mh.is_feed_window_ist = lambda: True
    monkeypatch.setitem(sys.modules, "market_hours", mh)
    sys.modules.pop("angelone_ws_feed", None)
    import angelone_ws_feed as f
    monkeypatch.setattr(f, "_ensure_schema", lambda *a, **k: None)
    yield f
    f._running = False
    if f._thread is not None:
        f._thread.join(timeout=5)
    sys.modules.pop("angelone_ws_feed", None)


class _Session:
    """Answers HTTP `code` for every batch whose first token is in `bad_first`; others succeed."""

    def __init__(self, bad_first, code=404, body="Not Found"):
        self.bad_first, self.code, self.body, self.calls = set(bad_first), code, body, []

    async def ensure_session(self):
        return None

    async def get_quotes_batch(self, exchange, tokens, lane=None):
        self.calls.append(list(tokens))
        if tokens[0] in self.bad_first:
            raise _HttpErr(self.code, self.body)
        return [{"symbolToken": t, "ltp": 10.0, "open": 1, "high": 2, "low": 1, "close": 1, "tradeVolume": 5} for t in tokens]


def _run_cycles(feed, monkeypatch, sess, plan, min_calls):
    import angelone_budget as bud
    ac = types.ModuleType("angelone_client")
    ac.get_session = lambda: sess
    sm = types.ModuleType("angelone_scrip_master")
    all_tok = sorted({t for batch, _ in plan for t in batch})
    sm.get_tokens_bulk = lambda syms, wait_s=0: {x: all_tok[i] for i, x in enumerate(syms)}
    sm.status = lambda: {"loaded_symbols": 10}
    monkeypatch.setitem(sys.modules, "angelone_client", ac)
    monkeypatch.setitem(sys.modules, "angelone_scrip_master", sm)
    monkeypatch.setattr(feed, "BATCH_GAP_S", 0.0)
    monkeypatch.setattr(feed, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(feed, "DB_BATCH_WRITES", True)
    monkeypatch.setattr(feed, "_upsert_ticks_batch_sync", lambda rows: None)
    monkeypatch.setattr(feed, "_upsert_tick_sync", lambda *a: None)
    monkeypatch.setattr(feed, "_plan_batches", lambda tokens, token_map, now=None: [(list(b), lane) for b, lane in plan])
    feed.start_feed_background([f"S{i}" for i in range(len(all_tok))])
    end = time.time() + 10
    while time.time() < end and len(sess.calls) < min_calls:
        time.sleep(0.02)
    feed._running = False
    time.sleep(0.15)
    return bud


def test_is_background_lane_only_for_the_background_lane(feed):
    import angelone_budget as bud
    assert feed._is_background_lane(bud.BACKGROUND) is True
    assert feed._is_background_lane(bud.POSITION) is False
    assert feed._is_background_lane(bud.CANDIDATE) is False
    assert feed._is_background_lane(None) is False


def test_404_background_batch_is_sent_once_others_every_cycle_and_one_warning(feed, monkeypatch, caplog):
    import angelone_budget as bud
    plan = [(["1", "2"], bud.POSITION), (["3", "4"], bud.CANDIDATE), (["5", "6"], bud.BACKGROUND)]
    sess = _Session(bad_first={"1", "3", "5"}, body="Not Found: no such route")
    with caplog.at_level(logging.DEBUG):
        _run_cycles(feed, monkeypatch, sess, plan, min_calls=9)
    sent = lambda first: sum(1 for c in sess.calls if c[0] == first)   # noqa: E731
    assert sent("5") == 1                    # BACKGROUND: sent once, then paused
    assert sent("1") >= 3 and sent("3") >= 3 # held and candidate batches: every cycle, as before
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "answered HTTP 404" in r.getMessage()]
    assert len(warns) == 1 and "Not Found: no such route" in warns[0] and "1 such failure" in warns[0]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING and "quote batch" in r.getMessage() and "failed:" in r.getMessage()]
    assert b404.BOX.stats["not_found"] >= 7 and b404.BOX.stats["paused_skips"] >= 1


def test_a_batch_that_recovers_is_sent_again_after_the_pause(feed, monkeypatch):
    import angelone_budget as bud
    plan = [(["1", "2"], bud.POSITION), (["5", "6"], bud.BACKGROUND)]
    sess = _Session(bad_first={"5"})
    real = sess.get_quotes_batch
    state = {"n": 0}

    async def flaky(exchange, tokens, lane=None):          # the BACKGROUND batch answers 404 once, then works
        if tokens[0] == "5":
            state["n"] += 1
            if state["n"] > 1:
                sess.bad_first.clear()
        return await real(exchange, tokens, lane=lane)
    sess.get_quotes_batch = flaky
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "0.3")
    _run_cycles(feed, monkeypatch, sess, plan, min_calls=12)
    sent5 = sum(1 for c in sess.calls if c[0] == "5")
    assert sent5 >= 2                                       # 404 first, paused, then sent again once the pause ended
    assert sent5 < sum(1 for c in sess.calls if c[0] == "1")   # and it WAS paused in between (fewer sends than the held batch)
    assert b404.BOX.stats["paused_skips"] >= 1
    assert b404.BOX.should_skip(["5", "6"], True) is False  # the success cleared / outlived the pause
    assert feed.get_live_quote("S2") is not None and feed.get_live_quote("S3") is not None


def test_500_keeps_the_old_line_and_pauses_nothing(feed, monkeypatch, caplog):
    import angelone_budget as bud
    plan = [(["5", "6"], bud.BACKGROUND)]
    sess = _Session(bad_first={"5"}, code=500)
    with caplog.at_level(logging.WARNING):
        _run_cycles(feed, monkeypatch, sess, plan, min_calls=3)
    assert len(sess.calls) >= 3              # not paused: sent every cycle
    assert any("quote batch (0-2) failed" in r.getMessage() for r in caplog.records)
    assert not any("answered HTTP 404" in r.getMessage() for r in caplog.records)
    assert b404.BOX.stats["not_found"] == 0 and b404.BOX.stats["paused_skips"] == 0


def test_off_switches_restore_the_old_behaviour(feed, monkeypatch, caplog):
    import angelone_budget as bud
    monkeypatch.setenv("ANGELONE_BATCH_404_BACKOFF_S", "0")
    monkeypatch.setenv("ANGELONE_BATCH_404_LOG_EVERY_S", "0")
    plan = [(["5", "6"], bud.BACKGROUND)]
    sess = _Session(bad_first={"5"})
    with caplog.at_level(logging.WARNING):
        _run_cycles(feed, monkeypatch, sess, plan, min_calls=3)
    assert len(sess.calls) >= 3              # never paused
    warns = [r for r in caplog.records if "answered HTTP 404" in r.getMessage()]
    assert len(warns) >= 3                   # logged every time
