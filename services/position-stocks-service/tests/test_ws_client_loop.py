"""
tests/test_ws_client_loop.py

Covers feed/ws_client.py (session112 round 19) — the last real gap after
round 17's test_ws_client.py, which deliberately left all WS network IO
(_ws_loop, start, stop) untested: "leaving actual asyncio WS integration
to the existing secret-redaction test" (which never actually exercised
the loop body either — it only tested the logging filter in isolation).

feed/ws_client.py was at 51% (108/222 statements missed) — by far the
biggest production-code gap in the service, and it's the live tick feed
that had a real frame-parsing bug fixed in session36 (see this file's own
module docstring). This round closes:
  * _ws_loop's full body (lines 411-611): session-not-ready backoff,
    empty-scrip-master backoff, successful connect+subscribe+chunking,
    heartbeat ping, binary tick parsing/routing/buffer-append, unknown-
    token ticks, on_tick callback dispatch (+ its own exception guard),
    text "pong"/other frames, the clean-close `else` branch (both the
    idle-timeout-is-expected and the "anything else" paths), and the
    ConnectionClosed / generic-Exception handlers.
  * start() / stop() (lines 616-633): task creation, the already-running
    no-op, and stop()'s cancel-and-await-CancelledError path.
  * Three small defensive except-branches round 17 left unreached because
    they need a genuinely malformed buffer, not just a short one:
    _redact_secrets's own except (an object whose __str__ raises),
    _SecretRedactingFilter.filter's except (getMessage() raising), and
    _parse_best5/_parse_frame's struct.unpack_from except/continue paths
    (forced via monkeypatching struct.unpack_from for one call, the same
    technique test_scrip_master.py already uses for its own defensive
    branches — the 20-byte-aligned packet loop in _parse_best5 and the
    len(data)>=51 guard in _parse_frame mean these excepts are otherwise
    unreachable with well-formed-length input).

_ws_loop is driven by monkeypatching asyncio.sleep (module-local
reference) to flip _running=False and return immediately — since the
loop's own `while _running:` condition is checked fresh every pass, and
every code path through the loop body ends in either `continue` (early
backoff branches) or falls through to the same bottom-of-loop
`await asyncio.sleep(backoff)`, this reliably yields exactly one full
pass per test with no real waiting and no CancelledError choreography
needed (unlike main.py's loops, which don't re-check a plain module
global every pass).

websockets.connect is faked with a minimal async-context-manager +
async-iterator (_FakeConnect/_FakeWS) rather than any real socket.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_ws_client_loop.py -q \\
        --cov=feed.ws_client --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANGELONE_CLIENT_ID", "")
os.environ.setdefault("ANGELONE_MPIN", "")
os.environ.setdefault("ANGELONE_API_KEY", "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

import pytest
from websockets.exceptions import ConnectionClosed

import config
import feed.ws_client as wsc


# ─── helpers ────────────────────────────────────────────────────────────────

def _reset_state():
    wsc._tick_buffers.clear()
    wsc._last_volume.clear()
    wsc._last_quote.clear()
    wsc._token_to_symbol.clear()
    wsc._on_tick_callbacks.clear()
    wsc._running = False
    wsc._connected = False
    wsc._reconnect_attempts = 0
    wsc._last_tick_at = None
    wsc._ws_task = None


def _build_mode3_frame(token: str, ltp_paise: int, volume: int = 0,
                        depth_packets: bytes = b"") -> bytes:
    """Same layout as test_ws_client.py's own builder — kept local so this
    file has no import-order dependency on that one. `depth_packets`
    (added for round 26's `_last_quote` coverage) extends the frame past
    the 347-byte SnapQuote threshold so `_parse_frame` returns real
    best_bid/best_ask instead of None."""
    token_bytes = token.encode("ascii").ljust(25, b"\x00")[:25]
    header = (
        bytes([3, 1]) + token_bytes +
        struct.pack("<q", 12345678) +
        struct.pack("<q", int(time.time())) +
        struct.pack("<q", ltp_paise)
    )
    quote_ext = (
        struct.pack("<q", 100) + struct.pack("<q", ltp_paise) +
        struct.pack("<q", volume) +
        struct.pack("<d", 0.0) + struct.pack("<d", 0.0) +
        struct.pack("<q", 0) * 4
    )
    snap_ext = struct.pack("<q", 0) * 3
    if depth_packets:
        depth = depth_packets[:200].ljust(200, b"\x00")
    else:
        depth = b"\x00" * 200
    snap_ext += depth + struct.pack("<q", 0) * 4
    return header + quote_ext + snap_ext


def _build_depth_packet(flag: int, price_paise: int, qty: int = 100) -> bytes:
    """One 20-byte depth packet — same layout as test_ws_client.py's."""
    return (
        struct.pack("<H", flag) + struct.pack("<q", qty) +
        struct.pack("<q", price_paise) + struct.pack("<H", 1)
    )


def _one_shot_sleep_stops_running():
    """Patched onto wsc.asyncio.sleep: every _ws_loop code path either
    `continue`s back to `while _running:` or falls through to the same
    bottom-of-loop sleep call — so flipping _running off here, whichever
    call site hits it, ends the loop after exactly one pass.

    One call site is NOT a backoff/reconnect wait, though: the per-chunk
    subscribe-message pacing sleep (`await asyncio.sleep(0.1)`, a fixed
    literal distinct from every backoff duration used elsewhere) — firing
    the whole-loop stop there would end the loop before the message
    `async for` is ever entered. Let that one pass through as a no-op."""
    async def _sleep(duration=0, *a, **kw):
        if duration == 0.1:
            return
        wsc._running = False
    return _sleep


class _FakeSession:
    def __init__(self, ready=True):
        self.client_id = "CID1"
        self.api_key = "KEY1"
        self.token = "jwt-token" if ready else None
        self.feed_token = "feed-token" if ready else None
        self._ensure_calls = 0

    async def ensure_session(self):
        self._ensure_calls += 1


class _FakeWS:
    """Minimal stand-in for a websockets ClientConnection: async-iterable
    over a fixed message list, records sent frames, exposes close_code/
    close_reason for the loop's post-loop `else` branch."""
    def __init__(self, messages, close_code=1000, close_reason="",
                 raise_during_iter=None, on_next=None):
        self._messages = list(messages)
        self.sent = []
        self.close_code = close_code
        self.close_reason = close_reason
        self._raise_during_iter = raise_during_iter
        self._on_next = on_next  # optional callback(index) fired before each yield

    async def send(self, msg):
        self.sent.append(msg)

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._raise_during_iter and self._idx == self._raise_during_iter[0]:
            raise self._raise_during_iter[1]
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        if self._on_next:
            self._on_next(self._idx)
        msg = self._messages[self._idx]
        self._idx += 1
        return msg


class _FakeConnect:
    """Stands in for the object `websockets.connect(...)` returns, used
    only as `async with ... as ws:`."""
    def __init__(self, ws=None, raise_on_enter=None):
        self._ws = ws
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter:
            raise self._raise_on_enter
        return self._ws

    async def __aexit__(self, *exc):
        return False


def _patch_common(monkeypatch, session, symbol_token_map, connect_obj):
    monkeypatch.setattr(wsc, "get_session", lambda: session)
    monkeypatch.setattr(wsc, "get_all_nse_eq", lambda: symbol_token_map)
    monkeypatch.setattr(wsc.websockets, "connect", lambda *a, **kw: connect_obj)
    monkeypatch.setattr(wsc.asyncio, "sleep", _one_shot_sleep_stops_running())


# ══════════════════════════════════════════════════════════════════════════════
# _ws_loop — early backoff branches
# ══════════════════════════════════════════════════════════════════════════════

class TestWsLoopEarlyBackoff:
    def setup_method(self):
        _reset_state()

    def test_session_not_ready_backs_off_and_stops(self, monkeypatch):
        session = _FakeSession(ready=False)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect())
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        assert wsc._connected is False
        assert wsc._reconnect_attempts == 1
        assert session._ensure_calls == 1

    def test_empty_scrip_master_backs_off_and_stops(self, monkeypatch):
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {}, _FakeConnect())
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        assert wsc._connected is False
        assert wsc._reconnect_attempts == 1


# ══════════════════════════════════════════════════════════════════════════════
# _ws_loop — successful connect + message handling
# ══════════════════════════════════════════════════════════════════════════════

class TestWsLoopHappyPath:
    def setup_method(self):
        _reset_state()

    def test_binary_tick_parsed_routed_and_buffered(self, monkeypatch):
        frame = _build_mode3_frame("3045", ltp_paise=100050, volume=5000)
        ws = _FakeWS([frame], close_code=1000, close_reason="")
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        received = []
        wsc.register_on_tick(lambda sym, ltp, vol, ts: received.append((sym, ltp, vol)))
        wsc._running = True
        asyncio.run(wsc._ws_loop())

        assert wsc._connected is False  # reset after the connection ended
        assert wsc._last_tick_at is not None
        buf = wsc.get_tick_buffer("SBIN")
        assert len(buf) == 1
        assert buf[0][1] == pytest.approx(1000.50)
        assert wsc.get_last_volume("SBIN") == 5000
        assert received == [("SBIN", pytest.approx(1000.50), 5000)]
        # subscribed + chunked correctly (one chunk, one token)
        assert len(ws.sent) == 1
        assert '"tokens": ["3045"]' in ws.sent[0] or "3045" in ws.sent[0]

    def test_unknown_token_tick_is_dropped_silently(self, monkeypatch):
        frame = _build_mode3_frame("9999", ltp_paise=5000)  # not in the map
        ws = _FakeWS([frame])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        assert wsc.get_tick_buffer("SBIN") == []
        # a tick WAS parsed (updates last_tick_at) even though no symbol matched
        assert wsc._last_tick_at is not None

    def test_on_tick_callback_exception_is_swallowed(self, monkeypatch):
        frame = _build_mode3_frame("3045", ltp_paise=10000)
        ws = _FakeWS([frame])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))

        def _boom(sym, ltp, vol, ts):
            raise RuntimeError("callback exploded")
        wsc.register_on_tick(_boom)
        wsc._running = True
        asyncio.run(wsc._ws_loop())  # must not raise
        assert wsc.get_tick_buffer("SBIN")  # tick still buffered despite callback error

    def test_text_pong_frame_ignored_other_text_logged(self, monkeypatch):
        ws = _FakeWS(["pong", "some-status-message"])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())  # must not raise either way

    def test_running_false_breaks_out_of_message_loop(self, monkeypatch):
        frame1 = _build_mode3_frame("3045", ltp_paise=10000)
        frame2 = _build_mode3_frame("3045", ltp_paise=20000)

        def _stop_before_second(idx):
            if idx == 1:
                wsc._running = False
        ws = _FakeWS([frame1, frame2], on_next=_stop_before_second)
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        # only the first tick was ever processed — the loop broke before the second
        assert len(wsc.get_tick_buffer("SBIN")) == 1

    def test_heartbeat_ping_sent_when_interval_elapsed(self, monkeypatch):
        monkeypatch.setattr(config, "ANGELONE_WS_HEARTBEAT_INTERVAL_S", 0.0)
        frame = _build_mode3_frame("3045", ltp_paise=10000)
        ws = _FakeWS([frame])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        assert "ping" in ws.sent

    def test_heartbeat_send_failure_is_swallowed(self, monkeypatch):
        # Covers lines 510-511: `except Exception: pass` around the
        # heartbeat `ws.send("ping")` — the happy-path heartbeat test above
        # never lets that send fail, so the guard itself was never hit.
        monkeypatch.setattr(config, "ANGELONE_WS_HEARTBEAT_INTERVAL_S", 0.0)
        frame = _build_mode3_frame("3045", ltp_paise=10000)
        ws = _FakeWS([frame])

        async def _boom_send(msg):
            if msg == "ping":
                raise RuntimeError("send failed")
            ws.sent.append(msg)
        ws.send = _boom_send
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())  # must not raise
        # the tick after the failed heartbeat still gets processed normally
        assert len(wsc.get_tick_buffer("SBIN")) == 1

    def test_stale_ticks_are_pruned_from_the_buffer(self, monkeypatch):
        # Covers line 542: `buf.popleft()` inside the time-bounded prune
        # loop. Pre-seed the buffer with an ancient (epoch) tick — any new
        # tick's real `ts` (time.time()) is always far more than
        # _MAX_BUFFER_AGE_S (65 min) past it, so the prune loop evicts it.
        wsc._tick_buffers["SBIN"].append((0.0, 999.0))
        frame = _build_mode3_frame("3045", ltp_paise=10000)
        ws = _FakeWS([frame])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        buf = wsc.get_tick_buffer("SBIN")
        assert len(buf) == 1  # ancient entry evicted, only the new tick remains
        assert buf[0][1] == pytest.approx(100.00)

    def test_best_bid_ask_stored_from_full_depth_frame(self, monkeypatch):
        # Covers line 549: `_last_quote[symbol] = (best_bid, best_ask, ts)`.
        # Every other happy-path test uses a bare (non-depth) frame, whose
        # `_parse_frame` always returns best_bid=best_ask=None, so this
        # branch was never reached.
        ask_pkt = _build_depth_packet(flag=0, price_paise=100100)
        bid_pkt = _build_depth_packet(flag=1, price_paise=100000)
        depth = ask_pkt + bid_pkt + b"\x00" * (8 * 20)
        frame = _build_mode3_frame("3045", ltp_paise=100050, depth_packets=depth)
        ws = _FakeWS([frame])
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        best_bid, best_ask = wsc.get_best_bid_ask("SBIN")
        assert best_bid == pytest.approx(1000.00)
        assert best_ask == pytest.approx(1001.00)

    def test_idle_timeout_close_logged_as_expected(self, monkeypatch, caplog):
        ws = _FakeWS([], close_code=1001, close_reason="Connection Idle Timeout")
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        with caplog.at_level(logging.INFO, logger="position-stocks-ws-client"):
            asyncio.run(wsc._ws_loop())
        assert any("idle-timeout" in r.message for r in caplog.records)

    def test_other_clean_close_logged_as_warning(self, monkeypatch, caplog):
        ws = _FakeWS([], close_code=1000, close_reason="some other reason")
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        with caplog.at_level(logging.WARNING, logger="position-stocks-ws-client"):
            asyncio.run(wsc._ws_loop())
        assert any("server closed the connection cleanly" in r.message for r in caplog.records)

    def test_multi_chunk_subscribe(self, monkeypatch):
        monkeypatch.setattr(config, "ANGELONE_WS_MAX_SYMBOLS_PER_CONNECTION", 2)
        ws = _FakeWS([])
        session = _FakeSession(ready=True)
        symbol_map = {"A": "1", "B": "2", "C": "3"}
        _patch_common(monkeypatch, session, symbol_map, _FakeConnect(ws))
        wsc._running = True
        asyncio.run(wsc._ws_loop())
        assert len(ws.sent) == 2  # 3 tokens, chunk size 2 -> 2 messages


# ══════════════════════════════════════════════════════════════════════════════
# _ws_loop — error handling
# ══════════════════════════════════════════════════════════════════════════════

class TestWsLoopErrors:
    def setup_method(self):
        _reset_state()

    def test_connection_closed_during_iteration_is_caught(self, monkeypatch, caplog):
        ws = _FakeWS([], raise_during_iter=(0, ConnectionClosed(None, None)))
        session = _FakeSession(ready=True)
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, _FakeConnect(ws))
        wsc._running = True
        with caplog.at_level(logging.WARNING, logger="position-stocks-ws-client"):
            asyncio.run(wsc._ws_loop())  # must not raise
        assert wsc._connected is False

    def test_generic_exception_on_connect_is_caught(self, monkeypatch, caplog):
        session = _FakeSession(ready=True)
        connect = _FakeConnect(raise_on_enter=RuntimeError("connect refused"))
        _patch_common(monkeypatch, session, {"SBIN": "3045"}, connect)
        wsc._running = True
        with caplog.at_level(logging.ERROR, logger="position-stocks-ws-client"):
            asyncio.run(wsc._ws_loop())  # must not raise
        assert wsc._connected is False
        assert wsc._reconnect_attempts == 1


# ══════════════════════════════════════════════════════════════════════════════
# start() / stop()
# ══════════════════════════════════════════════════════════════════════════════

class TestStartStop:
    def setup_method(self):
        _reset_state()

    def test_start_creates_task_and_sets_running(self, monkeypatch):
        async def _stub_loop():
            await asyncio.Event().wait()  # never returns on its own
        monkeypatch.setattr(wsc, "_ws_loop", _stub_loop)

        async def _run():
            await wsc.start()
            assert wsc._running is True
            assert wsc._ws_task is not None
            assert not wsc._ws_task.done()
            # calling start() again while running is a no-op (same task)
            task_before = wsc._ws_task
            await wsc.start()
            assert wsc._ws_task is task_before
            await wsc.stop()
            assert wsc._running is False
            assert wsc._connected is False
        asyncio.run(_run())

    def test_stop_when_no_task_is_a_noop(self):
        wsc._running = True
        wsc._connected = True
        wsc._ws_task = None
        asyncio.run(wsc.stop())
        assert wsc._running is False
        assert wsc._connected is False

    def test_stop_when_task_already_done_skips_cancel(self):
        async def _finished():
            return None

        async def _run():
            task = asyncio.create_task(_finished())
            await asyncio.sleep(0)  # let it complete
            assert task.done()
            wsc._ws_task = task
            wsc._running = True
            await wsc.stop()
            assert wsc._running is False
        asyncio.run(_run())


# ══════════════════════════════════════════════════════════════════════════════
# Small defensive except-branches unreachable with well-formed-length input
# ══════════════════════════════════════════════════════════════════════════════

class TestRedactSecretsExceptionPath:
    def test_unstringable_object_returns_placeholder(self):
        class _Explodes:
            def __str__(self):
                raise RuntimeError("no string for you")
        assert wsc._redact_secrets(_Explodes()) == "<text withheld: redaction failed>"


class TestSecretRedactingFilterExceptionPath:
    def test_getmessage_exception_is_swallowed_and_record_passes(self):
        f = wsc._SecretRedactingFilter()

        class _BadRecord:
            def getMessage(self):
                raise ValueError("bad format string")
        # filter() must never raise, and must still return True (never drop a log record)
        assert f.filter(_BadRecord()) is True


class TestParseBest5ExceptionPath:
    def test_malformed_packet_is_skipped_via_continue(self, monkeypatch):
        from feed import ws_client as _wsc_mod

        real_unpack = struct.unpack_from
        calls = {"n": 0}

        def _flaky_unpack(fmt, buf, offset=0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise struct.error("forced parse failure")
            return real_unpack(fmt, buf, offset)
        monkeypatch.setattr(_wsc_mod.struct, "unpack_from", _flaky_unpack)

        packet1 = b"\x00" * 20  # first packet -> forced failure -> continue
        packet2 = (
            struct.pack("<H", 0) + struct.pack("<q", 100) +
            struct.pack("<q", 5000) + struct.pack("<H", 1)
        )  # a real, valid ASK packet
        data = packet1 + packet2 + b"\x00" * (8 * 20)
        best_bid, best_ask = _wsc_mod._parse_best5(data)
        assert best_ask == pytest.approx(50.00)


class TestParseFrameExceptionPath:
    def test_struct_error_returns_none(self, monkeypatch):
        from feed import ws_client as _wsc_mod
        monkeypatch.setattr(
            _wsc_mod.struct, "unpack_from",
            lambda *a, **kw: (_ for _ in ()).throw(struct.error("forced")),
        )
        data = b"\x00" * 60
        assert _wsc_mod._parse_frame(data) is None
