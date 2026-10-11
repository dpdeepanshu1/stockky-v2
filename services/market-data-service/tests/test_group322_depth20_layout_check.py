"""group322: depth20 layout self-check - insane / crossed books are dropped, frame samples and verdict are reported."""
from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dhan_data import client, depth20, scrip_master


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("DHAN_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DHAN_DATA_ENABLED", "1")
    monkeypatch.setenv("DHAN_DEPTH20_ENABLED", "1")
    monkeypatch.setattr(client, "available", lambda: True)
    depth20._reset_for_tests()
    scrip_master._set_for_tests({"ABC": 11}, {"NIFTY": 13})
    yield
    depth20._reset_for_tests()


def test_levels_sane_rules():
    assert depth20.levels_sane([(100, 1, 1), (99, 1, 1)], "bid")
    assert not depth20.levels_sane([(99, 1, 1), (100, 1, 1)], "bid")        # bids rising
    assert depth20.levels_sane([(100, 1, 1), (101, 1, 1)], "ask")
    assert not depth20.levels_sane([(101, 1, 1), (100, 1, 1)], "ask")       # asks falling
    assert not depth20.levels_sane([(float("nan"), 1, 1)], "bid")
    assert not depth20.levels_sane([(-1, 1, 1)], "ask")
    assert depth20.levels_sane([], "bid")
    assert not depth20.levels_sane(None or [("x", 1, 1)], "bid")


def test_insane_packet_is_dropped_and_counted():
    depth20.watch("ABC")
    pkt = {"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": [(99.0, 1, 1), (100.0, 1, 1)]}
    assert depth20.apply_packet(pkt) is None
    assert depth20.status()["suspect_packets"] == 1
    assert depth20.get("ABC", wait_s=0)["reason"] == "warming"


def test_crossed_book_is_not_served():
    depth20.watch("ABC")
    depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": [(101.0, 10, 1)]})
    depth20.apply_packet({"kind": "ask", "segment": "NSE_EQ", "security_id": 11, "levels": [(100.0, 10, 1)]})
    out = depth20.get("ABC", wait_s=0)
    assert out["available"] is False and out["reason"] == "warming"
    assert depth20.status()["crossed_books"] >= 1


def test_good_book_still_served():
    depth20.watch("ABC")
    depth20.apply_packet({"kind": "bid", "segment": "NSE_EQ", "security_id": 11, "levels": [(99.9, 10, 1), (99.8, 5, 1)]})
    depth20.apply_packet({"kind": "ask", "segment": "NSE_EQ", "security_id": 11, "levels": [(100.1, 10, 1)]})
    assert depth20.get("ABC", wait_s=0)["available"] is True


def test_frame_samples_keep_first_three_then_only_forced():
    for i in range(6):
        depth20.note_frame(bytes([i]) * 40)
    st = depth20.status()
    assert len(st["frame_samples"]) == 3
    assert st["frame_samples"][0]["head_hex"] == "00" * 24 and st["frame_samples"][0]["len"] == 40
    depth20.note_frame(b"\xff" * 10, "bad", force=True)
    tags = [f["tag"] for f in depth20.status()["frame_samples"]]
    assert tags[-1] == "bad"
    for _ in range(10):
        depth20.note_frame(b"\x01" * 10, "bad", force=True)
    assert len(depth20.status()["frame_samples"]) == 5


def test_verdict_values():
    assert depth20.layout_verdict({"messages": 0}) == "unverified"
    assert depth20.layout_verdict({"messages": 9, "bid_packets": 8, "ask_packets": 8, "suspect_packets": 0}) == "ok"
    assert depth20.layout_verdict({"messages": 9, "bid_packets": 1, "ask_packets": 1, "suspect_packets": 2,
                                   "bad_packets": 3}) == "suspect"
    assert depth20.layout_verdict({"messages": 4, "bid_packets": 0, "ask_packets": 0, "unknown_codes": {"7": 4}}) == "suspect"
    assert depth20.status()["layout_verdict"] == "unverified"
