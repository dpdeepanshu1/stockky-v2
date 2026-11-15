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
    assert len(c["jobs"]) >= 30 and len(c["packs"]) == 12 and "builder" not in c["jobs"][0]
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


def test_agent_routes_need_login_and_never_show_a_key(client):
    """E553: /agent/status and /agent/run are admin-only; the status shows key flags, never a key; an unknown mode is refused."""
    assert client.get("/agent/status").status_code == 401
    assert client.post("/agent/run", json={}).status_code == 401
    assert client.post("/agent/resume").status_code == 401, "round 536: resume is admin-only too"
    st = client.get("/agent/status", headers=H())
    assert st.status_code == 200, st.text
    d = st.json()
    assert d["mode"] in ("off", "report", "propose") and set(d["keys"]) == {"gemini", "groq"} and all(isinstance(v, bool) for v in d["keys"].values())
    assert "BROKER_STOP_ENABLED" not in d["allow_list"] and "KRONOS_GATE_MODE" in d["allow_list"]
    assert client.post("/agent/run", headers=H(), json={"mode": "apply"}).status_code == 400


def test_self_improve_routes_need_login_validate_and_never_set_a_switch(client):
    """Round 541 (S1): /agent/loop, /agent/history, /agent/settings are admin-only; settings are bounded; propose needs confirm; a change is saved and can be reset.
    Not run in the authoring sandbox (no fastapi): the same logic is covered offline by test_round541_self_improve.py."""
    for method, path in (("get", "/agent/loop"), ("get", "/agent/history"), ("get", "/agent/settings"), ("post", "/agent/settings")):
        assert getattr(client, method)(path).status_code == 401, path
    loop = client.get("/agent/loop", headers=H())
    assert loop.status_code == 200, loop.text
    d = loop.json()
    assert d["banner"]["state"] in ("off", "dry_run", "live") and [c["id"] for c in d["chain"]] == ["daily_close", "agent", "queued_tests", "autocalib"]
    assert all(c["state"] in ("done", "waiting", "skipped", "failed", "not_due", "unknown") for c in d["chain"]) and d["schedule"]["slots"]
    hist = client.get("/agent/history?limit=5", headers=H())
    assert hist.status_code == 200 and "days" in hist.json() and client.get("/agent/history?limit=0", headers=H()).status_code == 422
    assert client.get("/agent/settings", headers=H()).json()["bounds"]["max_ideas"] == [1, 10]
    url = "/agent/settings"
    assert client.post(url, headers=H(), json={"mode": "apply"}).status_code == 400
    assert client.post(url, headers=H(), json={"mode": "propose"}).status_code == 400, "propose needs confirm"
    assert client.post(url, headers=H(), json={"max_ideas": 11}).status_code == 400 and client.post(url, headers=H(), json={"run_after": "10:00"}).status_code == 400
    ok = client.post(url, headers=H(), json={"max_ideas": 4})
    assert ok.status_code == 200 and ok.json()["values"]["max_ideas"] == 4 and ok.json()["source"]["max_ideas"] == "saved" and ok.json()["audit"][0]["by"] == "tester"
    back = client.post(url, headers=H(), json={"reset": True})
    assert back.status_code == 200 and back.json()["saved"] == {}


def test_weekly_and_csv_routes_are_admin_only_read_only_and_well_formed(client):
    """Round 545 (S5): /agent/weekly and /agent/history.csv need login; the CSV is a file download with a header row; neither sets anything."""
    for path in ("/agent/weekly", "/agent/history.csv"):
        assert client.get(path).status_code == 401, path
    w = client.get("/agent/weekly", headers=H())
    assert w.status_code == 200 and len(w.json()["days"]) == 7 and "summary" in w.json()
    c = client.get("/agent/history.csv", headers=H())
    assert c.status_code == 200 and c.headers["content-type"].startswith("text/csv") and "attachment" in c.headers["content-disposition"]
    assert c.text.splitlines()[0].startswith("date,at_ist,mode,trigger") and client.get("/agent/history.csv?limit=0", headers=H()).status_code == 422


def test_autocalib_settings_route_audits_who_changed_what(client):
    """Round 543 (S3): PUT /autocalib/settings needs login, still validates, and a real change shows up in GET /autocalib/status as one audit line by the caller."""
    url = "/autocalib/settings"
    assert client.put(url, json={"dry_run": True}).status_code == 401
    assert client.put(url, headers=H(), json={"pinned": ["BROKER_STOP_ENABLED"]}).status_code == 400
    before = len(client.get("/autocalib/status?coverage=false", headers=H()).json()["audit"])
    cur = client.get("/autocalib/status?coverage=false", headers=H()).json()["settings"]["dry_run"]
    ok = client.put(url, headers=H(), json={"dry_run": not cur})
    assert ok.status_code == 200 and ok.json()["dry_run"] is (not cur)
    st = client.get("/autocalib/status?coverage=false", headers=H()).json()
    assert len(st["audit"]) == before + 1 and st["audit"][0]["by"] == "tester" and st["audit"][0]["changes"]["dry_run"] == {"from": cur, "to": not cur}
    client.put(url, headers=H(), json={"dry_run": cur})                  # put it back
    assert "coverage" not in st and "protected" in st
