"""E285 round 87 (Real Trade side): every Dhan order call is appended to the account-wide one-second window position-stocks-service reads; Real Trade never waits.
Temp file, stubbed SDK, no broker, no database. Plain runner works too: python3 tests/test_e285_shared_order_window.py (service folder) is not supported (pytest fixtures)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import dhan_client, live_state
from execution import shared_rate_window as srw

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def window(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDER_RATE_SHARED_FILE", str(tmp_path / "win.json"))
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", True, raising=False)
    return tmp_path / "win.json"


def test_note_order_call_appends_one_rt_entry():
    dhan_client._note_order_call("place_order")
    dhan_client._note_order_call("cancel_order")
    assert srw.total_last_s() == (2, {"rt": 2})


def test_switch_off_records_nothing(monkeypatch, window):
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", False, raising=False)
    dhan_client._note_order_call("place_order")
    assert not window.exists()


def test_an_unusable_file_never_raises_or_stops_the_order(monkeypatch, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setenv("ORDER_RATE_SHARED_FILE", str(blocker / "sub" / "w.json"))
    dhan_client._note_order_call("place_order")                       # must simply return


def test_cancel_order_records_before_the_sdk_call(monkeypatch):
    seen = []
    client = SimpleNamespace(cancel_order=lambda oid: seen.append(srw.total_last_s()) or {"status": "success", "data": {"orderId": oid}})
    monkeypatch.setattr(dhan_client, "_get_sdk_client", lambda db: client)
    dhan_client.cancel_order(object(), is_armed=True, dhan_order_id="123")
    assert seen == [(1, {"rt": 1})]                                    # counted by the time the SDK was called


def test_forever_order_calls_are_counted(monkeypatch):
    monkeypatch.setattr(dhan_client, "_forever_headers_and_client", lambda db: ("C1", {"access-token": "t"}))
    resp = SimpleNamespace(status_code=200, text="{}", content=b"{}", json=lambda: {"data": {"orderId": "9"}})
    monkeypatch.setattr(dhan_client.httpx, "post", lambda *a, **k: resp)
    monkeypatch.setattr(dhan_client.httpx, "delete", lambda *a, **k: resp)
    dhan_client.place_forever_order(object(), is_armed=True, security_id="1", exchange_segment="NSE_EQ", transaction_type="SELL", quantity=1, price=0.0, trigger_price=100.0)
    dhan_client.cancel_forever_order(object(), dhan_order_id="9")
    assert srw.total_last_s() == (2, {"rt": 2})


def test_order_rate_part_reads_the_shared_window_or_says_it_is_off(monkeypatch):
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", False, raising=False)
    assert live_state.order_rate_part() == {"enabled": False}
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", True, raising=False)
    srw.record("ps", "modify_super_order")
    out = live_state.order_rate_part()
    assert out["enabled"] is True and out["working"] is True and out["calls_last_s"] == 1 and out["by_service_last_s"] == {"ps": 1}


def test_the_two_services_ship_the_same_copy_and_the_catalog_has_the_switch():
    a = (ROOT / "execution" / "shared_rate_window.py").read_bytes()
    b = (ROOT.parent / "position-stocks-service" / "execution" / "shared_rate_window.py").read_bytes()
    assert a == b
    from control.catalog import CATALOG
    assert {f.key: f for f in CATALOG}["ORDER_RATE_SHARED_ENABLED"].shipped == "true"
