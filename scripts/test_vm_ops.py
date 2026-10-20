"""group323: vm_ops.sh, stats_loop.sh and monday_check.py (no pytest fixtures: plain tempfile, stub crontab / curl)."""
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import monday_check as M  # noqa: E402

VM_OPS = os.path.join(HERE, "vm_ops.sh")
STATS = os.path.join(HERE, "stats_loop.sh")
UUID = "123e4567-e89b-12d3-a456-426614174000"


def _bash(script, env=None, tz=None):
    e = dict(os.environ)
    if tz:
        e["TZ"] = tz
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30)


def _stubs(tmp, crontab_text, curl_rc=0):
    """A fake crontab (file-backed) and a fake curl; returns env overrides."""
    store = os.path.join(tmp, "cron.txt")
    with open(store, "w") as f:
        f.write(crontab_text)
    ct = os.path.join(tmp, "crontab")
    with open(ct, "w") as f:
        f.write(f'#!/bin/bash\nif [ "$1" = "-l" ]; then cat "{store}"; else cat > "{store}"; fi\n')
    cu = os.path.join(tmp, "curl")
    with open(cu, "w") as f:
        f.write(f'#!/bin/bash\necho "$@" >> "{tmp}/curl_calls"\nexit {curl_rc}\n')
    for p in (ct, cu):
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return {"CRONTAB_BIN": ct, "CURL_BIN": cu, "HB_OK_FILE": os.path.join(tmp, "hb_ok"),
            "ENV_FILE": os.path.join(tmp, "no.env")}, store


PRUNE = "0 2 * * 0 docker builder prune -f --filter until=168h >/dev/null 2>&1; docker image prune -f >/dev/null 2>&1"
PLACEHOLDER = "*/5 * * * * curl -fsS -m 10 --retry 2 https://hc-ping.com/YOUR-UUID >/dev/null 2>&1"


# ------------------------------------------------------------------ ping URL validation
def test_valid_ping_url_accepts_real_and_refuses_placeholders():
    good = [f"https://hc-ping.com/{UUID}", f"https://hc-ping.com/{UUID}/fail", "https://hc.example.org/ping/abc123"]
    bad = ["", "https://hc-ping.com/YOUR-UUID", "http://hc-ping.com/" + UUID, "https://hc-ping.com/not-a-uuid",
           "https://hc-ping.com/", "hc-ping.com/" + UUID, "https://hc.example.org/ping/<uuid>", "https://x.org/xxxx"]
    for u in good:
        r = subprocess.run(["bash", "-c", 'source "$1"; valid_ping_url "$2"', "x", VM_OPS, u], capture_output=True, text=True)
        assert r.returncode == 0, u
    for u in bad:
        r = subprocess.run(["bash", "-c", 'source "$1"; valid_ping_url "$2"', "x", VM_OPS, u], capture_output=True, text=True)
        assert r.returncode == 1, u


# ------------------------------------------------------------------ heartbeat
def test_heartbeat_replaces_the_placeholder_line_and_keeps_other_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, PRUNE + "\n" + PLACEHOLDER + "\n")
        r = subprocess.run(["bash", VM_OPS, "heartbeat", f"https://hc-ping.com/{UUID}"], capture_output=True, text=True,
                           env={**os.environ, **env})
        assert r.returncode == 0, r.stdout + r.stderr
        text = open(store).read()
        assert "YOUR-UUID" not in text
        assert PRUNE in text
        assert text.count(UUID) == 1 and text.count("# stockky-heartbeat") == 1
        assert "*/5 * * * *" in text and "touch" in text
        assert "test ping accepted" in r.stdout
        assert os.path.exists(env["HB_OK_FILE"])


def test_heartbeat_twice_leaves_one_line():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, PRUNE + "\n")
        for _ in range(2):
            subprocess.run(["bash", VM_OPS, "heartbeat", f"https://hc-ping.com/{UUID}"], capture_output=True, text=True,
                           env={**os.environ, **env})
        text = open(store).read()
        assert text.count("# stockky-heartbeat") == 1 and text.count(PRUNE) == 1


def test_heartbeat_refuses_placeholder_and_changes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, PRUNE + "\n" + PLACEHOLDER + "\n")
        before = open(store).read()
        r = subprocess.run(["bash", VM_OPS, "heartbeat", "https://hc-ping.com/YOUR-UUID"], capture_output=True, text=True,
                           env={**os.environ, **env})
        assert r.returncode == 1
        assert "placeholder" in r.stdout
        assert open(store).read() == before


def test_heartbeat_reads_url_from_env_file():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, "")
        envfile = os.path.join(tmp, ".env")
        with open(envfile, "w") as f:
            f.write(f'OTHER=1\nHC_PING_URL="https://hc-ping.com/{UUID}"\n')
        env["ENV_FILE"] = envfile
        env2 = {k: v for k, v in os.environ.items() if k != "HC_PING_URL"}
        r = subprocess.run(["bash", VM_OPS, "heartbeat"], capture_output=True, text=True, env={**env2, **env})
        assert r.returncode == 0, r.stdout
        assert UUID in open(store).read()


def test_heartbeat_reports_a_rejected_test_ping():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, "", curl_rc=22)
        r = subprocess.run(["bash", VM_OPS, "heartbeat", f"https://hc-ping.com/{UUID}"], capture_output=True, text=True,
                           env={**os.environ, **env})
        assert r.returncode == 1 and "REJECTED" in r.stdout


def test_status_flags_the_placeholder_as_not_working():
    with tempfile.TemporaryDirectory() as tmp:
        env, _ = _stubs(tmp, PLACEHOLDER + "\n")
        r = subprocess.run(["bash", "-c", 'source "$1"; hb_status', "x", VM_OPS], capture_output=True, text=True,
                           env={**os.environ, **env})
        assert r.returncode == 1 and "NOT working" in r.stdout


def test_heartbeat_remove_drops_only_heartbeat_lines():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, PRUNE + "\n")
        subprocess.run(["bash", VM_OPS, "heartbeat", f"https://hc-ping.com/{UUID}"], capture_output=True, text=True,
                       env={**os.environ, **env})
        subprocess.run(["bash", VM_OPS, "heartbeat-remove"], capture_output=True, text=True, env={**os.environ, **env})
        text = open(store).read()
        assert PRUNE in text and "stockky-heartbeat" not in text


# ------------------------------------------------------------------ schedule (IST -> server clock)
def test_ist_to_cron_on_a_utc_server():
    for hhmm, want in (("08:40", "10 3"), ("09:30", "0 4"), ("10:15", "45 4")):
        r = subprocess.run(["bash", "-c", 'source "$1"; ist_to_cron "$2"', "x", VM_OPS, hhmm], capture_output=True, text=True,
                           env={**os.environ, "TZ": "UTC"})
        assert r.stdout.strip() == want, (hhmm, r.stdout, r.stderr)


def test_ist_to_cron_refuses_when_the_date_would_change():
    r = subprocess.run(["bash", "-c", 'source "$1"; ist_to_cron "$2"', "x", VM_OPS, "09:30"], capture_output=True, text=True,
                       env={**os.environ, "TZ": "America/Los_Angeles"})
    assert r.returncode != 0


def test_schedule_adds_three_tagged_weekday_jobs_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        env, store = _stubs(tmp, PRUNE + "\n")
        e = {**os.environ, **env, "TZ": "UTC"}
        for _ in range(2):
            r = subprocess.run(["bash", VM_OPS, "schedule"], capture_output=True, text=True, env=e)
            assert r.returncode == 0, r.stdout + r.stderr
        lines = [ln for ln in open(store).read().splitlines() if "# stockky-ops" in ln]
        assert len(lines) == 3
        assert any(ln.startswith("10 3 * * 1-5 ") and "sampler start" in ln for ln in lines)
        assert any(ln.startswith("0 4 * * 1-5 ") and "--label 0930" in ln for ln in lines)
        assert any(ln.startswith("45 4 * * 1-5 ") and "--label 1015" in ln for ln in lines)
        assert PRUNE in open(store).read()


# ------------------------------------------------------------------ sampler report
def test_stats_loop_report_gives_peaks_per_container():
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "s.log")
        with open(log, "w") as f:
            f.write("2026-10-12T09:30:00+05:30|HOST|load1=0.5|mem_used_mib=2000|mem_avail_mib=5000|swap_used_mib=10\n"
                    "2026-10-12T09:30:00+05:30|api-gateway|cpu=12.5|mem_mib=300\n"
                    "2026-10-12T09:31:00+05:30|HOST|load1=1.5|mem_used_mib=3100|mem_avail_mib=3900|swap_used_mib=50\n"
                    "2026-10-12T09:31:00+05:30|api-gateway|cpu=40.0|mem_mib=650\n"
                    "2026-10-12T09:31:00+05:30|market-data|cpu=5.0|mem_mib=900\n")
        r = subprocess.run(["bash", STATS, "--report", log], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert "peak used 3100 MiB at 2026-10-12T09:31:00+05:30" in r.stdout
        assert "lowest available 3900 MiB" in r.stdout and "peak swap 50 MiB" in r.stdout and "(2 samples)" in r.stdout
        assert "api-gateway" in r.stdout and "peak mem    650 MiB" in r.stdout and "peak cpu 40.0%" in r.stdout
        assert "market-data" in r.stdout and "900 MiB" in r.stdout


def test_stats_loop_report_missing_file():
    r = subprocess.run(["bash", STATS, "--report", "/nonexistent/x.log"], capture_output=True, text=True)
    assert r.returncode == 1 and "no log" in r.stdout


def test_to_mib_units():
    for v, want in (("512MiB", "512"), ("1.5GiB", "1536"), ("2048KiB", "2"), ("100MiB", "100")):
        r = subprocess.run(["bash", "-c", 'eval "$(sed -n "/^to_mib()/,/^}/p" "$1")"; to_mib "$2"', "x", STATS, v], capture_output=True, text=True)
        assert r.stdout.strip() == want, (v, r.stdout, r.stderr)


# ------------------------------------------------------------------ monday_check.py
def _fake(routes):
    def fetch(url, timeout=8.0):
        for key, val in routes.items():
            if url.endswith(key) or key in url:
                return val
        return 0, None, "URLError"
    return fetch


def test_market_is_open_now_clock_rule():
    mon_1030_ist = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)
    sun_1030_ist = datetime(2026, 10, 11, 5, 0, tzinfo=timezone.utc)
    mon_0900_ist = datetime(2026, 10, 12, 3, 30, tzinfo=timezone.utc)
    assert M.market_is_open_now(mon_1030_ist) and not M.market_is_open_now(sun_1030_ist) and not M.market_is_open_now(mon_0900_ist)


def test_session_verdict_1015_must_be_open():
    now = datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc)          # Mon 10:30 IST: market hours (group470: the check is skipped outside them)
    ok = M.check_session("1015", _fake({"/market/session": (200, {"status": "open", "day": "d"}, "")}), now)
    assert ok[0][0] == "OK"
    bad = M.check_session("1015", _fake({"/market/session": (200, {"status": "unknown"}, "")}), now)
    assert bad[0][0] == "FAIL" and "expected `open`" in bad[0][1]
    wk = M.check_session("1015", _fake({"/market/session": (200, {"status": "unknown", "reason": "weekend"}, "")}), now)
    assert wk[0][0] == "INFO"
    down = M.check_session("1015", _fake({}), now)
    assert down[0][0] == "FAIL"
    early = M.check_session("0930", _fake({"/market/session": (200, {"status": "pending"}, "")}), now)
    assert early[0][0] == "INFO"


def test_session_verdict_is_skipped_not_failed_when_the_market_is_closed():
    """group470: the 19 Oct run at 16:11 IST turned `unknown` into a FAIL on the 1015 label."""
    after_close = datetime(2026, 10, 19, 10, 41, tzinfo=timezone.utc)   # Mon 16:11 IST
    sunday = datetime(2026, 10, 11, 5, 0, tzinfo=timezone.utc)
    for now in (after_close, sunday):
        r = M.check_session("1015", _fake({"/market/session": (200, {"status": "unknown"}, "")}), now)
        assert len(r) == 1 and r[0][0] == "INFO" and "market is closed" in r[0][1]


def test_quote_age_over_20s_fails_only_in_market_hours():
    old = _fake({"/quote/RELIANCE": (200, {"age_s": 45.2, "source": "dhan"}, "")})
    assert M.check_quote_age(True, old)[0][0] == "FAIL"
    assert M.check_quote_age(False, old)[0][0] == "OK"
    fresh = _fake({"/quote/RELIANCE": (200, {"age_s": 3.1, "source": "dhan"}, "")})
    assert M.check_quote_age(True, fresh)[0][0] == "OK"
    assert M.check_quote_age(True, _fake({"/quote/RELIANCE": (200, {"source": "x"}, "")}))[0][0] == "WARN"
    assert M.check_quote_age(True, _fake({"/quote/RELIANCE": (200, {"age_s": "n/a"}, "")}))[0][0] == "WARN"
    assert M.check_quote_age(True, _fake({}))[0][0] == "FAIL"
    assert M.check_quote_age(False, _fake({}))[0][0] == "WARN"


def test_depth20_layout_verdicts():
    def r(verdict, avail=True):
        return _fake({"/internal/dhan-status": (200, {"depth20": {"layout_verdict": verdict, "suspect_packets": 0, "crossed_books": 0, "enabled": True}}, ""),
                      "/depth/RELIANCE": (200, {"available": avail, "reason": "warming", "spread_pct": 0.02}, "")})
    assert [lv for lv, _ in M.check_depth20(r("ok"))] == ["OK", "OK"]
    assert [lv for lv, _ in M.check_depth20(r("suspect"))][0] == "FAIL"
    assert [lv for lv, _ in M.check_depth20(r("unverified", avail=False))] == ["WARN", "WARN"]
    assert M.check_depth20(_fake({}))[0][0] == "FAIL"


def test_order_ws_verdicts():
    f = lambda body: _fake({"/orders/ws-status": (200, body, "")})  # noqa: E731
    assert M.check_order_ws(f({"enabled": False}))[0][0] == "INFO"
    assert M.check_order_ws(f({"enabled": True, "layout_verdict": "ok"}))[0][0] == "OK"
    assert M.check_order_ws(f({"enabled": True, "layout_verdict": "suspect"}))[0][0] == "FAIL"
    assert M.check_order_ws(f({"enabled": True, "layout_verdict": "unverified"}))[0][0] == "WARN"
    assert M.check_order_ws(_fake({}))[0][0] == "FAIL"


def test_health_check_lists_each_dead_service():
    f = _fake({"8005/health": (500, None, "HTTP 500")})
    f2 = lambda url, timeout=8.0: (200, {}, "") if "8005" not in url else (500, None, "HTTP 500")  # noqa: E731
    out = M.check_health(f2)
    assert len(out) == len(M.SERVICES)
    assert [t for lv, t in out if lv == "FAIL"] == ["real-trade /health -> 500"]


def test_scan_log_text_caps_and_counts():
    text = "\n".join([f"line {i} FAIL x" if i % 2 else f"line {i} ok" for i in range(200)] + ["tail suspect", "CRITICAL boom"])
    hits, total = M.scan_log_text(text, limit=5)
    assert total == 102 and len(hits) == 5 and hits[-1] == "CRITICAL boom" and hits[-2] == "tail suspect"
    assert M.scan_log_text("all fine\nnothing here") == ([], 0)


def test_docker_log_scan_with_fake_runner():
    class R:
        def __init__(self, rc, out, err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    clean = M.check_docker_logs(runner=lambda *a, **k: R(0, "all good\n"))
    assert clean[0][0] == "OK"
    dirty = M.check_docker_logs(runner=lambda *a, **k: R(0, "x CRITICAL y\nsession suspect\n"))
    assert dirty[0][0] == "WARN" and [lv for lv, _ in dirty[1:]] == ["LOG", "LOG"]
    failed = M.check_docker_logs(runner=lambda *a, **k: R(1, "", "no configuration file"))
    assert failed[0][0] == "WARN"

    def boom(*a, **k):
        raise FileNotFoundError("docker")
    assert M.check_docker_logs(runner=boom)[0][0] == "WARN"


def test_run_and_render_summary_and_exit_code():
    f = _fake({"/health": (200, {}, ""), "/market/session": (200, {"status": "open"}, ""),
               "/quote/RELIANCE": (200, {"age_s": 2, "source": "dhan"}, ""),
               "/internal/dhan-status": (200, {"depth20": {"layout_verdict": "ok"}}, ""),
               "/depth/RELIANCE": (200, {"available": True}, ""),
               "/orders/ws-status": (200, {"enabled": False}, ""),
               "/internal/data-sources": (200, {"serving": "dhan"}, "")})
    real_host = M.check_host
    M.check_host = lambda: [("OK", "host stub")]   # keep the host's own disk / memory out of this test
    try:
        lines = M.run("1015", use_docker=False, now=datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc), fetch=f)
    finally:
        M.check_host = real_host
    text = M.render(lines)
    assert "SUMMARY fail=0" in text and "nothing to paste" in text
    f_bad = _fake({"/market/session": (200, {"status": "unknown"}, "")})
    lines = M.run("1015", use_docker=False, now=datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc), fetch=f_bad)
    assert "paste every FAIL" in M.render(lines)
    assert any(lv == "FAIL" for lv, _ in lines)


def test_main_writes_live_log_and_returns_exit_code():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, **{v: "http://127.0.0.1:9" for _, v in M.SERVICES.values()}}
        r = subprocess.run([sys.executable, os.path.join(HERE, "monday_check.py"), "--label", "1015", "--no-docker", "--out-dir", tmp],
                           capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 1, r.stdout  # nothing listens on port 9: every service is a FAIL
        files = os.listdir(tmp)
        assert len(files) == 1 and files[0].endswith("1015ist_live.log")
        assert "SUMMARY fail=" in open(os.path.join(tmp, files[0])).read()


def test_scripts_are_executable_and_have_valid_syntax():
    for p in ("vm_ops.sh", "stats_loop.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(HERE, p)]).returncode == 0
