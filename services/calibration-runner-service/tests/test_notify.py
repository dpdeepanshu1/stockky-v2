"""group447: Telegram message for automatic runs, the last-automatic-run timestamp, and 'only the latest result is kept'. Offline, stdlib only. Plain asserts or pytest."""
from __future__ import annotations

import json
import os
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


def _run(status="success", trigger="schedule", **kw):
    d = {"id": "r1", "kind": "pack", "target": "daily_close", "title": "Daily close pack", "by": "scheduler", "trigger": trigger, "status": status,
         "started_at": 1_760_000_000.0, "finished_at": 1_760_000_318.0, "note": "", "headline": [],
         "jobs": [{"id": "daily_trades", "title": "Daily trades: entry / exit / P&L", "status": "success"},
                  {"id": "service_advice", "title": "Service ON / OFF advice", "status": "attention"},
                  {"id": "tsfm_nightly", "title": "TimesFM nightly cache", "status": "attention"}]}
    d.update(kw)
    return d


def test_message_for_an_automatic_run_with_advice():
    with tempfile.TemporaryDirectory() as t:
        json.dump({"services": {"real-trade-service": {"verdict": "TURN OFF", "why": ["lost -683 over 27 trades"]},
                                "position-stocks-service": {"verdict": "NOT ENOUGH DATA", "why": ["19 trades over 2 day(s)"]}}}, open(os.path.join(t, "service_advice.json"), "w"))
        m = N.build_message(_run("attention"), t)
    txt = m["message"]
    assert m["title"] == "Stockky calibration" and txt.startswith("⚠️ Daily close pack: LOOK")
    assert "Automatic run (timetable)" in txt and "took 5m 18s" in txt and "IST" in txt
    assert "real-trade-service: TURN OFF - lost -683 over 27 trades" in txt and "recommendation only" in txt
    assert len(txt) <= N.MAX_CHARS


def test_blocked_run_says_it_did_not_run_and_manual_run_is_labelled():
    m = N.build_message(_run("blocked", jobs=[], note="Blocked: market hours"), "/nonexistent")
    assert m["message"].startswith("⛔ Daily close pack: BLOCKED") and "It did NOT run." in m["message"] and "Blocked: market hours" in m["message"]
    assert "Manual run by tester" in N.build_message(_run(trigger="manual", by="tester"), "/nonexistent")["message"]


def test_notifier_gating_manual_disabled_and_urgency_and_redaction():
    sent = []
    snd = lambda base, title, text, urgency="normal": sent.append((base, title, text, urgency)) or "sent"   # noqa: E731
    n = N.make_notifier("http://n:8000", True, False, lambda s: s.replace("Daily", "D***"), sender=snd)
    assert n(_run(trigger="manual"), "/x") is None and not sent
    assert n(_run("success"), "/x") == "sent" and sent[-1][3] == "normal" and "D*** close pack" in sent[-1][2]
    n(_run("failed"), "/x")
    assert sent[-1][3] == "high"
    assert N.make_notifier("http://n", False, True, sender=snd)(_run(), "/x") is None
    assert N.make_notifier("http://n", True, True, sender=snd)(_run(trigger="manual"), "/x") == "sent"


class _Fake(BaseHTTPRequestHandler):
    got: list = []
    reply = {"delivered": True}

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        _Fake.got.append((self.path, body))
        data = json.dumps(_Fake.reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_send_posts_to_the_notification_service_telegram_channel():
    srv, base = _serve()
    try:
        _Fake.got.clear()
        _Fake.reply = {"delivered": True}
        assert N.send(base, "T", "hello", "high") == "sent"
        path, body = _Fake.got[-1]
        assert path == "/notification/notify" and body == {"title": "T", "message": "hello", "channel": "telegram", "urgency": "high"}
        assert N.send(base + "/notification", "T", "x") == "sent" and _Fake.got[-1][0] == "/notification/notify"
        _Fake.reply = {"delivered": False, "note": "No notification channel matched"}
        assert N.send(base, "T", "x").startswith("not delivered: No notification channel")
    finally:
        srv.shutdown()
    assert N.send("http://127.0.0.1:1", "T", "x", timeout=1, tries=1).startswith("error: ")


def test_send_retries_a_connection_failure_and_says_why(monkeypatch):
    # group 458: "Telegram error: URLError" - one try, no reason. Now: retried, and the text names the cause and the URL.
    import urllib.error
    calls = []

    def flaky(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"delivered": true}'
        return _R()
    monkeypatch.setattr(N.urllib.request, "urlopen", flaky)
    assert N.send("http://n:8000", "T", "x", tries=3, pause=0) == "sent" and len(calls) == 3
    calls.clear()
    monkeypatch.setattr(N.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(urllib.error.URLError(OSError("Name or service not known"))))
    out = N.send("http://n:8000", "T", "x", tries=2, pause=0)
    assert out.startswith("error: URLError: ") and "Name or service not known" in out and "http://n:8000" in out and "after 2 tries" in out
    monkeypatch.setattr(N.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(urllib.error.HTTPError("u", 401, "no", {}, None)))
    assert N.send("http://n:8000", "T", "x", tries=3, pause=0).startswith("error: HTTP 401")   # a 4xx is final, not retried


def _runner(notify_fn=None, d=None, **kw):
    d = d or tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc, exist_ok=True)
    open(os.path.join(sc, "ok.py"), "w").write("print('PASS all good')\n")
    open(os.path.join(sc, "bad.py"), "w").write("import sys\nprint('FAIL')\nsys.exit(3)\n")
    ctx = dict(python=sys.executable, scripts=sc, bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
               decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t")
    jobs = {"j": J._job("j", "Job J", "ops", "now", "test", lambda c, p: [J._py(c, "ok.py")], [], heavy=kw.pop("heavy", False)),
            "b": J._job("b", "Job B", "ops", "now", "test", lambda c, p: [J._py(c, "bad.py")], [])}
    packs = {"p": {"id": "p", "title": "Pack P", "when": "", "what": "", "jobs": [["j", {}]], "stop_on_failure": False}}
    kw.setdefault("preflight_s", 0)      # group466: these tests use fake service URLs that never answer /health; preflight has its own tests
    r = R.Runner(os.path.join(d, "data"), sc, ctx, jobs, packs, lambda: TOKEN, nice=0, notify_fn=notify_fn, **kw)
    r.start()
    return r, d


def test_scheduled_run_is_announced_and_recorded_manual_is_not():
    calls = []
    notifier = N.make_notifier("http://n", True, False, sender=lambda base, title, text, urgency="normal": calls.append(text) or "sent")
    r, _ = _runner(notifier)
    try:
        man = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        assert not calls and not r.index[man.d["id"]].d.get("notified") and r.state()["last_automatic"] is None
        auto = r.submit("pack", "p", {}, "scheduler", "schedule")
        assert r.drain(30)
        d = r.index[auto.d["id"]].d
        assert d["status"] == "success" and d["notified"] == "sent" and len(calls) == 1 and "Pack P: OK" in calls[0]
        la = r.state()["last_automatic"]
        assert la["id"] == auto.d["id"] and la["status"] == "success" and la["telegram"] == "sent" and la["finished_at"].endswith("+05:30")
        assert r.state()["last_automatic_by_pack"]["p"]["id"] == auto.d["id"]
        assert r.index[auto.d["id"]].public()["notified_at"].endswith("+05:30")
        assert json.load(open(os.path.join(r._run_dir(auto.d["id"]), "meta.json")))["notified"] == "sent"
    finally:
        r.shutdown()


def test_a_failing_notifier_never_changes_the_run():
    def boom(run, d):
        raise RuntimeError("telegram down")
    r, _ = _runner(boom)
    try:
        auto = r.submit("pack", "p", {}, "scheduler", "schedule")
        assert r.drain(30)
        assert r.index[auto.d["id"]].d["status"] == "success" and r.index[auto.d["id"]].d["notified"] == "error: RuntimeError"
    finally:
        r.shutdown()


def test_blocked_scheduled_run_is_announced_too():
    calls = []
    notifier = N.make_notifier("http://n", True, False, sender=lambda base, title, text, urgency="normal": calls.append((text, urgency)) or "sent")
    r, _ = _runner(notifier, heavy=True, guard_market=True, now_fn=lambda: R.datetime(2026, 10, 19, 11, 0, tzinfo=R.IST))
    try:
        b = r.submit("pack", "p", {}, "scheduler", "schedule")
        assert b.d["status"] == "blocked"
        for _ in range(100):
            if calls and (r.state()["last_automatic"] or {}).get("telegram"):
                break
            time.sleep(0.05)
        assert calls and "BLOCKED" in calls[0][0] and "It did NOT run." in calls[0][0] and calls[0][1] == "high"
        la = r.state()["last_automatic"]
        assert la["status"] == "blocked" and la["id"] == b.d["id"]
    finally:
        r.shutdown()


# ------------------------------------------------------------------------------------------ only the latest result is kept
def _ids(r, target):
    return [x["id"] for x in r.list_runs(100, target)]


def test_latest_only_new_result_replaces_the_old_one_and_deletes_its_folder():
    r, _ = _runner(keep_latest_only=True)
    try:
        a = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        time.sleep(1.1)                                   # run ids carry the second
        b = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        assert _ids(r, "j") == [b.d["id"]]
        assert not os.path.isdir(r._run_dir(a.d["id"])) and os.path.isfile(os.path.join(r._run_dir(b.d["id"]), "output.txt"))
        assert r.latest("j")["id"] == b.d["id"]
    finally:
        r.shutdown()


def test_latest_only_a_failed_run_replaces_a_good_one_but_blocked_and_cancelled_do_not():
    r, _ = _runner(keep_latest_only=True)
    try:
        good = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        time.sleep(1.1)
        bad = r.submit("job", "b", {}, "me", "manual")
        assert r.drain(30)
        assert r.index[bad.d["id"]].d["status"] in ("failed", "attention")
        assert _ids(r, "j") == [good.d["id"]] and len(_ids(r, "b")) == 1          # different jobs never replace each other
        # a blocked newer run is kept as a note next to the real result, and a second blocked run replaces the first note
        r.guard_market = True
        r.now_fn = lambda: R.datetime(2026, 10, 19, 11, 0, tzinfo=R.IST)
        r.jobs["j"]["heavy"], r.jobs["j"]["window"] = True, "closed"
        time.sleep(1.1)
        b1 = r.submit("job", "j", {}, "me", "manual")
        assert b1.d["status"] == "blocked" and set(_ids(r, "j")) == {good.d["id"], b1.d["id"]}
        time.sleep(1.1)
        b2 = r.submit("job", "j", {}, "me", "manual")
        assert set(_ids(r, "j")) == {good.d["id"], b2.d["id"]}
        assert r.latest("j")["id"] == good.d["id"]                                # blocked never counts as the latest result
        # once a real run happens again it replaces both the old result and the notes
        r.jobs["j"]["heavy"], r.jobs["j"]["window"] = False, "any"
        time.sleep(1.1)
        c = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        assert _ids(r, "j") == [c.d["id"]]
    finally:
        r.shutdown()


def test_latest_only_off_keeps_the_history_and_never_touches_running_or_queued():
    r, _ = _runner(keep_latest_only=False)
    try:
        a = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        time.sleep(1.1)
        b = r.submit("job", "j", {}, "me", "manual")
        assert r.drain(30)
        assert set(_ids(r, "j")) == {a.d["id"], b.d["id"]} and r.supersede("job", "j") == 0
    finally:
        r.shutdown()
    r2, _ = _runner(keep_latest_only=True)
    try:
        x = r2.submit("job", "j", {}, "me", "manual")
        assert r2.supersede("job", "j") == 0 and x.d["id"] in r2.index              # queued / running runs are never dropped
        assert r2.drain(30)
    finally:
        r2.shutdown()


def test_latest_only_cleans_old_folders_at_start_and_last_automatic_survives_it():
    d = tempfile.mkdtemp()
    r, _ = _runner(d=d, keep_latest_only=False)
    try:
        auto = r.submit("pack", "p", {}, "scheduler", "schedule")
        assert r.drain(30)
        time.sleep(1.1)
        man = r.submit("pack", "p", {}, "me", "manual")                                # newer manual run of the same pack
        assert r.drain(30)
    finally:
        r.shutdown()
    r2, _ = _runner(d=d, keep_latest_only=True)                                          # runner restarted with the new setting
    try:
        assert _ids(r2, "p") == [man.d["id"]] and not os.path.isdir(r2._run_dir(auto.d["id"]))
        la = r2.state()["last_automatic"]
        assert la and la["id"] == auto.d["id"] and la["status"] == "success"            # rebuilt from the old runs on first start
        assert r2.state()["keep_latest_only"] is True
    finally:
        r2.shutdown()
    r3, _ = _runner(d=d, keep_latest_only=True)                                          # and now read from last_automatic.json, the run folder is gone
    try:
        assert r3.state()["last_automatic"]["id"] == auto.d["id"]
    finally:
        r3.shutdown()


def test_restart_mid_run_is_announced_once_and_recorded():
    d = tempfile.mkdtemp()
    r, _ = _runner(d=d)
    r.shutdown()
    rid = "20261019-090000-p-abcd"
    os.makedirs(os.path.join(d, "data", "runs", rid), exist_ok=True)
    meta = {"id": rid, "kind": "pack", "target": "p", "title": "Pack P", "params": {}, "by": "scheduler", "trigger": "schedule", "status": "running",
            "queued_at": time.time() - 60, "started_at": time.time() - 50}
    json.dump(meta, open(os.path.join(d, "data", "runs", rid, "meta.json"), "w"))
    calls = []
    notifier = N.make_notifier("http://n", True, False, sender=lambda base, title, text, urgency="normal": calls.append((text, urgency)) or "sent")
    r2, _ = _runner(notifier, d=d)
    try:
        for _ in range(100):
            if calls and (r2.state()["last_automatic"] or {}).get("telegram"):     # the notifier fires BEFORE the record is written: wait for the record too
                break
            time.sleep(0.05)
        assert len(calls) == 1 and "FAILED" in calls[0][0] and "restarted" in calls[0][0] and calls[0][1] == "high"
        la = r2.state()["last_automatic"]
        assert la and la["id"] == rid and la["status"] == "failed" and la["telegram"] == "sent"
        assert json.load(open(os.path.join(d, "data", "runs", rid, "meta.json")))["status"] == "failed"     # written back
    finally:
        r2.shutdown()
    calls2 = []
    r3, _ = _runner(N.make_notifier("http://n", True, False, sender=lambda *a, **k: calls2.append(a) or "sent"), d=d)
    try:
        time.sleep(0.3)
        assert calls2 == []             # not announced a second time on the next restart
    finally:
        r3.shutdown()


def test_skipped_timetable_slot_is_announced_and_remembered():
    import scheduler as S
    from datetime import datetime
    d = tempfile.mkdtemp()
    seen = []
    on_skip = N.make_skip_notifier("http://n", True, lambda p: "Monday open pack", sender=lambda base, title, text, urgency="normal": seen.append((text, urgency)) or "sent")

    def boom(*a, **k):
        raise ValueError("Monday open pack is already waiting in the queue")
    path = os.path.join(d, "state", "schedule.json")
    s = S.Scheduler(path, J.SCHEDULE_DEFAULT, boom, list(J.PACK_MAP), True, now_fn=lambda: datetime(2026, 10, 19, 8, 52, tzinfo=S.IST), on_skip=on_skip)
    assert s.tick() == ["s_monday_open"]
    assert len(seen) == 1 and "SKIPPED" in seen[0][0] and "already waiting" in seen[0][0] and seen[0][1] == "high"
    ls = s.get()["last_skipped"]
    assert ls["slot"] == "s_monday_open" and ls["telegram"] == "sent" and "already waiting" in ls["reason"]
    assert json.load(open(path))["last_skipped"]["slot"] == "s_monday_open"
    assert s.tick() == [] and len(seen) == 1             # the day is still marked fired: no repeat message
    off = N.make_skip_notifier("http://n", False)
    assert off({"id": "x"}, "r") is None
    bad = N.make_skip_notifier("http://n", True, sender=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert bad({"id": "x", "pack": "p"}, "r").startswith("error:")


def test_latest_only_is_per_settings_not_per_test():
    r, d = _runner(keep_latest_only=True)
    try:
        a = r.submit("job", "j", {}, "me")
        assert r.drain(30)
        a.d["params"] = {"days": 10}                       # pretend this run was made with other options than the next ones
        r._save(a)
        b = r.submit("job", "j", {}, "me")
        assert r.drain(30)
        assert set(_ids(r, "j")) == {a.d["id"], b.d["id"]}   # different settings: the 10-day run is NOT replaced by the default-settings run
        c = r.submit("job", "j", {}, "me")
        assert r.drain(30)
        assert set(_ids(r, "j")) == {a.d["id"], c.d["id"]}   # same settings: the newer one replaces b
    finally:
        r.shutdown()
    r2, _ = _runner(d=d, keep_latest_only=True)              # a restart (supersede_all) keeps one run per settings too
    try:
        assert set(_ids(r2, "j")) == {a.d["id"], c.d["id"]}
        assert r2.supersede("job", "j") == 1                 # no settings given = the old per-test behaviour
        assert _ids(r2, "j") == [c.d["id"]]
    finally:
        r2.shutdown()


class _MiniMonkeyPatch:
    """Just enough of pytest's monkeypatch for the stdlib runner below (setattr + undo)."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            setattr(target, name, old)


if __name__ == "__main__":
    import inspect
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            params = list(inspect.signature(v).parameters)
            unknown = [p for p in params if p != "monkeypatch"]
            if unknown:
                raise SystemExit(f"{k}: stdlib runner cannot supply fixture(s) {unknown} - run under pytest")
            mp = _MiniMonkeyPatch() if params else None
            try:
                v(mp) if mp else v()
            finally:
                if mp:
                    mp.undo()
            print("ok", k)
