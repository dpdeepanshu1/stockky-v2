"""group227 (item 1 of the 2026-10-07 10:33 IST log review): the AngelOne rate-limit cooldown is kept per family.

A getCandleData 403 used to start ONE cooldown that every AngelOne caller honoured, so quotes (a different endpoint with
its own limit, answering normally) and the 499-symbol feed poll stopped too and everything fell to a saturated yfinance.
Now: candle 403 -> candle callers only; quote 403 -> quote callers (ltpData, batches, feed, gainers) only.
ANGELONE_SPLIT_COOLDOWN=0 restores the single shared cooldown.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import angelone_budget as b
from test_group211_angelone_budget import (  # noqa: F401  (fixtures + harness reused)
    _clean, feed, _Session, _Client, _Resp, _session, _one_cycle, run, RATE_LIMITED,
)


# ── families ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,fam", [
    ("getCandleData", "candle"), ("angelone_candle", "candle"), ("CANDLES", "candle"),
    ("quote", "quote"), ("quote(batch)", "quote"), ("angelone_quote", "quote"), ("gainers", "quote"),
    ("", "quote"), (None, "quote"),
])
def test_family_for(name, fam):
    assert b.family_for(name) == fam


def test_split_off_puts_everything_in_one_family(monkeypatch):
    monkeypatch.setenv("ANGELONE_SPLIT_COOLDOWN", "0")
    assert b.split_enabled() is False
    assert b.family_for("getCandleData") == "quote"
    b.trip("getCandleData")
    assert b.in_global_cooldown("quote") and b.skip(b.CANDIDATE, "quote") is True


@pytest.mark.parametrize("val,expected", [("", True), (" ", True), ("1", True), ("0", False), ("off", False), (" No ", False)])
def test_split_flag_is_blank_safe(monkeypatch, val, expected):
    monkeypatch.setenv("ANGELONE_SPLIT_COOLDOWN", val)
    assert b.split_enabled() is expected


def test_unknown_family_name_is_treated_as_quote():
    b.trip("quote")
    assert b.in_global_cooldown("nonsense") is True


# ── independence ─────────────────────────────────────────────────────────────
def test_candle_trip_leaves_quotes_alone():
    assert b.trip("getCandleData") == 30.0
    assert b.in_global_cooldown("candle") and not b.in_global_cooldown("quote")
    assert b.skip(b.POSITION, "candle") is True and b.skip(b.POSITION, "quote") is False
    assert 29.0 < b.cooldown_remaining("candle") <= 30.0 and b.cooldown_remaining("quote") == 0.0


def test_quote_trip_leaves_candles_alone():
    assert b.trip("quote") == 30.0
    assert b.in_global_cooldown("quote") and not b.in_global_cooldown("candle")


def test_no_family_means_any_running_cooldown_counts():
    assert not b.in_global_cooldown()
    b.trip("getCandleData")
    assert b.in_global_cooldown() and b.skip(b.CANDIDATE) is True
    assert b.cooldown_remaining() > 0


def test_each_family_escalates_on_its_own():
    assert b.trip("getCandleData") == 30.0
    assert b.trip("quote") == 30.0                           # a candle trip does not make the first quote trip a 60 s one
    b._cool["candle"]["until"] = time.time() - 1
    assert b.trip("getCandleData") == 60.0                   # candle doubles
    assert b._cool["quote"]["dur"] == 30.0


def test_late_403_is_suppressed_per_family_only():
    b.trip("getCandleData")
    assert b.trip("getCandleData") == 0.0                    # same family, already running: late answer
    assert b.trip("quote") == 30.0                           # different family: a real, new trip
    s = b.stats()
    assert s["trips"] == 2 and s["suppressed_late_403s"] == 1


def test_trip_log_names_the_family(caplog):
    with caplog.at_level("WARNING"):
        b.trip("getCandleData")
    assert "AngelOne candle callers skip AngelOne for 30s" in caplog.text
    caplog.clear()
    b.trip("quote")
    assert "AngelOne quote callers skip AngelOne for 30s" in caplog.text


def test_trip_log_says_all_callers_when_split_is_off(monkeypatch, caplog):
    monkeypatch.setenv("ANGELONE_SPLIT_COOLDOWN", "0")
    with caplog.at_level("WARNING"):
        b.trip("getCandleData")
    assert "ALL AngelOne callers skip AngelOne for 30s" in caplog.text


def test_budget_off_means_no_family_cooldown(monkeypatch):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    assert b.trip("getCandleData") == 0.0
    assert not b.in_global_cooldown("candle") and b.cooldown_remaining("candle") == 0.0


# ── admit() checks the provider's own family ─────────────────────────────────
def test_admit_for_quotes_ignores_a_candle_cooldown_and_vice_versa():
    import rate_limiter as rl
    bucket = rl._get_bucket("angelone_quote")
    bucket.tokens = bucket.capacity if hasattr(bucket, "capacity") else 1000.0
    b.trip("getCandleData")
    assert run(b.admit(b.BACKGROUND, "angelone_quote", 1, 0.0)) is True
    assert run(b.admit(b.BACKGROUND, "angelone_candle", 1, 0.0)) is False
    assert b.stats()["lanes"]["background"]["skipped_cooldown"] == 1


# ── stats ────────────────────────────────────────────────────────────────────
def test_stats_report_each_family():
    s = b.stats()
    assert s["split_cooldown"] is True
    assert s["cooldowns"] == {"quote": {"active": False, "remaining_s": 0.0, "trips": 0},
                              "candle": {"active": False, "remaining_s": 0.0, "trips": 0}}
    b.trip("getCandleData")
    s = b.stats()
    assert s["global_cooldown_active"] is True
    assert s["cooldowns"]["candle"]["active"] is True and s["cooldowns"]["candle"]["trips"] == 1
    assert s["cooldowns"]["quote"]["active"] is False
    assert 29.0 < s["global_cooldown_remaining_s"] <= 30.0
    assert s["trips"] == 1


# ── the client: real endpoints ───────────────────────────────────────────────
def test_candle_403_then_quote_endpoints_still_send(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b")) == []
    assert b.in_global_cooldown("candle") and not b.in_global_cooldown("quote")
    sent = len(_Client.sent)
    run(s.get_quote("NSE", "1"))                             # goes out (the stub then answers 403 to it too)
    assert len(_Client.sent) == sent + 1
    assert b.stats()["last_trip_endpoint"] == "quote"       # and that 403 is a quote-family trip of its own


def test_quote_403_still_blocks_quote_endpoints_but_not_candles(monkeypatch):
    s = _session(monkeypatch, _Resp(403, RATE_LIMITED))
    assert run(s.get_quote("NSE", "1")) == {}
    assert b.in_global_cooldown("quote") and not b.in_global_cooldown("candle")
    sent = len(_Client.sent)
    assert run(s.get_quotes_batch("NSE", ["1"])) == []
    assert run(s.get_gainers_losers()) == []
    assert len(_Client.sent) == sent                         # quote family: nothing out
    run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b"))
    assert len(_Client.sent) == sent + 1                     # candles still go out


def test_client_helper_defaults_to_the_quote_family(monkeypatch):
    import angelone_client as ac
    b.trip("getCandleData")
    assert ac._budget_skip(None) is False
    assert ac._budget_skip(None, "candle") is True


# ── the feed poll keeps running during a candle cooldown ─────────────────────
def test_poll_cycle_keeps_polling_during_a_candle_cooldown(feed, monkeypatch):
    b.trip("getCandleData")
    sess = _one_cycle(feed, monkeypatch, 5)
    assert sorted(t for ts, _ in sess.calls for t in ts) == ["1", "2", "3", "4", "5"]


def test_poll_cycle_still_stops_during_a_quote_cooldown(feed, monkeypatch):
    sess = _one_cycle(feed, monkeypatch, 5, cooldown=True)
    assert sess.calls == []


def test_feed_cooldown_helper_only_sees_the_quote_family(feed):
    b.trip("getCandleData")
    assert feed._cooldown_running() is False
    b.trip("quote")
    assert feed._cooldown_running() is True


def test_feed_http_denied_safety_net_trips_the_quote_family_only(feed):
    import httpx
    req = httpx.Request("POST", "http://x")
    exc = httpx.HTTPStatusError("e", request=req, response=httpx.Response(403, request=req))
    b.trip("getCandleData")
    assert feed._trip_on_http_denied(exc) is True            # a running CANDLE cooldown must not hide a quote denial
    assert b.in_global_cooldown("quote")
