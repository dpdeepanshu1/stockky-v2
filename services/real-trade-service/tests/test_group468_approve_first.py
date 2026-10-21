"""group468: approve-first mode (entry_engine/approve_first.py). Pure state machine + storage wrappers against a fake cache."""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("g468_approve_first", os.path.join(_ROOT, "entry_engine", "approve_first.py"))
af = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(af)

FP = "abc123"


def _state(n=2, now=1000.0):
    return af.sync(None, FP, n, now)


def test_fingerprint_tracks_gate_settings_only():
    a = af.fingerprint({"STRUCTURE_MIN_RR": "1", "OTHER": "x", "DHAN_ACCESS_TOKEN": "t1", "STRUCTURE_API_KEY": "k1"})
    assert a == af.fingerprint({"STRUCTURE_MIN_RR": "1", "OTHER": "y", "DHAN_ACCESS_TOKEN": "t2", "STRUCTURE_API_KEY": "k2"})   # secrets / unrelated ignored
    assert a != af.fingerprint({"STRUCTURE_MIN_RR": "2"})
    assert af.fingerprint({"ENTRY_X": "1", "APPROVE_FIRST_N": "3"}) == af.fingerprint({"ENTRY_X": "1", "APPROVE_FIRST_N": "9"})   # its own knobs never reset it
    assert len(a) == 12


def test_n_required_parsing(monkeypatch=None):
    for raw, want in (("", 0), ("0", 0), ("3", 3), ("-2", 0), ("99", 20), ("x", 0)):
        os.environ["APPROVE_FIRST_N"] = raw
        assert af.n_required() == want
    os.environ.pop("APPROVE_FIRST_N")
    assert af.n_required() == 0 and not af.active("REAL")
    os.environ["APPROVE_FIRST_N"] = "2"
    try:
        assert af.active("REAL") and not af.active("DEMO")
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


class _FakeCache:
    """Stands in for resilience.local_cache so the wrappers run without a database."""
    store: dict = {}

    @classmethod
    def load_snapshot(cls, db, key):
        return cls.store.get(key)

    @classmethod
    def save_snapshot(cls, db, key, payload):
        cls.store[key] = payload


_MISSING = object()
_SAVED_MODS = {}


def _install_fake_cache():
    # Remember what was there so _restore_real_cache() can put it back: replacing sys.modules for the rest of the
    # run broke every later test that imports resilience.local_cache (symbol_master, intraday_news, main startup).
    for name in ("resilience", "resilience.local_cache"):
        _SAVED_MODS.setdefault(name, sys.modules.get(name, _MISSING))
    pkg = types.ModuleType("resilience")
    mod = types.ModuleType("resilience.local_cache")
    mod.load_snapshot, mod.save_snapshot = _FakeCache.load_snapshot, _FakeCache.save_snapshot
    sys.modules["resilience"], sys.modules["resilience.local_cache"] = pkg, mod
    _FakeCache.store = {}


def _restore_real_cache():
    for name, mod in list(_SAVED_MODS.items()):
        if mod is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    _SAVED_MODS.clear()


@pytest.fixture(autouse=True)
def _real_cache_back_after_each_test():
    yield
    _restore_real_cache()


def test_wrappers_end_to_end_and_off_by_default():
    _install_fake_cache()
    os.environ.pop("APPROVE_FIRST_N", None)
    assert af.gate(None, "REAL", "AAA")["allowed"] is True and _FakeCache.store == {}     # off: no state touched
    os.environ["APPROVE_FIRST_N"] = "1"
    try:
        assert af.gate(None, "DEMO", "AAA")["allowed"] is True                            # DEMO never held
        g = af.gate(None, "REAL", "AAA", {"qty": 3, "price": 50.0})
        assert g["allowed"] is False and g["new_pending"] is True
        assert af.status(None, "REAL")["pending"][0]["symbol"] == "AAA"
        assert af.do_approve(None, "REAL", "AAA")["ok"] is True
        assert af.gate(None, "REAL", "AAA")["allowed"] is True
        af.mark_entered(None, "REAL", "AAA", {"qty": 3})
        st = af.status(None, "REAL")
        assert st["remaining"] == 0 and st["history"][0]["symbol"] == "AAA"
        assert af.gate(None, "REAL", "BBB")["allowed"] is True                            # round used up
        assert af.do_reset(None, "REAL")["remaining"] == 1                                # a manual reset starts a new round
        assert af.do_approve(None, "REAL", "NOPE")["ok"] is False
        os.environ["STRUCTURE_MIN_RR"] = "2.5"                                           # a settings change starts a new round on its own
        try:
            assert af.gate(None, "REAL", "CCC")["allowed"] is False
        finally:
            os.environ.pop("STRUCTURE_MIN_RR")
    finally:
        os.environ.pop("APPROVE_FIRST_N", None)


def test_gate_fails_open_when_storage_breaks():
    _install_fake_cache()
    sys.modules["resilience.local_cache"].load_snapshot = lambda db, key: (_ for _ in ()).throw(RuntimeError("db down"))
    os.environ["APPROVE_FIRST_N"] = "2"
    try:
        g = af.gate(None, "REAL", "AAA")
        assert g["allowed"] is True and "failed open" in g["reason"]
        af.mark_entered(None, "REAL", "AAA")                                              # must not raise
        assert "error" in af.status(None, "REAL")
    finally:
        os.environ.pop("APPROVE_FIRST_N", None)
