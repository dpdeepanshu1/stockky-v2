"""group270: live poller slices, live_quotes writer, shadow comparison, and the (opt-in) websocket packet layer.

Run from services/market-data-service:   python3 -m pytest tests/test_group270_dhan_live_and_ws.py -v
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dhan_data import config, live_poller, live_store, quotes, scrip_master, ws_feed


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DHAN_") or k in ("QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    quotes._reset_for_tests()
    ws_feed._reset_for_tests()
    scrip_master._set_for_tests({}, {})
    yield
    quotes._reset_for_tests()
    ws_feed._reset_for_tests()


# ── poller ─────────────────────────────────────────────────────────────────────────────────────────────────────
class TestSlices:
    def test_small_universe_is_returned_whole(self):
        assert live_poller.next_slice(["A", "B"], 0, 10) == (["A", "B"], 0)

    def test_empty(self):
        assert live_poller.next_slice([], 5, 10) == ([], 0)

    def test_rotation_covers_everything_and_wraps(self):
        u = [f"S{i}" for i in range(10)]
        seen, off = [], 0
        for _ in range(3):
            chunk, off = live_poller.next_slice(u, off, 4)
            assert len(chunk) == 4
            seen += chunk
        assert set(seen) == set(u)
        chunk, off = live_poller.next_slice(u, 8, 4)
        assert chunk == ["S8", "S9", "S0", "S1"] and off == 2


# ── live_quotes writer ─────────────────────────────────────────────────────────────────────────────────────────
ROW = {"symbol": "TCS", "price": 3500.5, "previous_close": 3480.0, "open": 3490.0, "day_high": 3510.0,
       "day_low": 3470.0, "volume": 12345}


class TestLiveStore:
    def test_params_shape_matches_angelone_writer(self):
        p = live_store.build_params([ROW])[0]
        assert p["s"] == "TCS" and p["l"] == 3500.5 and p["v"] == 12345
        o = json.loads(p["o"])
        assert o["close"] == 3480.0 and o["open"] == 3490.0 and o["high"] == 3510.0 and o["low"] == 3470.0

    def test_missing_prev_close_stores_ltp_like_angelone_does(self):
        o = json.loads(live_store.build_params([{**ROW, "previous_close": None}])[0]["o"])
        assert o["close"] == 3500.5          # real-trade-service's _lq_prev_close reads close==ltp as "unknown"

    def test_indices_and_junk_rows_are_not_written(self):
        assert live_store.build_params([{"symbol": "^NSEI", "price": 24000}, {"symbol": "", "price": 1},
                                        {"symbol": "X", "price": 0}, {"symbol": "Y"}]) == []

    @pytest.mark.parametrize("order,mode,writes", [
        ("dhan,angelone,yfinance", "auto", True),
        ("angelone,dhan,yfinance", "auto", False),
        ("angelone,yfinance,dhan", "auto", False),
        ("angelone,yfinance,dhan", "on", True),
        ("dhan,angelone,yfinance", "off", False),
        ("dhan,angelone,yfinance", "garbage", True),
    ])
    def test_write_rule(self, monkeypatch, order, mode, writes):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", order)
        monkeypatch.setenv("DHAN_LIVE_WRITE", mode)
        assert live_store.should_write() is writes

    def test_submit_is_a_noop_when_not_writing(self, monkeypatch):
        monkeypatch.setenv("QUOTE_PROVIDER_ORDER", "angelone,dhan,yfinance")
        called = []
        monkeypatch.setattr(live_store, "_write_sync", lambda rows: called.append(rows) or 0)
        live_store.submit([ROW])
        time.sleep(0.1)
        assert called == []

    def test_submit_writes_on_background_thread(self, monkeypatch):
        called = []
        monkeypatch.setattr(live_store, "_write_sync", lambda rows: called.append(rows) or len(rows))
        live_store.submit([ROW])
        deadline = time.time() + 3
        while not called and time.time() < deadline:
            time.sleep(0.02)
        assert called and called[0][0]["symbol"] == "TCS"

    def test_write_sync_uses_dhan_source_and_executemany(self, monkeypatch):
        from sqlalchemy import create_engine, event, text
        eng = create_engine("sqlite://")

        @event.listens_for(eng, "connect")          # registered BEFORE the first connection; sqlite has no now()
        def _fn(dbapi, rec):
            dbapi.create_function("now", 0, lambda: "2026-10-09 10:00:00")
        with eng.begin() as c:
            c.execute(text("CREATE TABLE live_quotes (symbol TEXT PRIMARY KEY, ltp NUMERIC, ohlc_json TEXT, "
                           "volume BIGINT, source TEXT, updated_at TIMESTAMP)"))
        import angelone_ws_feed
        monkeypatch.setattr(angelone_ws_feed, "_get_live_quotes_engine", lambda: (eng, "postgresql"))
        monkeypatch.setattr(angelone_ws_feed, "_ensure_schema", lambda e, d: None)
        n = live_store._write_sync([ROW, {**ROW, "symbol": "INFY", "price": 1500.0}])
        assert n == 2
        with eng.connect() as c:
            rows = c.execute(text("SELECT symbol, ltp, source FROM live_quotes ORDER BY symbol")).fetchall()
        assert [(r[0], float(r[1]), r[2]) for r in rows] == [("INFY", 1500.0, "dhan"), ("TCS", 3500.5, "dhan")]
        live_store._write_sync([{**ROW, "price": 3600.0}])           # upsert, not duplicate
        with eng.connect() as c:
            assert float(c.execute(text("SELECT ltp FROM live_quotes WHERE symbol='TCS'")).scalar()) == 3600.0
            assert c.execute(text("SELECT COUNT(*) FROM live_quotes")).scalar() == 2


class TestShadowCompare:
    def test_counts_and_flags_differences(self, monkeypatch, caplog):
        import angelone_ws_feed
        prices = {"A": 100.0, "B": 100.0}
        monkeypatch.setattr(angelone_ws_feed, "get_live_quote", lambda s: {"price": prices.get(s)})
        live_poller._cmp.update(compared=0, over_half_pct=0, max_diff_pct=0.0, sum_diff_pct=0.0)
        with caplog.at_level("WARNING", logger="dhan-data.poller"):
            live_poller._compare([{"symbol": "A", "price": 100.1}, {"symbol": "B", "price": 101.0},
                                  {"symbol": "C", "price": 5.0}])
        st = live_poller.status()["shadow"]
        assert st["compared"] == 2 and st["over_half_pct"] == 1 and st["max_diff_pct"] == pytest.approx(1.0, abs=0.01)
        assert any("B dhan=101.00 angelone=100.00" in r.message for r in caplog.records)


# ── websocket packets (layout per Dhan v2 docs - VERIFY with a live capture) ───────────────────────────────────
def _hdr(code, seg, sid, body_len):
    return struct.pack("<BHBI", code, 8 + body_len, seg, sid)


def _quote_packet(sid=2885, ltp=2900.5, vol=1000000):
    body = struct.pack("<fhifiiiffff", ltp, 10, 1760000000, 2895.0, vol, 500, 700, 2880.0, 2870.0, 2910.0, 2875.0)
    return _hdr(4, 1, sid, len(body)) + body


class TestPackets:
    def test_quote_packet_is_50_bytes_and_parses(self):
        pkt = _quote_packet()
        assert len(pkt) == 50
        p = ws_feed.parse_packet(pkt)
        assert p["kind"] == "quote" and p["segment"] == "NSE_EQ" and p["security_id"] == 2885
        assert p["price"] == pytest.approx(2900.5) and p["volume"] == 1000000
        assert p["open"] == pytest.approx(2880.0) and p["high"] == pytest.approx(2910.0) and p["low"] == pytest.approx(2875.0)

    def test_ticker_packet_is_16_bytes(self):
        body = struct.pack("<fi", 101.25, 1760000000)
        pkt = _hdr(2, 1, 11536, len(body)) + body
        assert len(pkt) == 16
        p = ws_feed.parse_packet(pkt)
        assert p["kind"] == "ticker" and p["price"] == pytest.approx(101.25)

    def test_prev_close_packet(self):
        body = struct.pack("<fi", 2870.0, 0)
        p = ws_feed.parse_packet(_hdr(6, 1, 2885, len(body)) + body)
        assert p["kind"] == "prev_close" and p["prev_close"] == pytest.approx(2870.0)

    def test_index_segment_code(self):
        body = struct.pack("<fi", 24000.0, 1760000000)
        assert ws_feed.parse_packet(_hdr(2, 0, 13, len(body)) + body)["segment"] == "IDX_I"

    def test_disconnect_packet(self):
        body = struct.pack("<h", 807)
        p = ws_feed.parse_packet(_hdr(50, 1, 0, len(body)) + body)
        assert p["kind"] == "disconnect" and p["reason"] == 807

    def test_short_or_garbage_packets_are_none_not_exceptions(self):
        assert ws_feed.parse_packet(b"") is None and ws_feed.parse_packet(b"\x01\x02") is None
        assert ws_feed.parse_packet("text") is None
        short = _hdr(4, 1, 1, 10) + b"\x00" * 10               # quote header but truncated body
        assert ws_feed.parse_packet(short)["kind"] == "unknown"

    def test_unknown_code_is_reported_not_guessed(self):
        p = ws_feed.parse_packet(_hdr(99, 1, 5, 0))
        assert p == {"kind": "unknown", "code": 99, "segment": "NSE_EQ", "security_id": 5}


class TestSubscribe:
    def test_chunks_of_100(self):
        inst = [("NSE_EQ", i) for i in range(250)]
        msgs = ws_feed.build_subscribe_messages(inst)
        assert len(msgs) == 3
        first = json.loads(msgs[0])
        assert first["RequestCode"] == 17 and first["InstrumentCount"] == 100
        assert first["InstrumentList"][0] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "0"}
        assert json.loads(msgs[2])["InstrumentCount"] == 50

    def test_ticker_mode_code(self):
        assert json.loads(ws_feed.build_subscribe_messages([("NSE_EQ", 1)], ws_feed.REQ_SUBSCRIBE_TICKER)[0])["RequestCode"] == 15


class TestStateMerge:
    REV = {("NSE_EQ", 2885): "RELIANCE", ("IDX_I", 13): "^NSEI"}

    def test_quote_then_prev_close_builds_a_row(self):
        assert ws_feed.apply_packet(ws_feed.parse_packet(_quote_packet()), self.REV) == "RELIANCE"
        body = struct.pack("<fi", 2870.0, 0)
        ws_feed.apply_packet(ws_feed.parse_packet(_hdr(6, 1, 2885, len(body)) + body), self.REV)
        row = ws_feed.snapshot_rows({"RELIANCE"})[0]
        assert row["price"] == pytest.approx(2900.5) and row["previous_close"] == pytest.approx(2870.0)
        assert row["day_change_pct"] == pytest.approx(round((2900.5 - 2870.0) / 2870.0 * 100, 2), abs=0.01)
        assert row["source"] == "dhan_ws" and row["volume"] == 1000000

    def test_day_close_field_is_never_used_as_previous_close(self):
        ws_feed.apply_packet(ws_feed.parse_packet(_quote_packet()), self.REV)
        assert ws_feed.snapshot_rows({"RELIANCE"})[0]["previous_close"] is None   # R3: only the prev-close packet counts

    def test_unknown_instrument_ignored(self):
        assert ws_feed.apply_packet(ws_feed.parse_packet(_quote_packet(sid=424242)), self.REV) is None

    @pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
    def test_bad_prices_ignored(self, price):
        assert ws_feed.apply_packet(ws_feed.parse_packet(_quote_packet(ltp=price)), self.REV) is None

    def test_rows_feed_the_rest_cache(self):
        ws_feed.apply_packet(ws_feed.parse_packet(_quote_packet()), self.REV)
        quotes.inject_rows(ws_feed.snapshot_rows({"RELIANCE"}))
        assert quotes.peek("RELIANCE")["price"] == pytest.approx(2900.5)

    def test_disabled_by_default(self):
        assert config.ws_enabled() is False
        ws_feed.start(lambda: ["TCS"])
        assert ws_feed._thread is None or not ws_feed._thread.is_alive()
