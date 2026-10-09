"""group286: Dhan 20-level depth - packet parsing, book maths, on-demand watch list, public answer (no network).
Run from services/market-data-service:   python3 -m pytest tests/test_group286_depth20.py -q"""
from __future__ import annotations

import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dhan_data import client, config, depth20, scrip_master


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DHAN_") or k in ("QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.setenv("DHAN_DEPTH20_ENABLED", "1")
    monkeypatch.setattr(client, "available", lambda: True)
    depth20._reset_for_tests()
    scrip_master._set_for_tests({"ABC": 11, "XYZ": 22, "S0": 100, "S1": 101, "S2": 102}, {"NIFTY": 13})
    yield
    depth20._reset_for_tests()


def packet(code, seg, sid, levels, seq=1):
    body = b"".join(struct.pack("<dII", p, q, o) for p, q, o in levels)
    body += b"\x00" * (16 * (20 - len(levels)))
    return struct.pack("<HBBII", depth20.PACKET_SIZE, code, seg, sid, seq) + body


BIDS = [(99.9, 100, 3), (99.8, 200, 4), (99.5, 300, 5)]
ASKS = [(100.1, 50, 2), (100.2, 150, 3), (100.6, 400, 6)]


class TestParsing:
    def test_sizes(self):
        assert depth20.HEADER.size == 12 and depth20.PACKET_SIZE == 332

    def test_bid_packet(self):
        p = depth20.parse_side(packet(41, 1, 11, BIDS))
        assert p["kind"] == "bid" and p["segment"] == "NSE_EQ" and p["security_id"] == 11
        assert p["levels"] == BIDS            # empty slots dropped

    def test_ask_packet(self):
        assert depth20.parse_side(packet(51, 1, 11, ASKS))["kind"] == "ask"

    def test_other_code_or_short_is_none(self):
        assert depth20.parse_side(packet(2, 1, 11, BIDS)) is None
        assert depth20.parse_side(packet(41, 1, 11, BIDS)[:100]) is None

    def test_bad_values_dropped(self):
        p = depth20.parse_side(packet(41, 1, 11, [(float("nan"), 5, 1), (0.0, 5, 1), (99.0, 0, 1), (98.0, 7, 1)]))
        assert p["levels"] == [(98.0, 7, 1)]

    def test_stacked_message(self):
        msg = packet(41, 1, 11, BIDS) + packet(51, 1, 11, ASKS) + packet(41, 1, 22, BIDS)
        out = depth20.parse_message(msg)
        assert [x["kind"] for x in out] == ["bid", "ask", "bid"] and out[2]["security_id"] == 22

    def test_truncated_tail_is_one_bad(self):
        out = depth20.parse_message(packet(41, 1, 11, BIDS) + packet(51, 1, 11, ASKS)[:50])
        assert [x["kind"] for x in out] == ["bid", "bad"]

    def test_disconnect_reason(self):
        msg = struct.pack("<HBBII", 14, 50, 0, 0, 0) + struct.pack("<h", 807)
        assert depth20.parse_message(msg) == [{"kind": "disconnect", "reason": 807, "segment": "IDX_I", "security_id": 0}]

    def test_unknown_code_skipped_by_its_own_length(self):
        unk = struct.pack("<HBBII", 20, 99, 1, 11, 0) + b"\x00" * 8
        out = depth20.parse_message(unk + packet(41, 1, 11, BIDS))
        assert [x["kind"] for x in out] == ["unknown", "bid"] and out[0]["code"] == 99

    def test_unknown_code_with_silly_length_stops(self):
        out = depth20.parse_message(struct.pack("<HBBII", 0, 99, 1, 11, 0) + packet(41, 1, 11, BIDS))
        assert [x["kind"] for x in out] == ["unknown"]

    def test_non_bytes_and_empty(self):
        assert depth20.parse_message("text") == [{"kind": "bad"}]
        assert depth20.parse_message(b"") == []


class TestSubscribeMessages:
    def test_chunks_of_50(self):
        inst = [("NSE_EQ", i) for i in range(120)]
        msgs = [json.loads(m) for m in depth20.build_subscribe_messages(inst)]
        assert [m["InstrumentCount"] for m in msgs] == [50, 50, 20]
        assert msgs[0]["RequestCode"] == 23 and msgs[0]["InstrumentList"][0] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "0"}

    def test_unsubscribe_code(self):
        m = json.loads(depth20.build_subscribe_messages([("NSE_EQ", 1)], depth20.REQ_UNSUBSCRIBE_DEPTH)[0])
        assert m["RequestCode"] == 24

    def test_empty(self):
        assert depth20.build_subscribe_messages([]) == []


class TestSummarize:
    def test_basic_fields(self):
        s = depth20.summarize(BIDS, ASKS)
        assert s["best_bid"] == 99.9 and s["best_ask"] == 100.1
        assert s["spread_pct"] == pytest.approx(0.2, abs=0.001)
        assert s["bid_qty_20"] == 600 and s["ask_qty_20"] == 600 and s["imbalance_20"] == 0.0
        assert s["book_value_20"] == pytest.approx(s["bid_value_20"] + s["ask_value_20"])
        assert s["ask_depth_pct"] == pytest.approx((100.6 - 100.1) / 100.1 * 100, abs=0.001)

    def test_unsorted_input_is_sorted(self):
        s = depth20.summarize(list(reversed(BIDS)), list(reversed(ASKS)))
        assert s["best_bid"] == 99.9 and s["best_ask"] == 100.1

    @pytest.mark.parametrize("b,a", [([], ASKS), (BIDS, []), ([(101.0, 1, 1)], [(100.0, 1, 1)])])
    def test_unusable_book_is_none(self, b, a):
        assert depth20.summarize(b, a) is None

    def test_locked_book_is_kept_with_zero_spread(self):
        assert depth20.summarize([(100.0, 1, 1)], [(100.0, 1, 1)])["spread_pct"] == 0.0

    def test_buy_walk(self):
        s = depth20.summarize(BIDS, ASKS, qty=100)       # 50 @100.1 + 50 @100.2
        assert s["buy_complete"] and s["buy_avg_price"] == pytest.approx(100.15)
        assert s["buy_impact_pct"] == pytest.approx((100.15 - 100.1) / 100.1 * 100, abs=0.001)

    def test_sell_walk(self):
        s = depth20.summarize(BIDS, ASKS, qty=250)       # 100 @99.9 + 150 @99.8
        assert s["sell_complete"] and s["sell_avg_price"] == pytest.approx((9990 + 14970) / 250)
        assert s["sell_impact_pct"] > 0

    def test_qty_larger_than_book_is_incomplete(self):
        s = depth20.summarize(BIDS, ASKS, qty=5000)
        assert s["buy_complete"] is False and s["buy_filled_qty"] == 600

    def test_slip_window(self):
        s = depth20.summarize(BIDS, ASKS, slip_pct=0.2)   # asks up to 100.1*1.002 = 100.3002 -> 50+150
        assert s["buy_qty_within_slip"] == 200 and s["sell_qty_within_slip"] == 300

    def test_slip_zero_is_touch_only(self):
        s = depth20.summarize(BIDS, ASKS, slip_pct=0)
        assert s["buy_qty_within_slip"] == 50 and s["sell_qty_within_slip"] == 100

    def test_no_qty_no_walk_fields(self):
        s = depth20.summarize(BIDS, ASKS)
        assert "buy_avg_price" not in s and "buy_qty_within_slip" not in s


class TestWatchAndState:
    def test_watch_registers_equity(self):
        assert depth20.watch("ABC.NS") == "ok"
        assert "ABC" in depth20.status()["watching"]

    def test_index_and_unknown_unsupported(self):
        assert depth20.watch("^NSEI") == "unsupported"
        assert depth20.watch("NOPE") == "unsupported"
        assert depth20.status()["watching"] == []

    def test_apply_packet_needs_a_watched_symbol(self):
        assert depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": BIDS}) is None
        assert depth20.status()["unmatched"] == 1
        depth20.watch("ABC")
        assert depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": BIDS}) == "ABC"

    def test_lru_eviction(self, monkeypatch):
        monkeypatch.setenv("DHAN_DEPTH20_MAX_INSTRUMENTS", "2")
        for i, s in enumerate(["S0", "S1", "S2"]):
            depth20.watch(s, now_mono=float(i))
        assert depth20.status()["watching"] == ["S1", "S2"]
        # an evicted symbol's packets are no longer matched
        assert depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 100, "levels": BIDS}) is None

    def test_asking_again_refreshes_order(self, monkeypatch):
        monkeypatch.setenv("DHAN_DEPTH20_MAX_INSTRUMENTS", "2")
        depth20.watch("S0", now_mono=0.0); depth20.watch("S1", now_mono=1.0)
        depth20.watch("S0", now_mono=2.0)
        depth20.watch("S2", now_mono=3.0)
        assert depth20.status()["watching"] == ["S0", "S2"]

    def test_expire(self, monkeypatch):
        monkeypatch.setenv("DHAN_DEPTH20_TTL_S", "10")
        depth20.watch("S0", now_mono=0.0); depth20.watch("S1", now_mono=8.0)
        assert depth20.expire(now_mono=12.0) == ["S0"]
        assert depth20.status()["watching"] == ["S1"]

    def test_instrument_cap_never_exceeds_50(self, monkeypatch):
        monkeypatch.setenv("DHAN_DEPTH20_MAX_INSTRUMENTS", "500")
        assert depth20.status()["max_instruments"] == 50


class TestGet:
    def feed(self, sym="ABC", sid=11):
        depth20.watch(sym)
        depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": sid, "levels": BIDS})
        depth20.apply_packet({"kind": "ask", "segment": "NSE_EQ", "security_id": sid, "levels": ASKS})

    def test_available_book(self):
        self.feed()
        r = depth20.get("ABC", qty=100, slip_pct=0.2, wait_s=0)
        assert r["available"] is True and r["fresh"] and r["buy_qty_within_slip"] == 200 and r["buy_complete"]
        assert r["source"] == "dhan_depth20" and "_age_s" not in r

    def test_warming_when_no_book_yet(self):
        r = depth20.get("ABC", wait_s=0)
        assert r["available"] is False and r["reason"] == "warming"

    def test_half_a_book_is_warming(self):
        depth20.watch("ABC")
        depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": BIDS})
        assert depth20.get("ABC", wait_s=0)["reason"] == "warming"

    def test_stale(self, monkeypatch):
        self.feed()
        monkeypatch.setenv("DHAN_DEPTH20_STALE_S", "1")
        depth20._books["ABC"]["bid_at"] -= 100
        r = depth20.get("ABC", wait_s=0)
        assert r["available"] is False and r["reason"] == "stale" and r["fresh"] is False

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("DHAN_DEPTH20_ENABLED", "0")
        r = depth20.get("ABC", wait_s=0)
        assert r["available"] is False and r["reason"] == "depth20_disabled"
        assert depth20.status()["watching"] == []

    def test_dhan_disabled_and_unavailable(self, monkeypatch):
        monkeypatch.setattr(client, "available", lambda: False)
        assert depth20.get("ABC", wait_s=0)["reason"] == "dhan_unavailable"
        monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
        assert depth20.get("ABC", wait_s=0)["reason"] == "dhan_disabled"

    def test_unsupported_symbol(self):
        assert depth20.get("^NSEI", wait_s=0)["reason"] == "unsupported"

    def test_crossed_book_is_warming_not_a_crash(self):
        depth20.watch("ABC")
        depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": [(101.0, 5, 1)]})
        depth20.apply_packet({"kind": "ask", "segment": "NSE_EQ", "security_id": 11, "levels": [(100.0, 5, 1)]})
        assert depth20.get("ABC", wait_s=0)["available"] is False

    def test_never_raises(self, monkeypatch):
        monkeypatch.setattr(depth20, "watch", lambda k: (_ for _ in ()).throw(RuntimeError("boom")))
        r = depth20.get("ABC", wait_s=0)
        assert r["available"] is False and r["reason"] == "error:RuntimeError"

    def test_wait_picks_up_a_book_arriving_late(self):
        import threading, time
        depth20.watch("ABC")
        def late():
            time.sleep(0.15)
            self.feed()
        threading.Thread(target=late).start()
        assert depth20.get("ABC", wait_s=2.0)["available"] is True


def test_status_is_in_dhan_status_and_default_off(monkeypatch):
    monkeypatch.delenv("DHAN_DEPTH20_ENABLED")
    assert config.depth20_enabled() is False
    import dhan_data
    assert "depth20" in dhan_data.status()
