"""E115: the runner side of the Time Machine live gate: the check script, the job + pack + 09:20 slot, the control client call and the Market Weather 09:14 lock trigger."""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("tm_gate_check", REPO / "scripts" / "time_machine_gate_check.py")
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)


def gate_doc(mode="live", threads=True, checks=30, rejects=3):
    return {"mode": mode, "auto_enabled": True, "timer": {"threads_running": threads}, "counters": {"gate_checks": checks, "rejects": rejects, "no_opinion": 2},
            "refusal_rate": round(rejects / checks, 3) if checks else None, "cached_verdicts": 5}


def fake(docs, scans=None):
    def call(method, url):
        if method == "POST":
            if scans is not None:
                scans.append(url)
            return 200, {"ok": True, "targets": 2, "queued": 2, "ran": 0}
        for key, doc in docs.items():
            if key in url:
                return (200, doc) if doc is not None else (0, None)
        return 404, None
    return call


def test_pass_when_both_run_and_the_refusal_rate_is_sane():
    lines, code = chk.run("http://rt", "http://ps", fake({"rt": gate_doc(), "ps": gate_doc()}))
    assert code == 0 and lines[-1].startswith("TM GATE: PASS")


def test_scan_is_sent_to_both_services_with_the_runner_reason():
    sent = []
    chk.run("http://rt", "http://ps", fake({"rt": gate_doc(), "ps": gate_doc()}, sent), scan=True, wait=True)
    assert len(sent) == 2 and all("/time-machine/scan?reason=runner&wait=true" in u for u in sent)


def test_fail_cases():
    assert chk.run("http://rt", "http://ps", fake({"rt": gate_doc(mode="off"), "ps": gate_doc()}))[1] == 1
    assert chk.run("http://rt", "http://ps", fake({"rt": gate_doc(threads=False), "ps": gate_doc()}))[1] == 1
    assert chk.run("http://rt", "http://ps", fake({"rt": gate_doc(checks=40, rejects=30), "ps": gate_doc()}))[1] == 1
    assert chk.run("http://rt", "http://ps", fake({"rt": None, "ps": gate_doc()}))[1] == 1
    assert chk.run("http://rt", "http://ps", fake({"rt": gate_doc(mode="log"), "ps": gate_doc(mode="log")}), expect_mode="any")[1] == 0


def test_not_enough_data_is_not_a_failure():
    lines, code = chk.run("http://rt", "http://ps", fake({"rt": gate_doc(checks=2, rejects=0), "ps": gate_doc()}))
    assert code == 0 and "NOT_ENOUGH_DATA" in lines[-1]


def test_job_pack_and_schedule_are_registered():
    import jobs as J
    assert "tm_gate_check" in J.JOBS and "time_machine_open" in J.PACK_MAP
    assert any(e["id"] == "s_tm_open" and e["time"] == "09:20" and e["pack"] == "time_machine_open" and e["enabled"] for e in J.SCHEDULE_DEFAULT)
    assert ["tm_gate_check", {"scan": True}] in J.PACK_MAP["monday_open"]["jobs"]
    job = J.JOBS["tm_gate_check"]
    assert job["window"] == "any" and not job["heavy"]


def test_control_client_posts_the_scan():
    import autocalib as AC
    calls = []
    cc = AC.ControlClient({AC.RT: "http://rt", AC.PS: "http://ps"}, lambda: "t")
    cc._call = lambda service, method, path, body=None: calls.append((service, method, path)) or {"ok": True}
    cc.time_machine_scan(AC.PS, "weather lock")
    assert calls == [(AC.PS, "POST", "/time-machine/scan?reason=weather%20lock")]


def test_weather_lock_calls_after_lock_and_a_failure_never_stops_it():
    src = (ROOT / "weather.py").read_text()
    i = src.index("self.after_lock is not None")
    assert 'stage == "lock"' in src[i:i + 120] and "except Exception" in src[i:i + 300]
    main = (ROOT / "main.py").read_text()
    assert "after_lock=lambda" in main and "time_machine_scan(s," in main


def test_the_new_switches_are_protected_from_auto_calibration():
    import autocalib as AC
    protected = getattr(AC, "PROTECTED", {})
    for k in ("TM_GATE_MODE", "TM_GATE_BLOCK", "TM_PICK_NUDGE_PTS", "TM_AUTO_ENABLED"):
        assert k in protected, k
