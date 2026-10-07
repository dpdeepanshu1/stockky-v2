"""group231: during a QUOTE-family cooldown a held-position (POSITION lane) call may send one small probe per
second; every other lane and the candle family stay silent, and a probe answered 403 never extends the cooldown."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import angelone_budget as b
from test_group211_angelone_budget import (  # noqa: F401  (fixtures + harness reused)
    _clean, _Client, _Resp, _session, run, RATE_LIMITED,
)

OK_QUOTE = {"data": {"fetched": [{"symbolToken": "1", "ltp": 10.0}]}}


def _age_trip(seconds):
    """Pretend the quote cooldown started `seconds` ago (the cooldown itself keeps running)."""
    b._cool[b.QUOTE]["last_trip"] = time.time() - seconds


def test_probe_waits_min_age_after_the_trip():
    b.trip("quote")
    assert b.position_probe_allowed(b.POSITION) is False
    _age_trip(3)
    assert b.position_probe_allowed(b.POSITION) is True


def test_probe_is_one_per_interval():
    b.trip("quote"); _age_trip(3)
    assert b.position_probe_allowed(b.POSITION) is True
    assert b.position_probe_allowed(b.POSITION) is False
    b._probe_next = time.time() - 0.01
    assert b.position_probe_allowed(b.POSITION) is True
    assert b.stats()["position_probes"]["allowed"] == 2


@pytest.mark.parametrize("lane", [b.CANDIDATE, b.BACKGROUND, None])
def test_other_lanes_never_probe(lane):
    b.trip("quote"); _age_trip(3)
    assert b.position_probe_allowed(lane) is False


def test_candle_family_never_probes():
    b.trip("getCandleData"); _age_trip(3)
    assert b.position_probe_allowed(b.POSITION, "candle") is False


def test_probe_off_and_budget_off(monkeypatch):
    b.trip("quote"); _age_trip(3)
    monkeypatch.setenv("ANGELONE_POSITION_PROBE_INTERVAL_S", "0")
    assert b.position_probe_allowed(b.POSITION) is False
    monkeypatch.delenv("ANGELONE_POSITION_PROBE_INTERVAL_S")
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    assert b.position_probe_allowed(b.POSITION) is False


def test_denied_probe_pauses_probing():
    b.trip("quote"); _age_trip(3)
    assert b.position_probe_allowed(b.POSITION) is True
    b.position_probe_denied()
    assert b.position_probe_allowed(b.POSITION) is False
    assert b._probe_next - time.time() > 8
    assert b.stats()["position_probes"]["denied"] == 1


def test_reset_clears_probe_state():
    b.trip("quote"); _age_trip(3)
    b.position_probe_allowed(b.POSITION)
    b._reset()
    assert b.stats()["position_probes"] == {"allowed": 0, "denied": 0} and b._probe_next == 0.0


# ── client wiring ────────────────────────────────────────────────────────────
def test_get_quote_probe_sends_for_held_symbol_during_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(200, {"data": {"fetched": [{"ltp": 10.0}]}}))
    b.trip("quote"); _age_trip(3)
    run(s.get_quote("NSE", "1", lane=b.POSITION))
    assert len(_Client.sent) == 1


def test_get_quote_candidate_still_silent_during_cooldown(monkeypatch):
    s = _session(monkeypatch)
    b.trip("quote"); _age_trip(3)
    assert run(s.get_quote("NSE", "1", lane=b.CANDIDATE)) == {}
    assert _Client.sent == []


def test_get_quote_second_probe_inside_the_interval_is_silent(monkeypatch):
    s = _session(monkeypatch)
    b.trip("quote"); _age_trip(3)
    run(s.get_quote("NSE", "1", lane=b.POSITION))
    run(s.get_quote("NSE", "1", lane=b.POSITION))
    assert len(_Client.sent) == 1


def test_probe_403_does_not_extend_the_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    b.trip("quote"); _age_trip(3)
    until, trips = b._cool[b.QUOTE]["until"], b._cool[b.QUOTE]["trips"]
    assert run(s.get_quote("NSE", "1", lane=b.POSITION)) == {}
    assert b._cool[b.QUOTE]["until"] == until and b._cool[b.QUOTE]["trips"] == trips
    assert b.stats()["position_probes"]["denied"] == 1


def test_batch_probe_403_does_not_extend_the_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    b.trip("quote"); _age_trip(3)
    trips = b._cool[b.QUOTE]["trips"]
    assert run(s.get_quotes_batch("NSE", ["1"], lane=b.POSITION)) == []
    assert b._cool[b.QUOTE]["trips"] == trips and b.stats()["position_probes"]["denied"] == 1


def test_batch_probe_sends_during_cooldown(monkeypatch):
    s = _session(monkeypatch, _Resp(200, OK_QUOTE))
    b.trip("quote"); _age_trip(3)
    assert run(s.get_quotes_batch("NSE", ["1"], lane=b.POSITION)) == OK_QUOTE["data"]["fetched"]
    assert len(_Client.sent) == 1


def test_rate_limiter_cooldown_also_allows_a_probe(monkeypatch):
    s = _session(monkeypatch, _Resp(200, OK_QUOTE))
    import angelone_client as ac
    monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: True)   # the 30 s angelone_quote cooldown
    b.trip("quote"); _age_trip(3)
    run(s.get_quotes_batch("NSE", ["1"], lane=b.POSITION))
    assert len(_Client.sent) == 1
    assert run(s.get_quotes_batch("NSE", ["1"], lane=b.CANDIDATE)) == [] and len(_Client.sent) == 1
