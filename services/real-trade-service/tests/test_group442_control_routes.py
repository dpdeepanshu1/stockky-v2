"""group442: the /control routes (control/kit.mount) behind the admin dependency. Needs fastapi (CI has it); the engine itself is tested offline in scripts/test_control_kit.py."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from control import kit  # noqa: E402


def _admin(authorization: str = Header(default="")) -> str:
    if authorization != "Bearer good":
        raise HTTPException(status_code=401, detail="Invalid or expired admin session")
    return "admin"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    mod = types.ModuleType("fake_cfg_route")
    mod.ROOM = 0.5
    mod.GATE = True
    monkeypatch.setitem(sys.modules, "fake_cfg_route", mod)
    monkeypatch.delenv("T_ROOM", raising=False)
    monkeypatch.delenv("T_GATE", raising=False)
    flags = (
        kit.Flag("T_ROOM", "room", "g", "float", "h", lo=0, hi=5, looser="lower", targets=(("fake_cfg_route", "ROOM"),), live=True, shipped="0.5"),
        kit.Flag("T_GATE", "gate", "g", "bool", "h", looser="off", targets=(("fake_cfg_route", "GATE"),), live=True, shipped="true", recommended="true"),
    )
    app = FastAPI()
    kit.mount(app, kit.Controller("svc", flags, kit.Store(str(tmp_path))), _admin)
    c = TestClient(app)
    c.mod = mod
    return c


H = {"Authorization": "Bearer good"}


def test_every_route_needs_the_admin_session(client):
    assert client.get("/control/flags").status_code == 401
    assert client.post("/control/flags/T_ROOM", json={"value": "1"}).status_code == 401
    assert client.delete("/control/flags/T_ROOM").status_code == 401
    assert client.get("/control/audit").status_code == 401
    assert client.post("/control/preset/recommended", json={}).status_code == 401


def test_list_set_confirm_clear_and_audit(client):
    j = client.get("/control/flags", headers=H).json()
    assert j["service"] == "svc" and [f["key"] for f in j["flags"]] == ["T_ROOM", "T_GATE"]
    r = client.post("/control/flags/T_ROOM", json={"value": "0.9"}, headers=H).json()
    assert r["applied_live"] and client.mod.ROOM == 0.9
    r = client.post("/control/flags/T_GATE", json={"value": "false"}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["needs_confirm"] and client.mod.GATE is True
    assert client.post("/control/flags/T_GATE", json={"value": "false", "confirm": True, "note": "test"}, headers=H).json()["applied_live"]
    assert client.post("/control/flags/NOPE", json={"value": "1"}, headers=H).status_code == 404
    assert client.post("/control/flags/T_ROOM", json={"value": "99"}, headers=H).status_code == 400
    assert client.delete("/control/flags/T_ROOM", headers=H).json()["cleared"] is True and client.mod.ROOM == 0.5
    rows = client.get("/control/audit", headers=H).json()["rows"]
    assert [x["action"] for x in rows][:2] == ["clear", "set"]
    p = client.post("/control/preset/recommended", json={"dry_run": True}, headers=H).json()
    assert p["dry_run"] and p["changes"][0]["key"] == "T_GATE"
