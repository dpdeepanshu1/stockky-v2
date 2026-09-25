"""
tests/test_ws_client.py

Covers feed/ws_client.py (session112 round 17).

test_ws_client_secret_redaction.py already covers the redaction filter
exhaustively (it was written as a standalone regression for session99).
This file covers everything else:
  * _parse_best5 — depth-packet parsing, flag/price handling, ASK/BID swap
  * _parse_frame — all frame size paths (mode 1/2/3), bad frames, negative
    LTP/volume guards
  * _build_subscribe_msg — structure, action parameter
  * _build_reverse_map — token→symbol inversion
  * get_tick_buffer / get_last_ltp / get_last_volume / get_best_bid_ask
    — after seeding _tick_buffers / _last_volume / _last_quote directly
  * register_on_tick callback dispatch
  * ws_status — all fields, with/without task
  * Time-bounded buffer pruning (the _MAX_BUFFER_AGE_S fix) — simulated
    directly on _tick_buffers without a running WS loop

All WS network IO (websockets.connect) is NOT invoked — this file only
tests the pure helpers and in-memory state manipulation, leaving actual
asyncio WS integration to the existing secret-redaction test.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_ws_client.py -q \\
        --cov=feed.ws_client --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import time
import threading
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANGELONE_CLIENT_ID", "")
os.environ.setdefault("ANGELONE_MPIN", "")
os.environ.setdefault("ANGELONE_API_KEY", "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

import pytest

import feed.ws_client as wsc


# ─── helpers ────────────────────────────────────────────────────────────────

def _reset_state():
    """Reset mutable in-process WS state between tests."""
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
    """Build a minimal mode-3 SnapQuote frame.

    Common header (51 bytes) + Quote extension (72 bytes) +
    SnapQuote extension (256 bytes to hit the 347-byte depth threshold).
    """
    # Header: [mode=3][exch=1][token 25 bytes][seq 8][feed_time 8][ltp 8]
    token_bytes = token.encode("ascii").ljust(25, b"\x00")[:25]
    header = (
        bytes([3, 1]) +
        token_bytes +
        struct.pack("<q", 12345678) +          # sequence
        struct.pack("<q", int(time.time())) +  # feed_time
        struct.pack("<q", ltp_paise)           # LTP
    )
    assert len(header) == 51

    # bytes 51-66: ltq + atp (int64 each)
    quote_ext = struct.pack("<q", 100) + struct.pack("<q", ltp_paise)
    # bytes 67-74: volume
    quote_ext += struct.pack("<q", volume)
    # bytes 75-90: buy_qty (f64) + sell_qty (f64)
    quote_ext += struct.pack("<d", 0.0) + struct.pack("<d", 0.0)
    # bytes 91-122: open/high/low/close (int64 each)
    quote_ext += struct.pack("<q", 0) * 4
    assert len(quote_ext) == 72  # bytes 51:123

    # SnapQuote extension to byte 347+
    # bytes 123-146: last_traded_ts + oi + oi_pct (int64 each)
    snap_ext = struct.pack("<q", 0) * 3
    # bytes 147-346: depth (200 bytes) — use provided or zeros
    if depth_packets:
        depth = depth_packets[:200].ljust(200, b"\x00")
    else:
        depth = b"\x00" * 200
    snap_ext += depth
    # bytes 347-378: circuit/52w (4 × int64)
    snap_ext += struct.pack("<q", 0) * 4

    return header + quote_ext + snap_ext


def _build_depth_packet(flag: int, price_paise: int, qty: int = 100) -> bytes:
    """Build one 20-byte depth packet."""
    return (
        struct.pack("<H", flag) +    # flag
        struct.pack("<q", qty) +     # quantity
        struct.pack("<q", price_paise) +  # price
        struct.pack("<H", 1)         # num_of_orders
    )


# ══════════════════════════════════════════════════════════════════════════════
# _parse_best5
# ══════════════════════════════════════════════════════════════════════════════

class TestParseBest5:
    def test_bid_and_ask_found(self):
        # flag==0 → ASK, flag==1 → BID (AngelOne's documented swap)
        ask_pkt = _build_depth_packet(flag=0, price_paise=100050)  # ₹1000.50
        bid_pkt = _build_depth_packet(flag=1, price_paise=100000)  # ₹1000.00
        # fill remaining 8 packets with zeros
        data = ask_pkt + bid_pkt + b"\x00" * (8 * 20)
        best_bid, best_ask = wsc._parse_best5(data)
        assert best_bid == pytest.approx(1000.00)
        assert best_ask == pytest.approx(1000.50)

    def test_ask_only(self):
        ask_pkt = _build_depth_packet(flag=0, price_paise=50000)
        data = ask_pkt + b"\x00" * (9 * 20)
        best_bid, best_ask = wsc._parse_best5(data)
        assert best_bid is None
        assert best_ask == pytest.approx(500.00)

    def test_zero_price_skipped(self):
        zero_pkt = _build_depth_packet(flag=0, price_paise=0)
        data = zero_pkt + b"\x00" * (9 * 20)
        best_bid, best_ask = wsc._parse_best5(data)
        assert best_ask is None

    def test_only_first_level_used(self):
        # First ask at ₹100, second ask at ₹101
        ask1 = _build_depth_packet(flag=0, price_paise=10000)
        ask2 = _build_depth_packet(flag=0, price_paise=10100)
        data = ask1 + ask2 + b"\x00" * (8 * 20)
        _, best_ask = wsc._parse_best5(data)
        assert best_ask == pytest.approx(100.00)

    def test_too_short_returns_none_none(self):
        best_bid, best_ask = wsc._parse_best5(b"\x00" * 10)
        assert best_bid is None
        assert best_ask is None

    def test_empty_data(self):
        best_bid, best_ask = wsc._parse_best5(b"")
        assert best_bid is None
        assert best_ask is None


# ══════════════════════════════════════════════════════════════════════════════
# _parse_frame
# ══════════════════════════════════════════════════════════════════════════════

class TestParseFrame:
    def test_mode3_full_frame(self):
        frame = _build_mode3_frame("3045", ltp_paise=100050, volume=50000)
        result = wsc._parse_frame(frame)
        assert result is not None
        token_str, ltp, ts, volume, best_bid, best_ask = result
        assert token_str == "3045"
        assert ltp == pytest.approx(1000.50)
        assert volume == 50000

    def test_ltp_paise_division(self):
        frame = _build_mode3_frame("1234", ltp_paise=219905)
        result = wsc._parse_frame(frame)
        assert result[1] == pytest.approx(2199.05)

    def test_frame_too_short_returns_none(self):
        assert wsc._parse_frame(b"\x00" * 50) is None

    def test_zero_ltp_returns_none(self):
        frame = _build_mode3_frame("3045", ltp_paise=0)
        assert wsc._parse_frame(frame) is None

    def test_negative_ltp_returns_none(self):
        frame = _build_mode3_frame("3045", ltp_paise=-100)
        assert wsc._parse_frame(frame) is None

    def test_negative_volume_clamped_to_zero(self):
        frame = _build_mode3_frame("3045", ltp_paise=10000, volume=0)
        # Manually corrupt volume bytes to be negative
        vol_offset = 67
        bad = bytearray(frame)
        struct.pack_into("<q", bad, vol_offset, -999)
        result = wsc._parse_frame(bytes(bad))
        assert result is not None
        assert result[3] == 0  # volume clamped

    def test_mode1_frame_no_volume_no_depth(self):
        # 51-byte frame only — no volume, no depth
        token_bytes = b"3045" + b"\x00" * 21
        frame = (
            bytes([1, 1]) + token_bytes +
            struct.pack("<q", 0) +            # sequence
            struct.pack("<q", 0) +            # feed_time
            struct.pack("<q", 50000)          # LTP = ₹500
        )
        assert len(frame) == 51
        result = wsc._parse_frame(frame)
        assert result is not None
        assert result[1] == pytest.approx(500.00)
        assert result[3] == 0      # volume = 0
        assert result[4] is None   # best_bid
        assert result[5] is None   # best_ask

    def test_depth_extracted_in_full_frame(self):
        ask_pkt = _build_depth_packet(flag=0, price_paise=100100)
        bid_pkt = _build_depth_packet(flag=1, price_paise=100000)
        depth = ask_pkt + bid_pkt + b"\x00" * (8 * 20)
        frame = _build_mode3_frame("3045", ltp_paise=100050, depth_packets=depth)
        result = wsc._parse_frame(frame)
        best_bid, best_ask = result[4], result[5]
        assert best_bid == pytest.approx(1000.00)
        assert best_ask == pytest.approx(1001.00)

    def test_token_stripped_of_nulls(self):
        frame = _build_mode3_frame("3045", ltp_paise=10000)
        result = wsc._parse_frame(frame)
        assert result[0] == "3045"

    def test_empty_bytes_returns_none(self):
        assert wsc._parse_frame(b"") is None


# ══════════════════════════════════════════════════════════════════════════════
# _build_subscribe_msg / _build_reverse_map
# ══════════════════════════════════════════════════════════════════════════════

class TestSubscribeAndMap:
    def test_subscribe_structure(self):
        msg = json.loads(wsc._build_subscribe_msg(["3045", "2885"]))
        assert msg["action"] == 1
        assert msg["params"]["mode"] == 3
        tl = msg["params"]["tokenList"]
        assert len(tl) == 1
        assert tl[0]["exchangeType"] == 1
        assert "3045" in tl[0]["tokens"]

    def test_unsubscribe_action(self):
        msg = json.loads(wsc._build_subscribe_msg(["3045"], action=0))
        assert msg["action"] == 0

    def test_build_reverse_map(self):
        _reset_state()
        wsc._build_reverse_map({"SBIN": "3045", "RELIANCE": "2885"})
        assert wsc._token_to_symbol == {"3045": "SBIN", "2885": "RELIANCE"}


# ══════════════════════════════════════════════════════════════════════════════
# get_tick_buffer / get_last_ltp / get_last_volume / get_best_bid_ask
# ══════════════════════════════════════════════════════════════════════════════

class TestAccessors:
    def setup_method(self):
        _reset_state()

    def test_get_tick_buffer_empty(self):
        assert wsc.get_tick_buffer("SBIN") == []

    def test_get_tick_buffer_returns_list_copy(self):
        now = time.time()
        wsc._tick_buffers["SBIN"].append((now, 1000.0))
        result = wsc.get_tick_buffer("SBIN")
        assert result == [(now, 1000.0)]
        # mutating result must not affect internal deque
        result.append((now + 1, 999.0))
        assert len(wsc._tick_buffers["SBIN"]) == 1

    def test_get_last_ltp_none_when_empty(self):
        assert wsc.get_last_ltp("MISSING") is None

    def test_get_last_ltp_returns_last(self):
        now = time.time()
        wsc._tick_buffers["INFY"].append((now - 1, 1500.0))
        wsc._tick_buffers["INFY"].append((now, 1510.0))
        assert wsc.get_last_ltp("INFY") == pytest.approx(1510.0)

    def test_get_last_volume_zero_when_absent(self):
        assert wsc.get_last_volume("UNKNOWN") == 0

    def test_get_last_volume_returns_stored(self):
        wsc._last_volume["RELIANCE"] = 12345
        assert wsc.get_last_volume("RELIANCE") == 12345

    def test_get_best_bid_ask_none_when_absent(self):
        assert wsc.get_best_bid_ask("NOQUOTE") is None

    def test_get_best_bid_ask_returns_pair(self):
        now = time.time()
        wsc._last_quote["SBIN"] = (999.50, 1000.50, now)
        bid, ask = wsc.get_best_bid_ask("SBIN")
        assert bid == pytest.approx(999.50)
        assert ask == pytest.approx(1000.50)

    def test_register_on_tick_callback(self):
        received = []
        wsc.register_on_tick(lambda sym, ltp, vol, ts: received.append((sym, ltp)))
        # fire manually
        for cb in wsc._on_tick_callbacks:
            cb("SBIN", 1000.0, 0, time.time())
        assert received == [("SBIN", 1000.0)]


# ══════════════════════════════════════════════════════════════════════════════
# Time-bounded buffer pruning (_MAX_BUFFER_AGE_S fix)
# ══════════════════════════════════════════════════════════════════════════════

class TestBufferPruning:
    """Simulate the time-bounded prune that _ws_loop does on every tick append.
    We replicate the exact pruning logic here to confirm the math, independent
    of a running asyncio loop."""

    def _apply_append_and_prune(self, buf: deque, ts: float, ltp: float):
        buf.append((ts, ltp))
        cutoff = ts - wsc._MAX_BUFFER_AGE_S
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def test_old_ticks_pruned(self):
        buf = deque(maxlen=wsc._MAX_TICKS)
        now = time.time()
        old_ts = now - wsc._MAX_BUFFER_AGE_S - 10  # definitely stale
        buf.append((old_ts, 900.0))

        self._apply_append_and_prune(buf, now, 1000.0)
        # old tick should be gone
        assert buf[0][0] == now

    def test_recent_ticks_kept(self):
        buf = deque(maxlen=wsc._MAX_TICKS)
        now = time.time()
        recent_ts = now - 60  # 1 minute ago — well within 65-minute window
        buf.append((recent_ts, 900.0))

        self._apply_append_and_prune(buf, now, 1000.0)
        assert len(buf) == 2  # both kept

    def test_max_buffer_age_is_65_minutes(self):
        assert wsc._MAX_BUFFER_AGE_S == 65 * 60

    def test_max_ticks_backstop(self):
        assert wsc._MAX_TICKS == 50_000


# ══════════════════════════════════════════════════════════════════════════════
# ws_status
# ══════════════════════════════════════════════════════════════════════════════

class TestWsStatus:
    def setup_method(self):
        _reset_state()

    def test_initial_status(self):
        s = wsc.ws_status()
        assert s["running"] is False
        assert s["connected"] is False
        assert s["subscribed_symbols"] == 0
        assert s["task_done"] is True
        assert s["reconnect_attempts"] == 0
        assert s["last_tick_at"] is None

    def test_connected_status(self):
        wsc._connected = True
        wsc._running = True
        wsc._reconnect_attempts = 3
        s = wsc.ws_status()
        assert s["connected"] is True
        assert s["running"] is True
        assert s["reconnect_attempts"] == 3

    def test_last_tick_at_iso_format(self):
        wsc._last_tick_at = time.time()
        s = wsc.ws_status()
        assert s["last_tick_at"] is not None
        # Should be a valid ISO8601 string
        datetime.fromisoformat(s["last_tick_at"].replace("Z", "+00:00"))

    def test_subscribed_symbols_count(self):
        wsc._build_reverse_map({"A": "1", "B": "2", "C": "3"})
        s = wsc.ws_status()
        assert s["subscribed_symbols"] == 3

    def test_task_done_true_when_no_task(self):
        wsc._ws_task = None
        assert wsc.ws_status()["task_done"] is True

    def test_task_done_false_with_running_task(self):
        async def _dummy():
            await asyncio.sleep(999)

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(_dummy())
            wsc._ws_task = task
            assert wsc.ws_status()["task_done"] is False
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        finally:
            loop.close()
            wsc._ws_task = None


# ══════════════════════════════════════════════════════════════════════════════
# _redact_secrets (extra edge cases not covered by the redaction test file)
# ══════════════════════════════════════════════════════════════════════════════

class TestRedactSecretsEdgeCases:
    def test_non_string_input(self):
        result = wsc._redact_secrets(12345)
        assert result == "12345"

    def test_none_input(self):
        result = wsc._redact_secrets(None)
        assert result == "None"

    def test_multiple_params_in_url(self):
        url = "/path?clientCode=C1&feedToken=SECRET1&apiKey=SECRET2&other=ok"
        result = wsc._redact_secrets(url)
        assert "SECRET1" not in result
        assert "SECRET2" not in result
        assert "other=ok" in result

    def test_no_secrets_unchanged(self):
        text = "no secrets here at all"
        assert wsc._redact_secrets(text) == text
