"""group 482: scripts/vm_review_evidence.sh - offline, fake curl / docker."""
import json
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "vm_review_evidence.sh")

RECORDS = json.dumps({"records": [
    {"position_id": 1, "mode": "REAL", "entry_price": 100, "quantity": 30, "charges": 2.1, "held_days": 0, "gross_pnl": 5, "net_pnl": 2.9,
     "return_pct": 0.2, "opened_at": "2026-10-20T04:00:00+00:00", "closed_at": "2026-10-20T06:00:00+00:00", "charges_source": "ledger"},
    {"position_id": 2, "mode": "REAL", "entry_price": 100, "quantity": 30, "charges": 16.8, "held_days": 1, "gross_pnl": 9, "net_pnl": -7.8,
     "return_pct": 0.3, "opened_at": "2026-10-20T04:10:00+00:00", "closed_at": "2026-10-21T06:00:00+00:00", "charges_source": "ledger"}]})


def _fake(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _curl_ok(tmp_path):
    (tmp_path / "records.json").write_text(RECORDS)
    return ('url="${@: -1}"\ncase "$url" in\n'
            '  *positions/REAL/records*) cat "%s"; printf "\\n200" ;;\n'
            '  *) printf \'{"ok": true}\\n200 0.01\' ;;\nesac\n') % (tmp_path / "records.json")


def _run(tmp_path, curl_body, token=True, env_text="STOPOUT_DAY_PAUSE_N=3\nSECRET_KEY=hunter2\nSIZE_LADDER_ENABLED=true\n"):
    envf = tmp_path / ".env"
    envf.write_text(env_text)
    curl = _fake(tmp_path, "curl", curl_body)
    docker = _fake(tmp_path, "docker", 'if [ "$2" = "ps" ]; then echo "real-trade-service  Up 2 minutes"; else cat >/dev/null; echo "Gate scorecard - fake"; fi\n')
    e = dict(os.environ, CURL_BIN=curl, DOCKER_BIN=docker, ENV_FILE=str(envf), OUT=str(tmp_path / "r.txt"), REC=str(tmp_path / "rec.json"))
    e.pop("ADMIN_TOKEN", None)
    if token:
        e["ADMIN_TOKEN"] = "tok123"
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e, timeout=60)
    return r, (tmp_path / "r.txt").read_text()


def test_full_report_has_every_section_and_no_secrets(tmp_path):
    r, out = _run(tmp_path, _curl_ok(tmp_path))
    assert r.returncode == 0
    for s in ("=== date", "=== services", "=== settings", "position-stocks review status", "real-trade review status",
              "real-trade charges by component", "REAL trade export", "charges split", "trade report", "gate scorecard"):
        assert s in out, s
    assert "STOPOUT_DAY_PAUSE_N=3" in out and "SIZE_LADDER_ENABLED=true" in out
    assert "hunter2" not in out and "SECRET_KEY" not in out
    assert "HTTP 200" in out
    assert "Carried trades pay Rs 16.80 each against Rs 2.10" in out
    assert "Gate scorecard - fake" in out
    assert r.stdout == out


def test_without_a_token_the_real_sections_are_skipped_and_say_so(tmp_path):
    r, out = _run(tmp_path, _curl_ok(tmp_path), token=False)
    assert "skipped: set ADMIN_TOKEN" in out
    assert "charges split" not in out and "real-trade review status" not in out
    assert "position-stocks review status" in out and "gate scorecard" in out
    assert "HTTP 401" not in out                              # no token -> skipped, not a 401 call


def test_a_failed_export_is_reported_and_the_trade_reports_are_skipped(tmp_path):
    curl = ('url="${@: -1}"\ncase "$url" in\n'
            '  *positions/REAL/records*) printf "unauthorized\\n401" ;;\n'
            '  *) printf \'{"ok": true}\\n200 0.01\' ;;\nesac\n')
    r, out = _run(tmp_path, curl)
    assert "export failed" in out and "HTTP 401" in out
    assert "Carried trades pay" not in out


def test_curl_connection_failure_is_labelled(tmp_path):
    r, out = _run(tmp_path, "exit 7\n")
    assert "curl failed (exit 7)" in out
    assert r.returncode == 0


def test_cap_limits_each_section(tmp_path):
    envf = tmp_path / ".env"
    envf.write_text("X=1\n")
    curl = _fake(tmp_path, "curl", 'head -c 20000 /dev/zero | tr "\\0" x; printf "\\n200 0.01"\n')
    docker = _fake(tmp_path, "docker", "echo ok\n")
    e = dict(os.environ, CURL_BIN=curl, DOCKER_BIN=docker, ENV_FILE=str(envf), OUT=str(tmp_path / "r.txt"), CAP="300")
    e.pop("ADMIN_TOKEN", None)
    subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e, timeout=60)
    assert len((tmp_path / "r.txt").read_text()) < 3000


def _login_curl(tmp_path, ok=True):
    log = tmp_path / "curl_args.log"
    login = 'cat >/dev/null; printf \'{"token": "tok-from-login"}\\n200\'' if ok else 'cat >/dev/null; printf \'{"detail": "Invalid admin credentials"}\\n401\''
    body = ('echo "$@" >> "%s"\nurl="${@: -1}"\ncase "$url" in\n  *auth/login*) %s ;;\n' % (log, login)
            + '  *positions/REAL/records*) cat "%s"; printf "\\n200" ;;\n  *) printf \'{"ok": true}\\n200 0.01\' ;;\nesac\n' % (tmp_path / "records.json"))
    (tmp_path / "records.json").write_text(RECORDS)
    return body, log


def _run_login(tmp_path, ok):
    body, log = _login_curl(tmp_path, ok)
    envf = tmp_path / ".env"
    envf.write_text("X=1\n")
    curl = _fake(tmp_path, "curl", body)
    docker = _fake(tmp_path, "docker", 'if [ "$2" = "ps" ]; then echo "real-trade-service  Up"; else cat >/dev/null; echo "Gate scorecard - fake"; fi\n')
    e = dict(os.environ, CURL_BIN=curl, DOCKER_BIN=docker, ENV_FILE=str(envf), OUT=str(tmp_path / "r.txt"), REC=str(tmp_path / "rec.json"),
             ADMIN_USERNAME="owner", ADMIN_PASSWORD="s3cret-pw")
    e.pop("ADMIN_TOKEN", None)
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e, timeout=60)
    return r, (tmp_path / "r.txt").read_text(), (log.read_text() if log.exists() else "")


def test_login_with_username_and_password_gets_a_token_and_leaks_nothing(tmp_path):
    r, out, args = _run_login(tmp_path, True)
    assert "login ok (HTTP 200)" in out
    assert "real-trade charges by component" in out and "charges split" in out
    assert "Bearer tok-from-login" in args                    # the token was used for the REAL calls
    assert "s3cret-pw" not in out and "s3cret-pw" not in r.stdout and "s3cret-pw" not in args   # password never in the report or on a command line
    assert "tok-from-login" not in out


def test_failed_login_is_reported_and_real_sections_are_skipped(tmp_path):
    r, out, args = _run_login(tmp_path, False)
    assert "login failed" in out and "HTTP 401" in out
    assert "skipped: set ADMIN_TOKEN" in out
    assert "charges split" not in out
    assert "s3cret-pw" not in out
