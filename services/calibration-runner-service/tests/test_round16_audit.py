"""Round 16 audit of calibration-runner-service: each test below failed on the round-15 code and passes now. Offline, stdlib only (the two route tests need fastapi and skip without it)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402
import runner as R  # noqa: E402
import scheduler as S  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = "tok-ABCDEF-1234567890"


def _ctx(scripts: str) -> dict:
    return dict(python=sys.executable, scripts=scripts, bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
                decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t")


def _runner(jobs, data=None, **kw):
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc, exist_ok=True)
    kw.setdefault("nice", 0)
    kw.setdefault("preflight_s", 0)
    r = R.Runner(data or os.path.join(d, "data"), sc, _ctx(sc), jobs, {}, lambda: TOKEN, **kw)
    return r, d, sc


def _job(jid, builder, params=None, **kw):
    return J._job(jid, jid, "ops", "now", "test", builder, params or [], **kw)


# ----------------------------------------------------------------------------------------------------- parameters
def test_float_parameters_are_typed_and_bounded():
    setup = J.JOBS["setup_backtest"]
    ok = J.coerce_params(setup, {"cost_pct": "0.25"})
    assert ok["cost_pct"] == 0.25 and isinstance(ok["cost_pct"], float)
    for bad in (-5, "nan", "inf", 1.5, "abc", None):
        with pytest.raises(ValueError):
            J.coerce_params(setup, {"cost_pct": bad})
    short = J.JOBS["short_history_replay"]
    with pytest.raises(ValueError):
        J.coerce_params(short, {"stop_pct": 0})                  # below its 0.2 minimum: used to reach the script's command line
    assert J.coerce_params(short, {})["stop_pct"] == 1.0         # defaults unchanged


def test_int_parameters_reject_fractions_and_infinity():
    j = J.JOBS["daily_trades"]
    assert J.coerce_params(j, {"days": 7.0})["days"] == 7
    for bad in (3.7, float("inf"), "x"):
        with pytest.raises(ValueError):
            J.coerce_params(j, {"days": bad})


def test_heavy_timeout_survives_inf(monkeypatch):
    monkeypatch.setenv("RUNNER_HEAVY_TIMEOUT_S", "inf")
    assert J._heavy_timeout() == 6 * 3600
    monkeypatch.setenv("RUNNER_HEAVY_TIMEOUT_S", "120")
    assert J._heavy_timeout() == 3600                            # never below 1 h


# ----------------------------------------------------------------------------------------------------- worker thread
def test_worker_thread_survives_a_failing_save_and_keeps_running_jobs():
    r, d, sc = _runner({})
    r.jobs = {"ok": _job("ok", lambda c, p: [J._http("dead", "GET", "http://127.0.0.1:1/x") | {"soft": True}])}
    real_save, real_exec = r._save, r._execute
    state = {"boom": True}

    def save(run):
        if state["boom"] and run.d["status"] == "failed":
            raise OSError(28, "No space left on device")
        return real_save(run)

    def execute(run):
        if state["boom"]:
            raise RuntimeError("internal")
        return real_exec(run)

    r._save, r._execute = save, execute
    r.start()
    first = r.submit("job", "ok")
    assert r.drain(30)
    assert r.index[first.d["id"]].d["status"] == "failed"        # the in-memory record still says what happened
    assert all(t.is_alive() for t in r.threads), "a worker thread died on the failed save"
    state["boom"] = False
    second = r.submit("job", "ok")
    assert r.drain(30)
    assert r.index[second.d["id"]].d["status"] == "success"
    r.shutdown()


def test_after_run_exception_does_not_end_the_worker():
    r, d, sc = _runner({})
    r.jobs = {"ok": _job("ok", lambda c, p: [J._http("dead", "GET", "http://127.0.0.1:1/x") | {"soft": True}])}
    r._after_run = lambda run: (_ for _ in ()).throw(RuntimeError("hook"))
    r.start()
    r.submit("job", "ok")
    assert r.drain(30)
    assert all(t.is_alive() for t in r.threads)
    r.shutdown()


def test_a_meta_file_that_is_not_a_run_does_not_stop_the_service_starting():
    d = tempfile.mkdtemp()
    data = os.path.join(d, "data")
    for name, body in (("a", "[]"), ("b", "null"), ("c", "{}"), ("d", '{"id": "x"}'), ("e", "{broken")):
        os.makedirs(os.path.join(data, "runs", name))
        open(os.path.join(data, "runs", name, "meta.json"), "w").write(body)
    good = {"id": "g1", "kind": "job", "target": "t", "status": "success", "queued_at": 1.0}
    os.makedirs(os.path.join(data, "runs", "g1"))
    open(os.path.join(data, "runs", "g1", "meta.json"), "w").write(json.dumps(good))
    r, _, _ = _runner({}, data=data)
    assert list(r.index) == ["g1"]


# ----------------------------------------------------------------------------------------------------- tail
def test_tail_never_cuts_a_multibyte_character_at_the_chunk_edge():
    r, d, sc = _runner({})
    run = R.Run("rid", "job", "x", "t", {}, "", "")
    run.d["status"] = "success"
    r.index["rid"] = run
    os.makedirs(r._run_dir("rid"))
    text = ("₹" * 100_000) + "\n" + ("→ ok ₹ 1,234.50\n" * 20_000)       # 3-byte characters: the 256 KB edge falls inside one
    open(os.path.join(r._run_dir("rid"), "output.txt"), "w", encoding="utf-8").write(text)
    got, off, hops = "", 0, 0
    while True:
        t = r.tail("rid", off)
        got += t["text"]
        hops += 1
        if t["offset"] == off or t["offset"] >= t["size"]:
            off = t["offset"]
            break
        off = t["offset"]
    assert hops >= 2 and off == len(text.encode("utf-8"))
    assert got == text and "\ufffd" not in got


# ----------------------------------------------------------------------------------------------------- scheduler
def test_scheduler_still_submits_the_pack_when_the_state_file_cannot_be_saved():
    d = tempfile.mkdtemp()
    got = []
    clock = {"t": datetime(2026, 10, 19, 8, 52, tzinfo=IST)}
    s = S.Scheduler(os.path.join(d, "state", "schedule.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a), list(J.PACK_MAP), True, now_fn=lambda: clock["t"])

    def full_disk():
        raise OSError(28, "No space left on device")

    s._save = full_disk
    assert s.tick() == ["s_monday_open"]
    assert got and got[0][1] == "monday_open"                    # used to raise before submit: the pack never ran and the slot was marked done
    assert s.tick() == []                                        # and it is not submitted twice the same day


# ----------------------------------------------------------------------------------------------------- remote job that failed
def test_poll_failed_only_blames_this_runs_job():
    poll = {"key": "state", "until_not": ["running"], "attention_on": ["failed"], "fresh_key": "started"}
    now = time.time()
    fresh = json.dumps({"state": "failed", "started": now - 5})
    old = json.dumps({"state": "failed", "started": now - 86400})
    assert R.Runner._poll_failed(poll, "failed", fresh, now) == "failed"
    assert R.Runner._poll_failed(poll, "failed", old, now) == ""             # last week's crash is not this run's
    assert R.Runner._poll_failed(poll, "done", fresh, now) == ""
    assert R.Runner._poll_failed(poll, "failed", "not json", now) == ""
    assert R.Runner._poll_failed(None, "failed", fresh, now) == ""
    assert R.Runner._poll_failed({"key": "state", "until_not": []}, "failed", fresh, now) == ""


class _JobH(BaseHTTPRequestHandler):
    state = {"state": "failed", "started": 0.0, "error": "ValueError: boom"}

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps(_JobH.state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def test_a_crashed_remote_backtest_is_attention_not_a_green_success():
    srv = HTTPServer(("127.0.0.1", 0), _JobH)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    r, d, sc = _runner({})
    poll = {"key": "state", "until_not": ["running"], "every_s": 0.05, "max_s": 10, "attention_on": ["failed"], "fresh_key": "started"}
    r.jobs = {"w": _job("w", lambda c, p: [J._http("wait", "GET", base + "/latest-job", poll=poll) | {"soft": True}])}
    r.start()
    _JobH.state = {"state": "failed", "started": time.time(), "error": "ValueError: boom"}
    a = r.submit("job", "w")
    assert r.drain(30)
    assert r.index[a.d["id"]].d["status"] == "attention", r.index[a.d["id"]].d
    _JobH.state = {"state": "failed", "started": time.time() - 7 * 86400, "error": "old"}
    b = r.submit("job", "w")
    assert r.drain(30)
    assert r.index[b.d["id"]].d["status"] == "success"                       # an old failed job (the recorded-chain replay was skipped) is not blamed
    _JobH.state = {"state": "done", "started": time.time(), "trades": 3}
    c = r.submit("job", "w")
    assert r.drain(30)
    assert r.index[c.d["id"]].d["status"] == "success"
    r.shutdown()
    srv.shutdown()


def test_fno_backtests_wait_steps_ask_for_the_failure_flag():
    ctx = dict(_ctx("/s"), run="/r", cal="/c", exp="/e", fno="http://fno")
    steps = J.b_fno_backtests(ctx, J.coerce_params(J.JOBS["fno_backtests"], {}))
    waits = [s for s in steps if s["title"].startswith("Wait:")]
    assert len(waits) == 3 and all(w["poll"]["attention_on"] == ["failed"] and w["poll"]["fresh_key"] == "started" for w in waits)


# ----------------------------------------------------------------------------------------------------- zips
def test_zip_survives_a_file_removed_while_zipping(monkeypatch):
    r, d, sc = _runner({})
    run = R.Run("rid", "job", "x", "t", {}, "", "")
    run.d["status"] = "success"
    r.index["rid"] = run
    os.makedirs(r._run_dir("rid"))
    open(os.path.join(r._run_dir("rid"), "output.txt"), "w").write("hello")
    open(os.path.join(r._run_dir("rid"), "gone.txt"), "w").write("x")
    real = R.Runner.files

    def files(self, rid):
        out = real(self, rid)
        os.remove(os.path.join(self._run_dir(rid), "gone.txt"))     # replaced by a newer result between the listing and the write
        return out

    monkeypatch.setattr(R.Runner, "files", files)
    import zipfile
    zp = r.zip_run("rid", os.path.join(d, "z.zip"))
    assert zipfile.ZipFile(zp).namelist() == ["output.txt"]


def test_files_listing_skips_a_file_that_vanishes(monkeypatch):
    r, d, sc = _runner({})
    os.makedirs(r._run_dir("rid"))
    open(os.path.join(r._run_dir("rid"), "output.txt"), "w").write("hello")
    open(os.path.join(r._run_dir("rid"), "b.txt"), "w").write("x")
    real = os.path.getsize

    def size(p):
        if p.endswith("b.txt"):
            raise FileNotFoundError(p)
        return real(p)

    monkeypatch.setattr(R.os.path, "getsize", size)
    assert [f["name"] for f in r.files("rid")] == ["output.txt"]


# ----------------------------------------------------------------------------------------------------- the capital split switch stays with the person
def test_capital_alloc_mode_is_protected_like_capital_lend_mode():
    import autocalib as AC
    assert "CAPITAL_ALLOC_MODE" in AC.PROTECTED and "CAPITAL_LEND_MODE" in AC.PROTECTED
    assert all(rule.key != "CAPITAL_ALLOC_MODE" for rule in AC.RULES)


# ----------------------------------------------------------------------------------------------------- routes (need fastapi)
def _route_client():
    pytest.importorskip("fastapi")
    pytest.importorskip("jwt")
    from fastapi.testclient import TestClient
    data = tempfile.mkdtemp()
    os.environ.setdefault("SESSION_SECRET", "route-test-secret-0123456789")
    os.environ.setdefault("RUNNER_DATA_DIR", data)
    os.environ.setdefault("RUNNER_SCRIPTS_DIR", str(HERE.parent.parent / "scripts"))
    os.environ.setdefault("RUNNER_GUARD_MARKET_HOURS", "0")
    import main
    import auth
    return TestClient(main.app), {"Authorization": "Bearer " + auth.mint_service_token("tester", 600)}


def test_download_zips_leave_nothing_behind_in_tmp(monkeypatch):
    client, hdr = _route_client()
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr(tempfile, "tempdir", tmpdir)
    with client as c:
        for _ in range(3):
            resp = c.get("/results/latest.zip", headers=hdr)
            assert resp.status_code == 200 and resp.content[:2] == b"PK"
        assert c.get("/runs/nope/zip", headers=hdr).status_code == 404
    assert [f for f in os.listdir(tmpdir) if f.endswith(".zip")] == [], "temp zip files were left behind"
