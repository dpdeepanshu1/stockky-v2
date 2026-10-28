"""E235: worker pool + rt / ps run tags. Offline, stdlib only (plain asserts or pytest)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import jobs as J  # noqa: E402
import runner as R  # noqa: E402,F401
from test_runner_engine import _cmd, _job, _runner, _script  # noqa: E402

SLEEP = ("import sys,time,os\nopen(sys.argv[1],'a').write('start %s %f\\n' % (sys.argv[2], time.time()))\n"
         "time.sleep(float(sys.argv[3]))\nopen(sys.argv[1],'a').write('end %s %f\\n' % (sys.argv[2], time.time()))\n")


def _spans(log):
    st, en = {}, {}
    for ln in open(log):
        k, n, t = ln.split()
        (st if k == "start" else en)[n] = float(t)
    return st, en


def _overlap(st, en, a, b):
    return st[a] < en[b] and st[b] < en[a]


def _setup(workers=4, **kw):
    r, d, sc = _runner({}, workers=workers, **kw)
    _script(sc, "nap.py", SLEEP)
    return r, d, sc, os.path.join(d, "log.txt")


def _nap(jid, log, secs, **kw):
    return _job(jid, lambda c, p: [_cmd(c, "nap.py", log, jid, secs)], **kw)


def test_independent_runs_overlap():
    r, d, sc, log = _setup(4)
    r.jobs = {k: _nap(k, log, 1.0) for k in "abcd"}
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert _overlap(st, en, "a", "b") and _overlap(st, en, "c", "d")
    r.shutdown()


def test_one_worker_is_serial():
    r, d, sc, log = _setup(1)
    r.jobs = {k: _nap(k, log, 0.3) for k in "ab"}
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert not _overlap(st, en, "a", "b")
    r.shutdown()


def test_writer_and_reader_of_same_file_do_not_overlap():
    r, d, sc, log = _setup(4)
    exp = os.path.join(d, "data", "exports")
    os.makedirs(exp, exist_ok=True)
    open(os.path.join(exp, "rt_latest.json"), "w").write("{}")

    def job(jid, flag):
        return _job(jid, lambda c, p: [_cmd(c, "nap.py", log, jid, 0.6, flag, os.path.join(exp, "rt_latest.json"))])
    r.jobs = {"w": job("w", "--out"), "r1": job("r1", "--in"), "r2": job("r2", "--in")}
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert not _overlap(st, en, "w", "r1") and not _overlap(st, en, "w", "r2")
    assert _overlap(st, en, "r1", "r2")
    r.shutdown()


def test_solo_job_runs_alone():
    r, d, sc, log = _setup(4)
    J.SOLO_JOBS = frozenset(set(J.SOLO_JOBS) | {"s"})
    r.jobs = {"a": _nap("a", log, 0.5), "s": _nap("s", log, 0.5), "b": _nap("b", log, 0.5)}
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert not _overlap(st, en, "s", "a") and not _overlap(st, en, "s", "b")
    r.shutdown()


def test_heavy_cap():
    r, d, sc, log = _setup(4, max_heavy=1)
    r.jobs = {"h1": _nap("h1", log, 0.5, heavy=True, window="any"),
              "h2": _nap("h2", log, 0.5, heavy=True, window="any"),
              "l": _nap("l", log, 0.5)}
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert not _overlap(st, en, "h1", "h2")
    r.shutdown()


def test_service_tag_in_id():
    r, d, sc, log = _setup(2)
    r.jobs = {"a": _nap("a", log, 0.1)}
    rt = r.submit("job", "a", service="rt")
    ps = r.submit("job", "a", service="ps")
    sh = r.submit("job", "a", trigger="schedule")
    assert "-rt-" in rt.d["id"] and "-ps-" in ps.d["id"] and "-rt-" not in sh.d["id"] and "-ps-" not in sh.d["id"]
    assert r.drain(30)
    ids = lambda svc: {x["id"] for x in r.list_runs(60, "", svc)}  # noqa: E731
    assert rt.d["id"] in ids("rt") and ps.d["id"] not in ids("rt") and sh.d["id"] in ids("rt")
    assert ps.d["id"] in ids("ps") and rt.d["id"] not in ids("ps")
    try:
        r.submit("job", "a", service="xx")
        assert False
    except ValueError:
        pass
    r.shutdown()


def test_output_names_the_tab():
    r, d, sc, log = _setup(2)
    r.jobs = {"a": _nap("a", log, 0.05)}
    a = r.submit("job", "a", service="ps")
    assert r.drain(30)
    txt = open(os.path.join(d, "data", "runs", a.d["id"], "output.txt")).read()
    assert "tab Position Stocks" in txt
    r.shutdown()


def test_duplicate_in_queue_scoped_by_service():
    r, d, sc, log = _setup(1)
    r.jobs = {"slow": _nap("slow", log, 0.8), "a": _nap("a", log, 0.1)}
    r.submit("job", "slow")
    time.sleep(0.2)
    r.submit("job", "a", service="rt")
    r.submit("job", "a", service="ps")
    try:
        r.submit("job", "a", service="rt")
        assert False
    except ValueError as e:
        assert "already waiting" in str(e)
    assert r.drain(30)
    r.shutdown()


def test_supersede_keeps_one_result_per_service():
    r, d, sc, log = _setup(2, keep_latest_only=True)
    r.jobs = {"a": _nap("a", log, 0.05)}
    for svc in ("rt", "ps", "rt", "ps"):
        r.submit("job", "a", service=svc)
        assert r.drain(30)
    assert sorted(x["service"] for x in r.list_runs(60, "a")) == ["ps", "rt"]
    r.shutdown()


def test_latest_is_per_tab():
    r, d, sc, log = _setup(2)
    r.jobs = {"a": _nap("a", log, 0.05)}
    rt = r.submit("job", "a", service="rt")
    assert r.drain(30)
    ps = r.submit("job", "a", service="ps")
    assert r.drain(30)
    assert r.latest("a", "rt")["id"] == rt.d["id"]
    assert r.latest("a", "ps")["id"] == ps.d["id"]
    r.shutdown()


def test_cancel_stops_only_that_run():
    r, d, sc, log = _setup(3)
    r.jobs = {"a": _nap("a", log, 5.0), "b": _nap("b", log, 0.6)}
    ra = r.submit("job", "a")
    rb = r.submit("job", "b")
    time.sleep(0.4)
    assert r.cancel(ra.d["id"])
    assert r.drain(30)
    assert ra.d["status"] == "cancelled" and rb.d["status"] == "success"
    r.shutdown()


def test_state_reports_all_running():
    r, d, sc, log = _setup(3)
    r.jobs = {"a": _nap("a", log, 0.8), "b": _nap("b", log, 0.8)}
    ra = r.submit("job", "a", service="rt")
    time.sleep(0.1)
    r.submit("job", "b", service="ps")
    time.sleep(0.4)
    s = r.state("rt")
    assert [x["id"] for x in s["running"]] == [ra.d["id"]] and s["active_total"] == 2 and s["workers"] == 3
    assert r.drain(30)
    r.shutdown()


def test_queued_run_says_why_it_waits():
    r, d, sc, log = _setup(1)
    r.jobs = {"a": _nap("a", log, 0.8), "b": _nap("b", log, 0.1)}
    r.submit("job", "a")
    time.sleep(0.2)
    r.submit("job", "b")
    q = r.state()["queued"]
    assert q and q[0]["waiting_for"]
    assert r.drain(30)
    r.shutdown()


def test_workers_can_be_changed_and_survive_a_restart():
    r, d, sc, log = _setup(1)
    r.jobs = {k: _nap(k, log, 1.0) for k in "ab"}
    assert r.set_workers(99) == 8 and r.set_workers(0) == 1
    r.set_workers(2)
    for k in r.jobs:
        r.submit("job", k)
    assert r.drain(30)
    st, en = _spans(log)
    assert _overlap(st, en, "a", "b")
    r.shutdown()
    r2 = R.Runner(os.path.join(d, "data"), sc, r.ctx_base, {}, {}, lambda: "tok-ABCDEF-1234567890", nice=0, preflight_s=0, workers=1)
    assert r2.workers == 2


def test_derive_locks_for_real_catalog():
    ctx = dict(python="py", scripts="/s", bars="/b", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw",
               analysis="http://an", decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t",
               run="/d/runs/x", cal="/d/calibration_out", exp="/d/exports")
    roots = {"cal": "/d/calibration_out", "exp": "/d/exports"}
    lk = {j["id"]: J.derive_locks(j, j["builder"](ctx, J.coerce_params(j, {})), roots) for j in J.JOB_LIST}
    assert "exp/rt_latest.json" in lk["export_records"]["excl"] and "exp/rt_latest.json" in lk["records_report"]["shared"]
    assert "cal/hist_7" in lk["fetch_history"]["excl"] and "cal/hist_7" in lk["ab_report"]["shared"]
    assert lk["dhan_sell_probe"]["solo"] and lk["latency_save"]["solo"] and not lk["records_report"]["solo"]


def test_wiring_check_runs_alone():
    ctx = dict(python="py", scripts="/s", bars="/b", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw",
               analysis="http://an", decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t",
               run="/d/runs/x", cal="/d/calibration_out", exp="/d/exports")
    j = J.JOBS["wiring_check"]
    lk = J.derive_locks(j, j["builder"](ctx, J.coerce_params(j, {})), {"cal": "/d/calibration_out", "exp": "/d/exports"})
    assert lk["solo"], "wiring_check writes an overlay on the live services"


def test_main_py_wiring_by_text():
    m = open(HERE.parent / "main.py").read()
    assert "workers=config.WORKERS" in m and "service=_svc(req.service)" in m and "_r().state(sv)" in m


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            print("ok", k)
            n += 1
    print(n, "passed")
