"""Group 292: orders/depth_gate refuses to judge depth on an old /quote book (age_s from market-data, group 289).

An old book is re-read once; if it is still older than ENTRY_DEPTH_MAX_QUOTE_AGE_S it counts as UNKNOWN depth (never
blocks, never sizes). No readable age follows ENTRY_DEPTH_QUOTE_AGE_UNKNOWN (allow by default)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

import config
from orders import depth_gate


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


@pytest.fixture
def md(monkeypatch):
    """Scripted market-data: st["bodies"] is the queue of /quote answers (the last one repeats)."""
    depth_gate._cache.clear()
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_SPREAD_PCT", 0.5)
    monkeypatch.setattr(config, "ENTRY_MIN_BOOK_VALUE", 50000.0)
    monkeypatch.setattr(config, "ENTRY_BOOK_MAX_SHARE_PCT", 10.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_QUOTE_AGE_S", 20.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", "allow")
    st = {"calls": 0, "bodies": [{"spread_pct": 0.9, "book_value_5": 1e6, "source": "dhan", "age_s": 1.0}]}

    class Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            i = min(st["calls"], len(st["bodies"]) - 1)
            st["calls"] += 1
            return Resp(200, dict(st["bodies"][i]))
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    yield st
    depth_gate._cache.clear()


# --- _quote_age_s ---------------------------------------------------------------------------------------------------

def test_age_s_is_read_as_given():
    assert depth_gate._quote_age_s({"age_s": 7.5}, "X") == 7.5


def test_time_spent_in_our_cache_is_added():
    body = {"age_s": 4.0}
    depth_gate._cache.clear()
    depth_gate._cache["X"] = (1000.0, body)
    assert depth_gate._quote_age_s(body, "X", now=1003.0) == 7.0
    depth_gate._cache.clear()


def test_cache_entry_of_a_different_body_adds_nothing():
    depth_gate._cache.clear()
    depth_gate._cache["X"] = (1000.0, {"age_s": 1.0})
    assert depth_gate._quote_age_s({"age_s": 4.0}, "X", now=1003.0) == 4.0
    depth_gate._cache.clear()


@pytest.mark.parametrize("bad", [None, "5", True, False, -1, float("nan"), float("inf"), 86400 * 31, [1]])
def test_unusable_age_s_is_unknown(bad):
    assert depth_gate._quote_age_s({"age_s": bad}, "X") is None


def test_as_of_is_the_fallback_when_age_s_is_missing():
    now = time.time()
    as_of = (datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=30)).isoformat()
    age = depth_gate._quote_age_s({"as_of": as_of}, "X", now=now)
    assert 29.9 < age < 30.1


def test_as_of_with_z_suffix():
    now = time.time()
    as_of = (datetime.fromtimestamp(now, timezone.utc) - timedelta(seconds=12)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    assert 11.0 < depth_gate._quote_age_s({"as_of": as_of}, "X", now=now) < 13.5


def test_naive_as_of_is_not_guessed():
    assert depth_gate._quote_age_s({"as_of": "2026-10-10T09:00:00"}, "X") is None


def test_future_as_of_is_age_zero():
    now = time.time()
    as_of = (datetime.fromtimestamp(now, timezone.utc) + timedelta(seconds=60)).isoformat()
    assert depth_gate._quote_age_s({"as_of": as_of}, "X", now=now) == 0.0


@pytest.mark.parametrize("body", [None, {}, "x", [], {"as_of": ""}, {"as_of": "garbage"}])
def test_no_age_info_is_unknown(body):
    assert depth_gate._quote_age_s(body, "X") is None


# --- reject_reason --------------------------------------------------------------------------------------------------

def test_fresh_book_is_judged(md):
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:0.90%")
    assert md["calls"] == 1


def test_old_book_is_reread_once_and_the_fresh_answer_is_used(md):
    md["bodies"] = [{"spread_pct": 0.9, "book_value_5": 1e6, "age_s": 60.0},
                    {"spread_pct": 0.9, "book_value_5": 1e6, "age_s": 2.0}]
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")
    assert md["calls"] == 2


def test_still_old_after_the_reread_means_unknown_depth_not_a_rejection(md):
    md["bodies"] = [{"spread_pct": 0.9, "book_value_5": 10.0, "age_s": 60.0}]      # would reject on both rules if trusted
    assert depth_gate.reject_reason("ABC") is None
    assert md["calls"] == 2                                                         # one read + exactly one re-read


def test_age_exactly_at_the_limit_is_fresh(md, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000.0)                                # no time spent in our cache
    md["bodies"] = [{"spread_pct": 0.9, "age_s": 20.0}]
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")
    assert md["calls"] == 1


def test_just_over_the_limit_is_old(md):
    md["bodies"] = [{"spread_pct": 0.9, "age_s": 20.5}]
    assert depth_gate.reject_reason("ABC") is None


def test_limit_zero_switches_the_check_off(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_QUOTE_AGE_S", 0.0)
    md["bodies"] = [{"spread_pct": 0.9, "age_s": 9999.0}]
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")
    assert md["calls"] == 1


def test_unknown_age_is_allowed_by_default(md):
    md["bodies"] = [{"spread_pct": 0.9, "book_value_5": 1e6}]                       # older market-data: no age_s
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")


def test_unknown_age_refuse_treats_depth_as_unknown(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", "refuse")
    md["bodies"] = [{"spread_pct": 0.9, "book_value_5": 1e6}]
    assert depth_gate.reject_reason("ABC") is None


def test_unknown_age_refuse_is_case_and_space_insensitive(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", " Refuse ")
    md["bodies"] = [{"spread_pct": 0.9}]
    assert depth_gate.reject_reason("ABC") is None


def test_reread_that_comes_back_without_age_follows_the_unknown_rule(md):
    md["bodies"] = [{"spread_pct": 0.1, "age_s": 60.0}, {"spread_pct": 0.9}]        # 2nd answer: no age -> allow
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")


def test_reread_failure_keeps_depth_unknown(md, monkeypatch):
    calls = {"n": 0}

    def flaky(symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"spread_pct": 0.9, "age_s": 60.0}
        return None
    monkeypatch.setattr(depth_gate, "_fetch", flaky)
    assert depth_gate.reject_reason("ABC") is None
    assert calls["n"] == 2


def test_exception_in_the_age_check_never_raises(md, monkeypatch):
    monkeypatch.setattr(depth_gate, "_quote_age_s", lambda *a, **k: 1 / 0)
    assert depth_gate.reject_reason("ABC") is None


def test_gate_off_still_makes_no_call(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", False)
    assert depth_gate.reject_reason("ABC") is None
    assert md["calls"] == 0


def test_a_cached_answer_ages_while_it_waits(md, monkeypatch):
    """A body that was 18 s old when market-data sent it is 21 s old after 3 s in our cache -> old, re-read."""
    md["bodies"] = [{"spread_pct": 0.9, "age_s": 18.0}, {"spread_pct": 0.9, "age_s": 1.0}]
    t = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: t["now"])
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")              # fetched at 1000, age 18 -> ok
    assert md["calls"] == 1
    t["now"] = 1003.0                                                               # cached (ttl 5 s) but now 21 s old
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_SPREAD:")
    assert md["calls"] == 2


# --- max_qty_from_book ----------------------------------------------------------------------------------------------

def test_size_cap_uses_a_fresh_book(md):
    md["bodies"] = [{"book_value_5": 200000.0, "age_s": 1.0}]
    assert depth_gate.max_qty_from_book("ABC", 100.0) == 100                         # 200000/2 * 10% / 100


def test_size_cap_ignores_an_old_book(md):
    md["bodies"] = [{"book_value_5": 200000.0, "age_s": 99.0}]
    assert depth_gate.max_qty_from_book("ABC", 100.0) is None
    assert md["calls"] == 2


def test_size_cap_with_unknown_age_refuse(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", "refuse")
    md["bodies"] = [{"book_value_5": 200000.0}]
    assert depth_gate.max_qty_from_book("ABC", 100.0) is None


# --- max_qty_from_depth20 -------------------------------------------------------------------------------------------

@pytest.fixture
def d20(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_SLIP_PCT", 0.3)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_MAX_SHARE_PCT", 10.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_WAIT_S", 0.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_QUOTE_AGE_S", 20.0)
    st = {"body": {"available": True, "buy_qty_within_slip": 1000, "age_s": 1.0}}

    class Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return Resp(200, dict(st["body"]))
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    return st


def test_depth20_fresh_book_caps(d20):
    assert depth_gate.max_qty_from_depth20("ABC", 500) == 100


def test_depth20_old_book_gives_no_cap(d20):
    d20["body"]["age_s"] = 45.0
    assert depth_gate.max_qty_from_depth20("ABC", 500) is None


def test_depth20_age_at_the_limit_still_caps(d20):
    d20["body"]["age_s"] = 20.0
    assert depth_gate.max_qty_from_depth20("ABC", 500) == 100


def test_depth20_without_age_is_used_as_before(d20):
    del d20["body"]["age_s"]
    assert depth_gate.max_qty_from_depth20("ABC", 500) == 100


def test_depth20_limit_zero_ignores_age(d20, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_QUOTE_AGE_S", 0.0)
    d20["body"]["age_s"] = 9999.0
    assert depth_gate.max_qty_from_depth20("ABC", 500) == 100


def test_depth20_garbage_age_is_ignored(d20):
    d20["body"]["age_s"] = "old"
    assert depth_gate.max_qty_from_depth20("ABC", 500) == 100


# --- config defaults ------------------------------------------------------------------------------------------------

def test_config_defaults(monkeypatch):
    """Reloads config in a way that restores the very same module state afterwards (monkeypatch undoes the env)."""
    import importlib
    monkeypatch.delenv("ENTRY_DEPTH_MAX_QUOTE_AGE_S", raising=False)
    monkeypatch.delenv("ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", raising=False)
    saved = dict(vars(config))
    try:
        importlib.reload(config)
        assert config.ENTRY_DEPTH_MAX_QUOTE_AGE_S == 20.0
        assert config.ENTRY_DEPTH_QUOTE_AGE_UNKNOWN == "allow"
        monkeypatch.setenv("ENTRY_DEPTH_MAX_QUOTE_AGE_S", "7")
        monkeypatch.setenv("ENTRY_DEPTH_QUOTE_AGE_UNKNOWN", "REFUSE")
        importlib.reload(config)
        assert config.ENTRY_DEPTH_MAX_QUOTE_AGE_S == 7.0
        assert config.ENTRY_DEPTH_QUOTE_AGE_UNKNOWN == "refuse"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
        for k, v in saved.items():
            if k.startswith("__"):
                continue
            setattr(config, k, v)
