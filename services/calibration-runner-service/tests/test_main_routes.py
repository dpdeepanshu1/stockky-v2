"""group442: HTTP routes of calibration-runner-service (pytest + TestClient; skipped when fastapi is not installed).
Not run in the authoring sandbox (no fastapi there): CI runs it. The engine itself is covered by test_runner_engine.py."""
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


def test_health_is_open_and_everything_else_needs_login(client):
    assert client.get("/health").status_code == 200
    for path in ("/catalog", "/runs", "/summary", "/schedule", "/auth/session"):
        assert client.get(path).status_code == 401, path
    assert client.get("/catalog", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_catalog_summary_and_session(client):
    c = client.get("/catalog", headers=H()).json()
    assert len(c["jobs"]) >= 30 and len(c["packs"]) == 11 and "builder" not in c["jobs"][0]
    assert client.get("/auth/session", headers=H()).json()["user"] == "tester"
    assert "jobs" in client.get("/summary", headers=H()).json()


def test_run_a_job_and_download_its_txt(client):
    r = client.post("/runs", headers=H(), json={"kind": "job", "target": "control_snapshot", "params": {}})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    for _ in range(100):
        d = client.get(f"/runs/{rid}", headers=H()).json()
        if d["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert d["status"] in ("failed", "attention", "success")      # no trading services in the test: failing to reach them is fine, it must still finish
    txt = client.get(f"/runs/{rid}/download", headers=H())
    assert txt.status_code == 200 and ("control_snapshot" in txt.text or "Switch snapshot" in txt.text)
    assert client.get(f"/runs/{rid}/zip", headers=H()).status_code == 200
    assert client.get(f"/runs/{rid}/download?file=../../etc/passwd", headers=H()).status_code == 404
    assert client.get(f"/runs/{rid}/tail?offset=0", headers=H()).json()["size"] > 0


def test_bad_requests(client):
    assert client.post("/runs", headers=H(), json={"kind": "job", "target": "nope"}).status_code == 404
    assert client.post("/runs", headers=H(), json={"kind": "job", "target": "fetch_history", "params": {"seed": 0}}).status_code == 400
    assert client.post("/runs", headers=H(), json={"kind": "job", "target": "fetch_history", "params": {"x": 1}}).status_code == 400


def test_schedule_roundtrip(client):
    s = client.get("/schedule", headers=H()).json()
    assert s["enabled"] is True        # E126: the timetable ships on (a file saved earlier with enabled=false still wins)
    s2 = client.put("/schedule", headers=H(), json={"enabled": False}).json()
    assert s2["enabled"] is False
    assert client.put("/schedule", headers=H(), json={"enabled": True}).json()["enabled"] is True
    bad = client.put("/schedule", headers=H(), json={"entries": [{"id": "x", "pack": "nope", "days": [0], "time": "09:00"}]})
    assert bad.status_code == 400
    client.put("/schedule", headers=H(), json={"enabled": True})
