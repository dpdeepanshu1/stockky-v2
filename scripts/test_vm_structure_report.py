"""group386: scripts/vm_structure_report.sh - offline, fake curl / docker."""
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "vm_structure_report.sh")


def _fake(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run(tmp_path, curl_body, env_text="BROKER_STOP_ENABLED=true\nSECRET_KEY=hunter2\n", extra=None):
    envf = tmp_path / ".env"
    envf.write_text(env_text)
    curl = _fake(tmp_path, "curl", curl_body)
    docker = _fake(tmp_path, "docker", 'echo "api-gateway  Up 2 minutes (healthy)"\n')
    e = dict(os.environ, CURL_BIN=curl, DOCKER_BIN=docker, ENV_FILE=str(envf), OUT=str(tmp_path / "r.txt"))
    e.update(extra or {})
    e.pop("ADMIN_TOKEN", None) if not extra or "ADMIN_TOKEN" not in extra else None
    r = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=e, timeout=60)
    return r, (tmp_path / "r.txt").read_text()


def test_report_has_every_section_status_and_no_secrets(tmp_path):
    r, txt = _run(tmp_path, 'echo "{\\"ok\\":true}"; printf "\\n200 0.012"\n')
    assert r.returncode == 0
    for label in ("services", "settings", "structure report", "observation stats", "structure refresh status",
                  "real-trade structure DEMO", "position-stocks structure", "db latency gateway", "news feed health",
                  "decision for INFY", "broker stop log", "structure log"):
        assert f"=== {label}" in txt, label
    assert "HTTP 200 in 0.012s" in txt and "BROKER_STOP_ENABLED=true" in txt
    assert "hunter2" not in txt and "SECRET_KEY" not in txt


def test_real_sections_skipped_without_token_and_sent_with_it(tmp_path):
    _, txt = _run(tmp_path, 'echo "$@" >> "$0.calls"; printf "{}\\n200 0.01"\n')
    assert "skipped: set ADMIN_TOKEN" in txt and "structure/status/REAL" not in txt
    t2 = tmp_path / "b"; t2.mkdir()
    _, txt2 = _run(t2, 'echo "$@" >> "$0.calls"; printf "{}\\n200 0.01"\n', extra={"ADMIN_TOKEN": "tok123"})
    assert "=== real-trade structure REAL" in txt2
    assert "Authorization: Bearer tok123" in (t2 / "curl.calls").read_text()
    assert "tok123" not in txt2


def test_failed_curl_and_404_are_distinguishable(tmp_path):
    r, txt = _run(tmp_path, "exit 7\n")
    assert "curl failed (exit 7)" in txt
    t2 = tmp_path / "c"; t2.mkdir()
    _, txt2 = _run(t2, 'printf "{\\"detail\\":\\"Not Found\\"}\\n404 0.002"\n')
    assert "HTTP 404 in 0.002s" in txt2 and "Not Found" in txt2


def test_sections_are_capped_and_missing_env_lines_noted(tmp_path):
    r, txt = _run(tmp_path, 'python3 -c "print(\'x\'*9000)"; printf "\\n200 0.5"\n', env_text="OTHER=1\n", extra={"CAP": "200"})
    assert "defaults apply" in txt
    assert max(len(line) for line in txt.splitlines()) <= 200
