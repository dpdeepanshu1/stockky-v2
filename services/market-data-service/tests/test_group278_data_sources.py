"""group278: GET /internal/data-sources report (order, per-source health, which source is serving).
Run: python3 -m pytest tests/test_group278_data_sources.py -q"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_sources as ds

ORDER = ["dhan", "angelone", "yfinance"]
DHAN_OK = {"enabled": True, "credentials": {"ok": True}, "client": {"paused": False, "breaker": {"state": "closed"},
                                                                    "last_error": None}}


def report(**kw):
    base = dict(quote_order=ORDER, history_order=ORDER, dhan=DHAN_OK)
    base.update(kw)
    return ds.build_report(**base)


def test_dhan_serves_when_healthy():
    r = report()
    assert r["quotes"]["serving"] == "dhan" and r["history"]["serving"] == "dhan"


def test_paused_dhan_falls_to_angelone():
    d = {**DHAN_OK, "client": {"paused": True, "last_error": "HTTP 429", "last_error_at": "t"}}
    r = report(dhan=d)
    assert r["quotes"]["serving"] == "angelone"
    assert r["quotes"]["sources"]["dhan"]["state"] == "paused"
    assert r["quotes"]["sources"]["dhan"]["last_error"] == "HTTP 429"


def test_open_breaker_counts_as_paused():
    d = {**DHAN_OK, "client": {"paused": False, "breaker": {"state": "OPEN"}}}
    assert report(dhan=d)["quotes"]["serving"] == "angelone"


def test_dhan_and_angelone_down_falls_to_yfinance():
    r = report(dhan={"enabled": False}, angelone_quote_cooldown_s=120.0)
    assert r["quotes"]["serving"] == "yfinance"
    assert r["quotes"]["sources"]["angelone"] == {"state": "cooling", "cooldown_seconds_left": 120.0}


def test_quote_and_history_cooldowns_are_separate():
    r = report(angelone_candle_cooldown_s=60.0, dhan={"enabled": False})
    assert r["quotes"]["serving"] == "angelone" and r["history"]["serving"] == "yfinance"


def test_all_down_serving_is_none():
    r = report(dhan={"enabled": False}, angelone_configured=False, yfinance_cooldown_s=30.0)
    assert r["quotes"]["serving"] is None


def test_order_is_respected():
    r = report(quote_order=["yfinance", "dhan"])
    assert r["quotes"]["serving"] == "yfinance" and r["quotes"]["order"] == ["yfinance", "dhan"]


def test_missing_dhan_credentials_is_not_configured():
    r = report(dhan={"enabled": True, "credentials": {"ok": False}, "client": {}})
    assert r["quotes"]["sources"]["dhan"]["state"] == "not_configured"


def test_collect_survives_every_helper_failing():
    def boom(*a):
        raise RuntimeError("x")
    r = ds.collect(boom, boom, boom, boom, boom)
    assert r["quotes"]["order"] == ORDER and r["quotes"]["serving"] == "angelone"   # Dhan unknown -> disabled; AngelOne assumed configured
