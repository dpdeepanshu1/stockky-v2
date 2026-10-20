"""group442: calibration-runner-service engine, catalog and scheduler. Offline, stdlib only (no fastapi needed). Plain asserts or pytest."""
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


def _runner(jobs, packs=None, **kw):
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    kw.setdefault("nice", 0)
    kw.setdefault("preflight_s", 0)      # group466: fake service URLs never answer /health; preflight has its own tests
    r = R.Runner(os.path.join(d, "data"), sc, _ctx(sc), jobs, packs or {}, lambda: TOKEN, **kw)
    r.start()
    return r, d, sc


def _script(sc, name, body):
    p = os.path.join(sc, name)
    open(p, "w").write(body)
    return p


def _job(jid, builder, params=None, **kw):
    return J._job(jid, jid, "ops", "now", "test", builder, params or [], **kw)


def _cmd(ctx, name, *args, **kw):
    return J._py(ctx, name, *args, **kw)


# ------------------------------------------------------------------------------------------ catalog
def test_every_job_builds_and_packs_reference_real_jobs():
    ctx = dict(_ctx("/s"), run="/r", cal="/c", exp="/e")
    for j in J.JOB_LIST:
        p = J.coerce_params(j, {})
        steps = j["builder"](ctx, p)
        assert steps, j["id"]
        for st in steps:
            assert st["kind"] in ("cmd", "http") and st["title"]
            json.dumps(st)
            if st["kind"] == "cmd":
                assert all(isinstance(a, str) for a in st["argv"]), (j["id"], st["argv"])
    for pk in J.PACKS:
        assert pk["jobs"]
        for jid, pp in pk["jobs"]:
            J.coerce_params(J.JOBS[jid], pp)
    assert len(J.JOBS) == len(J.JOB_LIST)
    groups = {g for g, _ in J.GROUPS}
    assert all(j["group"] in groups for j in J.JOB_LIST)
    for e in J.SCHEDULE_DEFAULT:
        assert e["pack"] in J.PACK_MAP


def test_catalog_scripts_exist_in_the_repo():
    scripts = HERE.parent.parent / "scripts"
    if not scripts.is_dir():
        return
    ctx = dict(_ctx(str(scripts)), run="/r", cal="/c", exp="/e")
    for j in J.JOB_LIST:
        for st in j["builder"](ctx, J.coerce_params(j, {})):
            if st["kind"] == "cmd" and len(st["argv"]) > 1 and st["argv"][1].endswith((".py", ".sh")):
                assert os.path.isfile(st["argv"][1]), (j["id"], st["argv"][1])


def test_script_flags_are_accepted_by_the_scripts():
    """--help of every script lists the flags the catalog passes (catches a renamed flag)."""
    import subprocess
    scripts = HERE.parent.parent / "scripts"
    if not scripts.is_dir():
        return
    ctx = dict(_ctx(str(scripts)), run="/r", cal="/c", exp="/e")
    cache = {}
    for j in J.JOB_LIST:
        for st in j["builder"](ctx, J.coerce_params(j, {})):
            if st["kind"] != "cmd" or not st["argv"][1].endswith(".py"):
                continue
            sp = st["argv"][1]
            if sp not in cache:
                cache[sp] = subprocess.run([sys.executable, sp, "--help"], capture_output=True, text=True, timeout=60).stdout
            for a in st["argv"][2:]:
                if a.startswith("globflag:"):          # group470: "globflag:--ab:<pattern>" carries a flag too
                    a = a.split(":", 2)[1]
                if a.startswith("--"):
                    assert a in cache[sp], f"{j['id']}: {os.path.basename(sp)} has no {a}"


def test_params_are_validated_and_bounded():
    j = J.JOBS["fetch_history"]
    assert J.coerce_params(j, {"seed": "11", "limit": 100})["seed"] == 11
    for bad in ({"seed": 0}, {"seed": "x"}, {"limit": 100000}, {"period": "50y"}, {"nope": 1}):
        try:
            J.coerce_params(j, bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        J.coerce_params(J.JOBS["calibration_full"], {"seeds": "7; rm -rf /"})
    except ValueError:
        pass
    else:
        raise AssertionError("shell characters must be refused")
    assert J.coerce_params(J.JOBS["control_snapshot"], {"only_changes": "true"})["only_changes"] is True


# ------------------------------------------------------------------------------------------ engine
def test_success_attention_failed_and_headline_and_redaction():
    holder = {}

    def b(ctx, p):
        s = holder["sc"]
        return [_cmd(ctx, "ok.py", title="ok step"), _cmd(ctx, "verdict.py", title="verdict step")]

    r, d, sc = _runner({})
    holder["sc"] = sc
    _script(sc, "ok.py", f"print('hello'); print('token is {TOKEN}'); print('Authorization: Bearer abcdefghijklmnopqrstuv')\n")
    _script(sc, "verdict.py", "print('Verdict: NOT_ENOUGH_DATA'); raise SystemExit(1)\n")
    _script(sc, "boom.py", "raise SystemExit(7)\n")
    r.jobs = {"a": _job("a", b), "f": _job("f", lambda c, p: [_cmd(c, "boom.py")])}
    ra = r.submit("job", "a")
    rf = r.submit("job", "f")
    assert r.drain(30)
    assert r.index[ra.d["id"]].d["status"] == "attention", r.index[ra.d["id"]].d
    assert r.index[rf.d["id"]].d["status"] == "failed"
    txt = open(os.path.join(d, "data", "runs", ra.d["id"], "output.txt")).read()
    assert "hello" in txt and TOKEN not in txt and "abcdefghijklmnopqrstuv" not in txt and "***" in txt
    assert any("NOT_ENOUGH_DATA" in h for h in r.index[ra.d["id"]].d["headline"])
    assert [s["status"] for s in r.index[ra.d["id"]].d["steps"]] == ["success", "attention"]
    meta = json.load(open(os.path.join(d, "data", "runs", ra.d["id"], "meta.json")))
    assert meta["status"] == "attention" and TOKEN not in json.dumps(meta)
    r.shutdown()


def test_token_reaches_the_script_through_env_not_argv():
    r, d, sc = _runner({})
    _script(sc, "env.py", "import os,sys; print('got', len(os.environ.get('TOKEN','')), 'argv_has_token', any('tok-' in a for a in sys.argv))\n")
    r.jobs = {"e": _job("e", lambda c, p: [_cmd(c, "env.py", env={"TOKEN": "{token}"})])}
    run = r.submit("job", "e")
    assert r.drain(30)
    txt = open(os.path.join(d, "data", "runs", run.d["id"], "output.txt")).read()
    assert f"got {len(TOKEN)} argv_has_token False" in txt, txt
    r.shutdown()


def test_missing_inputs_fail_clearly_and_drop_if_missing():
    r, d, sc = _runner({})
    _script(sc, "show.py", "import sys; print('args', sys.argv[1:])\n")
    cal = os.path.join(d, "data", "calibration_out")
    r.jobs = {"m": _job("m", lambda c, p: [_cmd(c, "show.py", os.path.join(c["cal"], "hist_7"))]),
              "g": _job("g", lambda c, p: [_cmd(c, "show.py", "glob:" + os.path.join(c["cal"], "ab_*", "x.json"))]),
              "o": _job("o", lambda c, p: [_cmd(c, "show.py", "--live", os.path.join(c["run"], "nope.json"), "--k", "v") | {"drop_if_missing": [os.path.join(c["run"], "nope.json")]}])}
    rm, rg, ro = r.submit("job", "m"), r.submit("job", "g"), r.submit("job", "o")
    assert r.drain(30)
    # group446: a missing input is "needs data first" (attention, amber), not a failed job (red)
    assert r.index[rm.d["id"]].d["status"] == "attention" and "input missing" in open(os.path.join(d, "data", "runs", rm.d["id"], "output.txt")).read()
    assert r.index[rm.d["id"]].d["steps"][0]["detail"].startswith("needs data first: ")
    assert r.index[rg.d["id"]].d["status"] == "attention"
    assert r.index[ro.d["id"]].d["status"] == "success"
    assert "'--k', 'v'" in open(os.path.join(d, "data", "runs", ro.d["id"], "output.txt")).read() and "--live" not in open(os.path.join(d, "data", "runs", ro.d["id"], "output.txt")).read().split("args")[1]
    os.makedirs(os.path.join(cal, "hist_7"))
    open(os.path.join(cal, "hist_7", "A.json"), "w").write("{}")
    rm2 = r.submit("job", "m")
    assert r.drain(30) and r.index[rm2.d["id"]].d["status"] == "success"
    r.shutdown()


def test_timeout_and_cancel_stop_the_process():
    r, d, sc = _runner({})
    _script(sc, "slow.py", "import time; print('start', flush=True); time.sleep(60)\n")
    r.jobs = {"s": _job("s", lambda c, p: [_cmd(c, "slow.py")], timeout_s=2), "c": _job("c", lambda c, p: [_cmd(c, "slow.py")], timeout_s=60)}
    t0 = time.time()
    rs = r.submit("job", "s")
    assert r.drain(30)
    assert r.index[rs.d["id"]].d["status"] == "timeout" and time.time() - t0 < 25
    rc = r.submit("job", "c")
    for _ in range(100):
        if r.current is rc and "start" in r.tail(rc.d["id"])["text"]:
            break
        time.sleep(0.1)
    assert r.cancel(rc.d["id"])
    assert r.drain(30)
    assert r.index[rc.d["id"]].d["status"] == "cancelled"
    r.shutdown()


def test_queue_runs_one_at_a_time_and_rejects_duplicates():
    r, d, sc = _runner({})
    _script(sc, "w.py", "import time; time.sleep(1)\n")
    r.jobs = {"w": _job("w", lambda c, p: [_cmd(c, "w.py")]), "x": _job("x", lambda c, p: [_cmd(c, "w.py")], [J.P("n", "n", "int", 1, 1, 9)])}
    r.submit("job", "w")
    r.submit("job", "x", {"n": 1})
    try:
        r.submit("job", "x", {"n": 1})
    except ValueError as e:
        assert "already waiting" in str(e)
    else:
        raise AssertionError("duplicate not refused")
    r.submit("job", "x", {"n": 2})
    assert len(r.state()["queued"]) >= 1
    assert r.drain(60)
    done = sorted(r.list_runs(), key=lambda x: x["started_at"] or "")
    for a, b in zip(done, done[1:]):
        assert a["finished_at"] <= b["started_at"], "two runs overlapped"
    r.shutdown()


def test_out_file_keeps_json_clean_and_copies_it_to_the_run():
    r, d, sc = _runner({})
    _script(sc, "js.py", "import sys,json; print('progress', file=sys.stderr); print(json.dumps({'a': 1}))\n")
    target = os.path.join(d, "data", "calibration_out", "backtest.json")
    r.jobs = {"j": _job("j", lambda c, p: [_cmd(c, "js.py", out_file=target)])}
    run = r.submit("job", "j")
    assert r.drain(30)
    assert json.load(open(target)) == {"a": 1}
    assert os.path.isfile(os.path.join(d, "data", "runs", run.d["id"], "backtest.json"))
    assert "progress" in open(os.path.join(d, "data", "runs", run.d["id"], "output.txt")).read()
    r.shutdown()


def test_output_cap():
    r, d, sc = _runner({}, max_output=2000)
    _script(sc, "big.py", "print('x' * 100)\n" * 1 + "for i in range(200): print('line', i, 'y' * 50)\n")
    r.jobs = {"b": _job("b", lambda c, p: [_cmd(c, "big.py")])}
    run = r.submit("job", "b")
    assert r.drain(30)
    p = os.path.join(d, "data", "runs", run.d["id"], "output.txt")
    assert os.path.getsize(p) < 4000 and "output cap" in open(p).read()
    r.shutdown()


class _H(BaseHTTPRequestHandler):
    seen = []
    polls = {"n": 0}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        _H.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path.startswith("/status"):
            _H.polls["n"] += 1
            return self._send(200, {"status": "running" if _H.polls["n"] < 3 else "done", "n": _H.polls["n"]})
        if self.path.startswith("/bad"):
            return self._send(500, {"err": "x"})
        self._send(200, {"ok": True, "echo": TOKEN})

    def do_POST(self):
        _H.seen.append((self.command, self.path, self.headers.get("Authorization")))
        self._send(200, {"started": True})


def test_http_steps_auth_save_poll_and_failure():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    r, d, sc = _runner({})
    saved = os.path.join(d, "data", "exports", "x_{date}.json")
    r.jobs = {"h": _job("h", lambda c, p: [J._http("get", "GET", base + "/a", auth=True, save=saved), J._http("post", "POST", base + "/b"),
                                          J._http("poll", "GET", base + "/status", poll={"key": "status", "until_not": ["running"], "every_s": 0.05, "max_s": 20})]),
              "bad": _job("bad", lambda c, p: [J._http("bad", "GET", base + "/bad")]),
              "soft": _job("soft", lambda c, p: [J._http("bad", "GET", base + "/bad") | {"soft": True}, J._http("ok", "GET", base + "/a")]),
              "dead": _job("dead", lambda c, p: [J._http("dead", "GET", "http://127.0.0.1:1/x")])}
    rh, rb, rs, rd = r.submit("job", "h"), r.submit("job", "bad"), r.submit("job", "soft"), r.submit("job", "dead")
    assert r.drain(60)
    assert r.index[rh.d["id"]].d["status"] == "success", r.index[rh.d["id"]].d
    assert r.index[rb.d["id"]].d["status"] == "failed" and r.index[rd.d["id"]].d["status"] == "failed"
    assert r.index[rs.d["id"]].d["status"] == "success"
    assert all(a == "Bearer " + TOKEN for _, _, a in _H.seen)
    txt = open(os.path.join(d, "data", "runs", rh.d["id"], "output.txt")).read()
    assert TOKEN not in txt and "poll: status=running" in txt and "poll: status=done" in txt
    files = os.listdir(os.path.join(d, "data", "exports"))
    assert any(f.startswith("x_20") and f.endswith(".json") for f in files), files
    r.shutdown()
    srv.shutdown()


def test_pack_runs_jobs_in_order_and_worst_status_wins():
    r, d, sc = _runner({})
    _script(sc, "a.py", "print('A')\n")
    _script(sc, "b.py", "print('FAIL gate'); raise SystemExit(1)\n")
    _script(sc, "c.py", "print('C')\n")
    r.jobs = {n: _job(n, (lambda nm: lambda c, p: [_cmd(c, nm + ".py")])(n)) for n in "abc"}
    r.packs = {"pk": {"id": "pk", "title": "pack", "jobs": [["a", {}], ["b", {}], ["c", {}]]}}
    run = r.submit("pack", "pk")
    assert r.drain(60)
    dd = r.index[run.d["id"]].d
    assert [j["id"] for j in dd["jobs"]] == ["a", "b", "c"] and dd["status"] == "attention"
    txt = open(os.path.join(d, "data", "runs", run.d["id"], "output.txt")).read()
    assert txt.index("JOB a") < txt.index("JOB b") < txt.index("JOB c")
    zp = r.zip_run(run.d["id"], os.path.join(d, "z.zip"))
    import zipfile
    assert "output.txt" in zipfile.ZipFile(zp).namelist()
    r.shutdown()


def test_market_hours_guard_and_confirm():
    mon_open = datetime(2026, 10, 19, 11, 0, tzinfo=IST)
    sat = datetime(2026, 10, 17, 11, 0, tzinfo=IST)
    assert R.market_open_now(mon_open) and not R.market_open_now(sat)
    assert not R.market_open_now(datetime(2026, 10, 19, 16, 0, tzinfo=IST)) and R.market_open_now(datetime(2026, 10, 19, 9, 0, tzinfo=IST))
    clock = {"t": mon_open}
    r, d, sc = _runner({}, now_fn=lambda: clock["t"])
    _script(sc, "n.py", "print('ran')\n")
    r.jobs = {"heavy": _job("heavy", lambda c, p: [_cmd(c, "n.py")], window="closed", heavy=True), "live": _job("live", lambda c, p: [_cmd(c, "n.py")], window="open")}
    b = r.submit("job", "heavy")
    assert b.d["status"] == "blocked" and "Market hours" in b.d["note"]
    ok = r.submit("job", "heavy", confirm=True)
    assert r.drain(30) and r.index[ok.d["id"]].d["status"] == "success"
    clock["t"] = sat
    assert r.submit("job", "live").d["status"] == "blocked"
    assert r.submit("job", "heavy").d["status"] in ("queued", "running", "success")
    assert r.drain(30)
    r.shutdown()


def test_file_path_blocks_traversal_and_history_survives_restart():
    r, d, sc = _runner({})
    _script(sc, "n.py", "print('ran')\n")
    r.jobs = {"n": _job("n", lambda c, p: [_cmd(c, "n.py")])}
    run = r.submit("job", "n")
    assert r.drain(30)
    rid = run.d["id"]
    assert r.file_path(rid, "output.txt") and not r.file_path(rid, "../../../etc/passwd") and not r.file_path(rid, "../" + rid + "/output.txt/../../x")
    r.shutdown()
    r2 = R.Runner(os.path.join(d, "data"), sc, _ctx(sc), r.jobs, {}, lambda: TOKEN, nice=0)
    assert r2.get(rid)["status"] == "success" and r2.latest("n")["id"] == rid


def test_prune_keeps_newest_and_leaves_active():
    r, d, sc = _runner({}, keep_runs=2)
    _script(sc, "n.py", "print('ran')\n")
    r.jobs = {"n": _job("n", lambda c, p: [_cmd(c, "n.py")], [J.P("k", "k", "int", 0, 0, 99)])}
    for k in range(4):
        r.submit("job", "n", {"k": k})
        assert r.drain(30)
        time.sleep(0.02)
    assert r.prune() == 2 and len(r.list_runs()) == 2
    r.shutdown()


# ------------------------------------------------------------------------------------------ scheduler
def test_due_slots_weekday_window_first_week_and_once_per_day():
    e = [{"id": "a", "pack": "p", "days": [0], "time": "08:50", "enabled": True}, {"id": "b", "pack": "p", "days": [5], "time": "13:00", "enabled": True, "first_week_only": True},
         {"id": "c", "pack": "p", "days": [0], "time": "08:50", "enabled": False}]
    mon = datetime(2026, 10, 19, 8, 55, tzinfo=IST)
    assert [x["id"] for x in S.due(e, {}, mon)] == ["a"]
    assert S.due(e, {"a": "2026-10-19"}, mon) == []
    assert S.due(e, {}, datetime(2026, 10, 19, 9, 5, tzinfo=IST)) == []            # a missed slot is not caught up
    assert S.due(e, {}, datetime(2026, 10, 19, 8, 49, tzinfo=IST)) == []
    assert [x["id"] for x in S.due(e, {}, datetime(2026, 10, 3, 13, 2, tzinfo=IST))] == ["b"]   # Sat 3 Oct
    assert S.due(e, {}, datetime(2026, 10, 17, 13, 2, tzinfo=IST)) == []                       # Sat 17 Oct: not first week


def test_scheduler_tick_persists_and_respects_the_master_switch():
    d = tempfile.mkdtemp()
    got = []
    clock = {"t": datetime(2026, 10, 19, 8, 52, tzinfo=IST)}
    s = S.Scheduler(os.path.join(d, "state", "schedule.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a), list(J.PACK_MAP), False, now_fn=lambda: clock["t"])
    assert s.tick() == [] and got == []
    s.put(True, None)
    assert s.tick() == ["s_monday_open"] and got[0][1] == "monday_open"
    assert s.tick() == []
    s2 = S.Scheduler(os.path.join(d, "state", "schedule.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a), list(J.PACK_MAP), False, now_fn=lambda: clock["t"])
    assert s2.get()["enabled"] is True and s2.tick() == []
    open(os.path.join(d, "state", "schedule.json"), "w").write("{broken")
    s3 = S.Scheduler(os.path.join(d, "state", "schedule.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: None, list(J.PACK_MAP), False)
    assert s3.get()["entries"][0]["id"] == "s_monday_open"


def test_schedule_validation():
    ids = list(J.PACK_MAP)
    ok = [{"id": "x1", "pack": "daily_close", "days": [0, 1], "time": "16:05", "enabled": True}]
    assert S.validate_entries(ok, ids)[0]["time"] == "16:05"
    for bad in ([{"id": "x", "pack": "nope", "days": [0], "time": "16:05"}], [{"id": "x", "pack": "daily_close", "days": [7], "time": "16:05"}],
                [{"id": "x", "pack": "daily_close", "days": [0], "time": "25:00"}], [{"id": "x y", "pack": "daily_close", "days": [0], "time": "16:05"}], "nope",
                [ok[0], ok[0]]):
        try:
            S.validate_entries(bad, ids)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_main_py_wiring_by_text():
    src = (HERE / "main.py").read_text()
    for route in ('"/health"', '"/catalog"', '"/runs"', '"/runs/{rid}/tail"', '"/runs/{rid}/download"', '"/runs/{rid}/zip"', '"/summary"', '"/schedule"', '"/auth/session"'):
        assert route in src, route
    assert src.count("Depends(require_admin)") >= 10
    health = src.split('@app.get("/health")')[1].split("@app.")[0]
    assert "require_admin" not in health


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
