"""group466: heavy jobs get 6 hours, an undelivered Telegram message is kept and retried, an automatic run waits for the stack, a failed run names what failed.
Offline, stdlib only. Plain asserts or pytest."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402
import notify as N  # noqa: E402
import runner as R  # noqa: E402

TOKEN = "tok-ABCDEF-1234567890"
HEAVY_IDS = ["ab_report", "structure_backtest", "legacy_gate", "entry_rules", "p8_retest", "ml_v2", "calibration_full", "tsfm_probe", "kronos_backtest"]


def test_heavy_jobs_get_six_hours_by_default():
    assert J.HEAVY_S == 6 * 3600
    for jid in HEAVY_IDS:
        assert J.JOBS[jid]["timeout_s"] == 6 * 3600, jid
    assert J.JOBS["ab_report"]["timeout_s"] > 7205          # the run that timed out at 7205 s (290/355 symbols)
    assert J.JOBS["services_health"]["timeout_s"] == 180    # short jobs keep their short limit


def test_heavy_limit_comes_from_the_environment_and_never_goes_below_one_hour(monkeypatch):
    monkeypatch.setenv("RUNNER_HEAVY_TIMEOUT_S", "28800")
    assert importlib.reload(J).HEAVY_S == 28800
    monkeypatch.setenv("RUNNER_HEAVY_TIMEOUT_S", "60")
    assert importlib.reload(J).HEAVY_S == 3600
    monkeypatch.setenv("RUNNER_HEAVY_TIMEOUT_S", "abc")
    assert importlib.reload(J).HEAVY_S == 6 * 3600
    monkeypatch.delenv("RUNNER_HEAVY_TIMEOUT_S")
    importlib.reload(J)


def test_resumable_jobs_get_checkpoint_args_and_resume_flag():
    ctx = dict(python="py", scripts="/s", cal="/cal", run="/run")
    for jid, name in (("ab_report", "ab"), ("legacy_gate", "legacy"), ("entry_rules", "entry"), ("p8_retest", "p8"), ("ml_v2", "ml_v2")):
        assert J.JOBS[jid]["resumable"], jid
        argv = J.JOBS[jid]["builder"](ctx, J.coerce_params(J.JOBS[jid], {}))[0]["argv"]
        assert "--checkpoint" in argv and argv[argv.index("--checkpoint") + 1] == f"/cal/ckpt/{name}_7" and "--resume" not in argv, jid
        argv = J.JOBS[jid]["builder"](dict(ctx, resume=True), J.coerce_params(J.JOBS[jid], {}))[0]["argv"]
        assert "--resume" in argv, jid
    assert not J.JOBS["structure_backtest"]["resumable"]
    assert all("resumable" in j for j in J.public_catalog()["jobs"])


def test_outbox_keeps_and_resends_then_drops_old(tmp_path):
    ob = N.Outbox(str(tmp_path / "ob"), max_age_s=3600)
    assert ob.add("T", "hello", "high") and ob.pending() == 1
    calls = []
    assert ob.flush("http://n", lambda *a, **k: (calls.append(a), "error: URLError: refused")[1]) == {"sent": 0, "dropped": 0, "left": 1}   # still down: kept
    assert ob.flush("http://n", lambda *a, **k: (calls.append(a), "sent")[1])["sent"] == 1 and ob.pending() == 0
    assert calls[-1][2].startswith("(delayed) hello") and calls[-1][3] == "high"
    ob.add("T", "old", "normal")
    f = next(Path(ob.dir).glob("ob_*.json"))
    doc = json.loads(f.read_text())
    doc["ts"] = time.time() - 7200
    f.write_text(json.dumps(doc))
    assert ob.flush("http://n", lambda *a, **k: "sent") == {"sent": 0, "dropped": 1, "left": 0}
    for i in range(7):
        N.Outbox(ob.dir, max_items=3).add("T", str(i))
    assert ob.pending() == 3


def test_outbox_drops_a_message_the_service_refuses_for_good(tmp_path):
    ob = N.Outbox(str(tmp_path / "ob"))
    ob.add("T", "x")
    assert ob.flush("http://n", lambda *a, **k: "error: HTTP 401 from http://n")["left"] == 0     # a 4xx never gets better by waiting
    ob.add("T", "y")
    assert ob.flush("http://n", lambda *a, **k: "not delivered: No notification channel")["left"] == 0


def test_notifier_saves_an_unreachable_send_and_says_so(tmp_path):
    ob = N.Outbox(str(tmp_path / "ob"))
    run = {"id": "r", "kind": "pack", "target": "daily_close", "title": "Daily close pack", "trigger": "schedule", "status": "failed", "by": "scheduler",
           "started_at": 1_760_000_000.0, "finished_at": 1_760_000_100.0, "jobs": [], "steps": [], "headline": []}
    notify = N.make_notifier("http://n", True, False, None, sender=lambda *a, **k: "error: URLError: [Errno 111] Connection refused reaching http://n", outbox=ob)
    res = notify(run, str(tmp_path))
    assert res.startswith("error: URLError") and "will be sent again" in res and ob.pending() == 1
    ok = N.make_notifier("http://n", True, False, None, sender=lambda *a, **k: "sent", outbox=ob)
    assert ok(run, str(tmp_path)) == "sent" and ob.pending() == 1          # a delivered message is not queued
    skip = N.make_skip_notifier("http://n", True, None, None, sender=lambda *a, **k: "error: URLError: timed out", outbox=ob)
    assert "will be sent again" in skip({"id": "s", "pack": "daily_close", "time": "16:05"}, "queue full") and ob.pending() == 2


def test_flusher_thread_delivers_when_the_service_comes_back(tmp_path):
    ob = N.Outbox(str(tmp_path / "ob"))
    ob.add("T", "m")
    state = {"up": False, "n": 0}

    def sender(*a, **k):
        state["n"] += 1
        return "sent" if state["up"] else "error: URLError: refused"
    t = N.start_outbox_flusher(ob, "http://n", every_s=1.0, sender=sender)
    assert t.daemon
    time.sleep(1.3)
    assert ob.pending() == 1
    state["up"] = True
    for _ in range(40):
        if not ob.pending():
            break
        time.sleep(0.1)
    assert ob.pending() == 0


def test_message_names_what_failed():
    run = {"id": "r", "kind": "pack", "target": "daily_close", "title": "Daily close pack", "trigger": "schedule", "status": "failed", "by": "scheduler",
           "started_at": 1_760_000_000.0, "finished_at": 1_760_000_100.0, "jobs": [{"id": "export_records", "title": "Save records", "status": "failed"}], "headline": [],
           "steps": [{"job": "export_records", "n": 1, "title": "Save closed real-trade records", "status": "failed", "detail": "exit 2"},
                     {"job": "x", "n": 1, "title": "ok step", "status": "success", "detail": ""}]}
    m = N.build_message(run, "/nonexistent")["message"]
    assert "What failed:" in m and "Save closed real-trade records: exit 2" in m and "ok step" not in m


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _runner(preflight_s, ctx_extra):
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    open(os.path.join(sc, "ok.py"), "w").write("print('PASS')\n")
    ctx = dict(python=sys.executable, scripts=sc, bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
               decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t")
    ctx.update(ctx_extra)
    jobs = {"j": J._job("j", "Job J", "ops", "now", "test", lambda c, p: [J._py(c, "ok.py")], [])}
    r = R.Runner(os.path.join(d, "data"), sc, ctx, jobs, {}, lambda: TOKEN, nice=0, preflight_s=preflight_s)
    r.start()
    return r


def test_automatic_run_waits_for_services_manual_run_does_not():
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    up = f"http://127.0.0.1:{srv.server_port}"
    try:
        r = _runner(30, {k: up for k in ("gw", "rt", "ps", "md", "decision", "notify")})
        a = r.submit("job", "j", {}, "scheduler", "schedule")
        assert r.drain(30)
        txt = open(os.path.join(r._run_dir(a.d["id"]), "output.txt")).read()
        assert "-- services ready" in txt and a.d["status"] == "success"
        r2 = _runner(2, {})          # nothing answers: waits only the preflight time, then runs anyway
        t0 = time.time()
        b = r2.submit("job", "j", {}, "scheduler", "schedule")
        assert r2.drain(40)
        txt = open(os.path.join(r2._run_dir(b.d["id"]), "output.txt")).read()
        assert "running anyway" in txt and b.d["status"] == "success" and time.time() - t0 < 30
        c = r2.submit("job", "j", {}, "admin", "manual")
        assert r2.drain(30)
        assert "waiting for services" not in open(os.path.join(r2._run_dir(c.d["id"]), "output.txt")).read()
    finally:
        srv.shutdown()


def test_timeout_line_says_how_to_raise_the_limit():
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    open(os.path.join(sc, "slow.py"), "w").write("import time; time.sleep(60)\n")
    ctx = dict(python=sys.executable, scripts=sc, bars="/x", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an", decision="http://dp",
               notify="http://n", kronos="http://k", tsfm="http://t")
    r = R.Runner(os.path.join(d, "data"), sc, ctx, {"s": J._job("s", "S", "ops", "now", "t", lambda c, p: [J._py(c, "slow.py")], [], timeout_s=1)}, {}, lambda: TOKEN, nice=0)
    r.start()
    x = r.submit("job", "s")
    assert r.drain(30)
    assert x.d["status"] == "timeout" and "RUNNER_HEAVY_TIMEOUT_S" in open(os.path.join(r._run_dir(x.d["id"]), "output.txt")).read()


def test_timeout_kills_pool_workers_and_resume_flag_reaches_the_script():
    """The whole process group dies at the limit (a pool worker used to survive), and a Resume run prints the RESUME line and passes ctx['resume']."""
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    pidf = os.path.join(d, "child.pid")
    open(os.path.join(sc, "pool.py"), "w").write(
        "import os, subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({pidf!r}, 'w').write(str(c.pid))\n"
        "print('args', sys.argv[1:], flush=True)\n"
        "time.sleep(120)\n")
    ctx = dict(python=sys.executable, scripts=sc, bars="/x", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an", decision="http://dp",
               notify="http://n", kronos="http://k", tsfm="http://t")
    job = J._job("s", "S", "ops", "now", "t", lambda c, p: [J._py(c, "pool.py", *(["--resume"] if c.get("resume") else []))], [], timeout_s=2, resumable=True)
    r = R.Runner(os.path.join(d, "data"), sc, ctx, {"s": job}, {}, lambda: TOKEN, nice=0)
    r.start()
    x = r.submit("job", "s", resume=True)
    assert r.drain(40)
    txt = open(os.path.join(r._run_dir(x.d["id"]), "output.txt")).read()
    assert x.d["status"] == "timeout" and "# RESUME:" in txt and "args ['--resume']" in txt and "press Resume" in txt
    pid = int(open(pidf).read())
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    if alive:                       # a zombie of an already killed child still answers kill(0): check /proc state
        try:
            alive = open(f"/proc/{pid}/stat").read().split(") ")[1][0] != "Z"
        except OSError:
            alive = False
    assert not alive, "the pool worker survived the timeout"


def test_resume_request_reaches_the_run_record_and_the_api_model():
    r = _runner(0, {})
    a = r.submit("job", "j", {}, "admin", "manual", resume=True)
    assert r.drain(30) and a.d["resume"] is True and a.public()["resume"] is True
    b = r.submit("job", "j", {}, "admin", "manual")
    assert r.drain(30) and b.d["resume"] is False
    src = (HERE / "main.py").read_text()
    assert "resume: bool = False" in src and "resume=req.resume" in src


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
