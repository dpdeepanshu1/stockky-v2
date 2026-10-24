"""Market Weather step 1: the /control/overlay routes (control/kit.mount) behind the admin dependency. Needs fastapi (CI has it); the engine itself is
tested offline in scripts/test_regime_overlay.py."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control import kit  # noqa: E402


def _admin(authorization: str = Header(default="")) -> str:
    if authorization != "Bearer good":
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")
    return "admin"


H = {"Authorization": "Bearer good"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    mod = types.ModuleType("fake_cfg_overlay_route")
    mod.MODE, mod.RISK = "live", 1.0
    monkeypatch.setitem(sys.modules, "fake_cfg_overlay_route", mod)
    monkeypatch.delenv("MARKET_STATE_MODE", raising=False)
    monkeypatch.delenv("T_RISK", raising=False)
    kit.ACTIVE.clear()
    flags = (
        kit.Flag("MARKET_STATE_MODE", "mode", "g", "enum", "h", choices=("off", "shadow", "live"), targets=(("fake_cfg_overlay_route", "MODE"),), live=True, shipped="live"),
        kit.Flag("T_RISK", "risk", "g", "float", "h", lo=0.1, hi=1.0, looser="higher", overlay=True, targets=(("fake_cfg_overlay_route", "RISK"),), live=True, shipped="1.0"),
    )
    app = FastAPI()
    kit.mount(app, kit.Controller("svc", flags, kit.Store(str(tmp_path))), _admin)
    c = TestClient(app)
    c.mod = mod
    yield c
    kit.ACTIVE.clear()


def _body(**over):
    b = {"entries": {"T_RISK": 0.5}, "until": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(), "state": "stressed", "version": "v1"}
    b.update(over)
    return b


def test_overlay_routes_need_the_admin_session(client):
    assert client.post("/control/overlay", json=_body()).status_code == 401
    assert client.get("/control/overlay").status_code == 401
    assert client.delete("/control/overlay").status_code == 401


def test_post_tightens_get_shows_it_and_delete_restores(client):
    r = client.post("/control/overlay", json=_body(), headers=H)
    assert r.status_code == 200 and r.json()["ok"] and client.mod.RISK == 0.5
    g = client.get("/control/overlay", headers=H).json()
    assert g["mode"] == "live" and g["active"]["state"] == "stressed" and g["overlays"][0]["key"] == "T_RISK"
    flags = client.get("/control/flags", headers=H).json()
    assert [f for f in flags["flags"] if f["key"] == "T_RISK"][0]["overlay"]["state"] == "stressed"
    assert client.delete("/control/overlay", headers=H).json()["cleared"] == ["T_RISK"]
    assert client.mod.RISK == 1.0


def test_same_value_is_accepted_and_a_past_until_is_a_400(client):
    r = client.post("/control/overlay", json=_body(entries={"T_RISK": 1.0}), headers=H)
    assert r.status_code == 200                                              # equal to the saved value = SAME, accepted
    r = client.post("/control/overlay", json=_body(until="2020-01-01T00:00:00+00:00"), headers=H)
    assert r.status_code == 400 and client.mod.RISK == 1.0
