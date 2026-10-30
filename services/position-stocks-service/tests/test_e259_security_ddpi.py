"""E259 - Dhan security-id lookup (blank-series rows, "listed but not EQ" message, one throttled re-load on a miss) and DDPI-aware eDIS summary.
Run from services/position-stocks-service:  python3 -m pytest tests/test_e259_security_ddpi.py -q"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import pytest

from execution import dhan_client as dc

CRED = "dhan_credentials_ro"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(dc, "_security_cache", {})
    monkeypatch.setattr(dc, "_security_other_series", {})
    monkeypatch.setattr(dc, "_security_cache_loaded_at", 0.0)
    monkeypatch.setattr(dc, "_security_load_failed_at", 0.0)
    monkeypatch.setattr(dc, "_security_miss_reload_at", 0.0)
    monkeypatch.setattr(dc, "_ddpi_cache", {"at": 0.0, "ttl": 0.0, "value": None})
    monkeypatch.setattr(dc, "_get_sdk_client", lambda db: object())


def _df(rows):
    return pd.DataFrame(rows, columns=["SEM_EXM_EXCH_ID", "SEM_INSTRUMENT_NAME", "SEM_SERIES", "SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID"])


ROWS = [("NSE", "EQUITY", "EQ", "TCS", "11536"), ("NSE", "EQUITY", None, "HFCL", "21951"),      # blank series -> NaN in pandas
        ("NSE", "EQUITY", "BE", "OLDCO", "999"), ("BSE", "EQUITY", "EQ", "ONLYBSE", "1"), ("NSE", "FUTSTK", "EQ", "TCS-FUT", "5")]


def _patch_list(monkeypatch, rows=ROWS):
    monkeypatch.setattr(dc, "_fetch_security_list_quiet", lambda client: _df(rows))


def test_norm_series():
    for v in (None, "", " ", "nan", "NaN", float("nan"), "None"):
        assert dc._norm_series(v) == "", v
    assert dc._norm_series(" EQ ") == "EQ" and dc._norm_series("BE") == "BE"


def test_blank_series_equity_row_is_kept_and_non_eq_is_recorded(monkeypatch):
    _patch_list(monkeypatch)
    dc._load_security_cache(None)
    assert dc._security_cache.get("HFCL") == "21951" and dc._security_cache.get("TCS") == "11536"
    assert "OLDCO" not in dc._security_cache and dc._security_other_series.get("OLDCO") == "BE"
    assert "ONLYBSE" not in dc._security_cache and "TCS-FUT" not in dc._security_cache


def test_miss_message_names_the_series(monkeypatch):
    _patch_list(monkeypatch)
    monkeypatch.setattr(dc, "_security_miss_reload_at", time.time())          # no re-load in this test
    dc._load_security_cache(None)
    with pytest.raises(dc.SecurityNotResolvedError) as e:
        dc.get_security_id(None, "OLDCO")
    assert "series 'BE'" in str(e.value) and "not tradable" in str(e.value)
    with pytest.raises(dc.SecurityNotResolvedError) as e2:
        dc.get_security_id(None, "NOSUCH")
    assert "not in Dhan's NSE equity list" in str(e2.value)


def test_miss_triggers_one_throttled_reload(monkeypatch):
    calls = []
    state = {"rows": [("NSE", "EQUITY", "EQ", "TCS", "11536")]}

    def fetch(client):
        calls.append(1)
        return _df(state["rows"])
    monkeypatch.setattr(dc, "_fetch_security_list_quiet", fetch)
    dc._load_security_cache(None)
    monkeypatch.setattr(dc, "_security_cache_loaded_at", time.time())
    assert len(calls) == 1
    state["rows"] = state["rows"] + [("NSE", "EQUITY", "EQ", "NEWCO", "777")]   # the list now has it, the cache does not
    assert dc.get_security_id(None, "NEWCO") == "777"                          # miss -> one re-load -> found
    assert len(calls) == 2
    with pytest.raises(dc.SecurityNotResolvedError):
        dc.get_security_id(None, "STILLNO")
    assert len(calls) == 2                                                     # throttled: no second re-load inside the window


def test_hit_never_reloads(monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "_fetch_security_list_quiet", lambda c: (calls.append(1), _df(ROWS))[1])
    dc._load_security_cache(None)
    monkeypatch.setattr(dc, "_security_cache_loaded_at", time.time())
    assert dc.get_security_id(None, "TCS") == "11536" and len(calls) == 1


# ---------------- DDPI ----------------
class _Resp:
    def __init__(self, body):
        self._b = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._b


def _creds(monkeypatch, ok=True):
    mod = getattr(dc, CRED)
    monkeypatch.setattr(mod, "get_decrypted_credentials", lambda db: ("cid", "tok") if ok else None)


@pytest.mark.parametrize("body,active,raw", [({"ddpi": "Active"}, True, "Active"), ({"ddpi": "Deactive"}, False, "Deactive"),
                                              ({"data": {"ddpi": "active"}}, True, "active"), ({"other": 1}, None, None)])
def test_ddpi_status_parses_profile(monkeypatch, body, active, raw):
    _creds(monkeypatch)
    monkeypatch.setattr(dc.httpx, "get", lambda *a, **k: _Resp(body))
    out = dc.ddpi_status(None)
    assert out == {"active": active, "raw": raw}


def test_ddpi_status_failure_is_unknown_and_cached(monkeypatch):
    _creds(monkeypatch)
    n = []

    def boom(*a, **k):
        n.append(1)
        raise RuntimeError("down")
    monkeypatch.setattr(dc.httpx, "get", boom)
    assert dc.ddpi_status(None)["active"] is None
    dc.ddpi_status(None)
    assert len(n) == 1                                   # the unknown answer is cached for a few minutes
    _creds(monkeypatch, ok=False)
    monkeypatch.setattr(dc, "_ddpi_cache", {"at": 0.0, "ttl": 0.0, "value": None})
    assert dc.ddpi_status(None)["active"] is None


def test_edis_summary_with_ddpi_active_is_verified(monkeypatch):
    monkeypatch.setattr(dc, "get_holdings", lambda db, *a, **k: [{"tradingSymbol": "KFINTECH", "totalQty": 3}], raising=False)
    monkeypatch.setattr(dc, "ddpi_status", lambda db: {"active": True, "raw": "Active"})

    def no_inquire(*a, **k):
        raise AssertionError("inquire must not be needed when DDPI is active")
    monkeypatch.setattr(dc, "edis_inquire", no_inquire)
    out = dc.edis_verification_summary(None)
    assert out["verified_today"] is True and out["ddpi_active"] is True and out["holdings_pending"] == 0


def test_edis_summary_without_ddpi_still_uses_inquire(monkeypatch):
    monkeypatch.setattr(dc, "get_holdings", lambda db, *a, **k: [{"tradingSymbol": "KFINTECH"}], raising=False)
    monkeypatch.setattr(dc, "ddpi_status", lambda db: {"active": False, "raw": "Deactive"})
    monkeypatch.setattr(dc, "edis_inquire", lambda db, isin="ALL": [{"isin": "INE138Y01010", "aprvdQty": 0, "totalQty": 3}])
    out = dc.edis_verification_summary(None)
    assert out["verified_today"] is False and out["holdings_pending"] == 1 and "ddpi_active" not in out
