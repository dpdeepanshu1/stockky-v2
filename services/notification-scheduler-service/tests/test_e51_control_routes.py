"""E51: the scheduler's /control routes (alert switches for the calibration page): fail-closed key, live apply, catalog == code defaults."""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SVC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SVC))
KEY = "k" * 20


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_CONTROL_KEY", KEY)
    for k in ("PATTERN_ALERTS_TIME", "PATTERN_ALERTS_ENABLED", "WATCHLIST_MOVE_ALERT_PCT"):
        monkeypatch.delenv(k, raising=False)
    from control import kit
    from control.auth import require_gateway
    from control.catalog import CATALOG
    app = FastAPI()
    kit.mount(app, kit.Controller("notification-scheduler-service", CATALOG, kit.Store(str(tmp_path))), require_gateway)
    yield TestClient(app)
    for f in CATALOG:  # the kit writes os.environ directly: never leak a switch into the next test
        os.environ.pop(f.key, None)


H = {"X-Control-Key": KEY, "X-Admin-User": "deep"}


def test_no_key_configured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("SCHEDULER_CONTROL_KEY")
    assert client.get("/control/flags", headers=H).status_code == 503
    monkeypatch.setenv("SCHEDULER_CONTROL_KEY", "short")
    assert client.get("/control/flags", headers={"X-Control-Key": "short"}).status_code == 503


def test_wrong_or_missing_key_is_401(client):
    assert client.get("/control/flags").status_code == 401
    assert client.get("/control/flags", headers={"X-Control-Key": "x" * 20}).status_code == 401
    assert client.post("/control/flags/PATTERN_ALERTS_TIME", json={"value": "17:00"}).status_code == 401


def test_lists_all_eight_switches_with_shipped_values(client):
    d = client.get("/control/flags", headers=H).json()
    assert d["service"] == "notification-scheduler-service" and d["summary"]["flags"] == 8
    assert {f["key"]: f["effective"] for f in d["flags"]}["PATTERN_ALERTS_TIME"] == "16:05"


def test_change_is_live_for_the_scheduler_code(client):
    r = client.post("/control/flags/WATCHLIST_MOVE_ALERT_PCT", headers=H, json={"value": 7.5})
    assert r.status_code == 200 and r.json()["applied_live"] is True and r.json()["restart_required"] is False
    assert os.environ["WATCHLIST_MOVE_ALERT_PCT"] == "7.5"
    sys.path.insert(0, str(SVC / "scheduler"))
    import watchlist_moves as wm
    assert wm.threshold_pct() == 7.5
    r = client.post("/control/flags/PATTERN_ALERTS_ENABLED", headers=H, json={"value": False})
    assert r.json()["applied_live"] is True
    import pattern_alerts as pa
    assert pa.enabled() is False
    assert client.delete("/control/flags/WATCHLIST_MOVE_ALERT_PCT", headers=H).json()["cleared"] is True
    assert "WATCHLIST_MOVE_ALERT_PCT" not in os.environ


def test_out_of_range_and_unknown_are_refused_and_audit_names_the_admin(client):
    assert client.post("/control/flags/WATCHLIST_MOVE_ALERT_PCT", headers=H, json={"value": 99}).status_code == 400
    assert client.post("/control/flags/NOT_A_SWITCH", headers=H, json={"value": 1}).status_code in (400, 404)
    client.post("/control/flags/PATTERN_ALERTS_TIME", headers=H, json={"value": "17:10"})
    rows = client.get("/control/audit", headers=H).json()["rows"]
    assert rows and rows[-1]["by"] == "deep"


def test_catalog_shipped_values_match_the_code_defaults():
    from control.catalog import CATALOG
    src = (SVC / "scheduler" / "pattern_alerts.py").read_text() + (SVC / "scheduler" / "watchlist_moves.py").read_text()
    for f in CATALOG:
        m = re.search(r'"%s",\s*([^,)\n]+)' % f.key, src)
        assert m, f.key
        default = m.group(1).strip().strip('"')
        assert float(default.replace(":", ".")) == float(f.shipped.replace(":", ".")) if re.match(r"^[\d.:]+$", default) else default.lower() == f.shipped.lower(), (f.key, default, f.shipped)


def test_kit_copy_is_identical_to_the_trading_services():
    a = (SVC / "control" / "kit.py").read_bytes()
    assert a == (SVC.parent / "real-trade-service" / "control" / "kit.py").read_bytes()
    assert (SVC / "control" / "boot.py").read_bytes() == (SVC.parent / "real-trade-service" / "control" / "boot.py").read_bytes()


def test_main_mounts_control_inside_a_guard_that_cannot_stop_boot():
    s = (SVC / "main.py").read_text()
    assert "_control_kit.mount(app, _control, _require_gateway)" in s and "except Exception as _control_err" in s
