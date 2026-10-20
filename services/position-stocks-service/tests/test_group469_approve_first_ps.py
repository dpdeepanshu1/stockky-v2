"""group469: position-stocks approve-first (orders/approve_first.py): the same state machine plus the JSON-file storage."""
from __future__ import annotations

import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("g469_ps_approve_first", os.path.join(_ROOT, "orders", "approve_first.py"))
af = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(af)

FP = "abc123"


def _state(n=2, now=1000.0):
    return af.sync(None, FP, n, now)


def test_fingerprint_tracks_gate_settings_only():
    a = af.fingerprint({"STRUCTURE_MIN_RR": "1", "OTHER": "x", "DHAN_ACCESS_TOKEN": "t1", "STRUCTURE_API_KEY": "k1"})
    assert a == af.fingerprint({"STRUCTURE_MIN_RR": "1", "OTHER": "y", "DHAN_ACCESS_TOKEN": "t2", "STRUCTURE_API_KEY": "k2"})
    assert af.fingerprint({"ENTRY_X": "1"}) != af.fingerprint({"ENTRY_X": "2"}) and af.fingerprint({"BOARD_Y": "1"}) != af.fingerprint({"BOARD_Y": "2"})
    assert af.fingerprint({"SCAN_INTERVAL": "1"}) == af.fingerprint({"SCAN_INTERVAL": "2"})            # operational names are not gate settings   # secrets / unrelated ignored
    assert a != af.fingerprint({"STRUCTURE_MIN_RR": "2"})
    assert af.fingerprint({"ENTRY_X": "1", "APPROVE_FIRST_N": "3"}) == af.fingerprint({"ENTRY_X": "1", "APPROVE_FIRST_N": "9"})   # its own knobs never reset it
    assert len(a) == 12


def test_n_required_parsing(monkeypatch=None):
    for raw, want in (("", 0), ("0", 0), ("3", 3), ("-2", 0), ("99", 20), ("x", 0)):
        os.environ["APPROVE_FIRST_N"] = raw
        assert af.n_required() == want
    os.environ.pop("APPROVE_FIRST_N")
    assert af.n_required() == 0 and not af.active()
    os.environ["APPROVE_FIRST_N"] = "2"
    try:
        assert af.active()
    finally:
        os.environ.pop("APPROVE_FIRST_N")


def test_sync_resets_on_change_and_prunes_expired():
    s = _state(2)
    af.decide(s, "AAA", {"qty": 1}, 1000.0)
    af.approve(s, "AAA", 1000.0)
    same = af.sync(s, FP, 2, 1001.0)
    assert same["remaining"] == 2 and "AAA" in same["approved"]                    # same fingerprint: kept
    changed = af.sync(s, "other", 2, 1001.0)
    assert changed["remaining"] == 2 and changed["approved"] == {} and changed["pending"] == {}   # new fingerprint: fresh round
    assert af.sync(s, FP, 5, 1001.0)["remaining"] == 5                              # N changed: fresh round
    later = af.sync(s, FP, 2, 1000.0 + af.ttl_s() + 1)
    assert later["approved"] == {}                                                  # expired approval pruned


def test_hold_then_approve_then_consume_flow():
    s = _state(2)
    r = af.decide(s, "aaa", {"qty": 5, "price": 100.0}, 1000.0)
    assert r["allowed"] is False and r["new_pending"] is True and r["remaining"] == 2
    again = af.decide(s, "AAA", {"qty": 5, "price": 101.0}, 1010.0)
    assert again["allowed"] is False and again["new_pending"] is False             # one alert per symbol
    assert s["pending"]["AAA"]["price"] == 101.0 and s["pending"]["AAA"]["since"] == 1000.0
    assert af.approve(s, "AAA", 1020.0) is True
    ok = af.decide(s, "AAA", {}, 1030.0)
    assert ok["allowed"] is True and s["remaining"] == 2                           # approved, but NOT used up until the BUY is sent
    assert af.consume(s, "AAA", 1040.0, {"qty": 5}) is True
    assert s["remaining"] == 1 and s["history"][-1]["symbol"] == "AAA" and "AAA" not in s["approved"]
    assert af.consume(s, "AAA", 1041.0) is False                                    # cannot be used up twice


def test_after_n_entries_the_gate_goes_quiet():
    s = _state(1)
    af.decide(s, "AAA", {}, 1000.0)
    af.approve(s, "AAA", 1001.0)
    af.consume(s, "AAA", 1002.0)
    assert s["remaining"] == 0
    r = af.decide(s, "BBB", {}, 1003.0)
    assert r["allowed"] is True and "used up" in r["reason"]


def test_reject_blocks_for_the_window_and_unknown_symbols_are_refused():
    s = _state(2)
    af.decide(s, "AAA", {}, 1000.0)
    assert af.reject(s, "AAA", 1001.0) is True
    blocked = af.decide(s, "AAA", {}, 1002.0)
    assert blocked["allowed"] is False and "rejected" in blocked["reason"] and "AAA" not in s["pending"]
    assert af.approve(s, "ZZZ", 1003.0) is False and af.reject(s, "ZZZ", 1003.0) is False
    assert af.decide(s, "AAA", {}, 1001.0 + af.ttl_s() - 1)["allowed"] is False
    s2 = af.sync(s, FP, 2, 1001.0 + af.ttl_s() + 1)                                 # window over: can be asked again
    assert af.decide(s2, "AAA", {}, 1001.0 + af.ttl_s() + 2)["new_pending"] is True



def _tmpdir():
    import tempfile
    d = tempfile.mkdtemp()
    os.environ["CONTROL_DIR"] = d
    af._mem.clear()
    return d


def test_file_storage_end_to_end_survives_a_reload_and_is_off_by_default():
    d = _tmpdir()
    os.environ.pop("APPROVE_FIRST_N", None)
    assert af.gate("AAA")["allowed"] is True and not os.path.exists(os.path.join(d, "approve_first_ps.json"))   # off: nothing written
    os.environ["APPROVE_FIRST_N"] = "1"
    try:
        g = af.gate("AAA", {"price": 50.0})
        assert g["allowed"] is False and g["new_pending"] is True and os.path.exists(os.path.join(d, "approve_first_ps.json"))
        af._mem.clear()                                                          # a restart: only the file is left
        assert af.status()["pending"][0]["symbol"] == "AAA"
        assert af.gate("AAA")["new_pending"] is False                            # still one alert per symbol after the reload
        assert af.do_approve("AAA")["ok"] is True and af.gate("AAA")["allowed"] is True
        af.mark_entered("AAA", {"qty": 3})
        assert af.status()["remaining"] == 0 and af.gate("BBB")["allowed"] is True
        assert af.do_reset()["remaining"] == 1 and af.do_approve("NOPE")["ok"] is False and af.do_reject("NOPE")["ok"] is False
        os.environ["ENTRY_MIN_PRICE"] = "9"                                      # a settings change starts a new round on its own
        try:
            assert af.gate("CCC")["allowed"] is False
        finally:
            os.environ.pop("ENTRY_MIN_PRICE")
    finally:
        os.environ.pop("APPROVE_FIRST_N", None)


def test_unwritable_directory_keeps_state_in_memory_and_a_corrupt_file_fails_open_to_memory():
    _tmpdir()
    os.environ["CONTROL_DIR"] = "/proc/nope/never"                              # cannot be created
    os.environ["APPROVE_FIRST_N"] = "1"
    try:
        assert af.gate("AAA")["allowed"] is False                                # held, kept in memory
        assert af.status()["pending"][0]["symbol"] == "AAA"
        d = _tmpdir()
        with open(os.path.join(d, "approve_first_ps.json"), "w") as fh:
            fh.write("{not json")
        assert af.gate("ZZZ")["allowed"] is False                                # corrupt file: a fresh round, never an exception
    finally:
        os.environ.pop("APPROVE_FIRST_N", None)
        os.environ.pop("CONTROL_DIR", None)
