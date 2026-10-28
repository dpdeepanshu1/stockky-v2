"""E235: the rt / ps tab tag on the HTTP routes (pytest + TestClient; skipped when fastapi is not installed)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
from fastapi.testclient import TestClient  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
DATA = tempfile.mkdtemp()
os.environ.update(SESSION_SECRET="route-test-secret-0123456789", RUNNER_DATA_DIR=DATA, RUNNER_SCRIPTS_DIR=str(HERE.parent.parent / "scripts"), RUNNER_GUARD_MARKET_HOURS="0")

import main  # noqa: E402
import auth  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


def H():
    return {"Authorization": "Bearer " + auth.mint_service_token("tester", 600)}


def _wait(client, rid):
    for _ in range(150):
        d = client.get(f"/runs/{rid}", headers=H()).json()
        if d["status"] not in ("queued", "running"):
            return d
        time.sleep(0.2)
    return d


def test_unknown_service_is_a_400(client):
    assert client.get("/runs?service=zz", headers=H()).status_code == 400
    assert client.get("/summary?service=zz", headers=H()).status_code == 400
    assert client.post("/runs", headers=H(), json={"kind": "job", "target": "control_snapshot", "service": "zz"}).status_code == 400


def test_a_tab_sees_its_own_runs_and_not_the_other_tabs(client):
    a = client.post("/runs", headers=H(), json={"kind": "job", "target": "control_snapshot", "service": "rt"}).json()
    b = client.post("/runs", headers=H(), json={"kind": "job", "target": "control_snapshot", "service": "ps"}).json()
    assert "-rt-" in a["id"] and "-ps-" in b["id"] and a["service"] == "rt" and b["service"] == "ps"
    _wait(client, a["id"])
    _wait(client, b["id"])
    rt_ids = {x["id"] for x in client.get("/runs?service=rt", headers=H()).json()["runs"]}
    ps_ids = {x["id"] for x in client.get("/runs?service=ps", headers=H()).json()["runs"]}
    assert a["id"] in rt_ids and b["id"] not in rt_ids
    assert b["id"] in ps_ids and a["id"] not in ps_ids
    st = client.get("/summary?service=rt", headers=H()).json()["state"]
    assert st["service"] == "rt" and st["workers"] >= 1 and "running" in st
