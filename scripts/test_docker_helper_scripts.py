"""E127: the VM test helpers keep the fixes the 24 Oct run needed (read as text; no Docker needed)."""
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))


def _t(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def test_docker_test_installs_httpx_below_0_28_when_the_service_does_not_list_it():
    s = _t("docker_test.sh")
    assert 'grep -qi "^httpx" requirements.txt || h="httpx<0.28"' in s
    assert re.search(r"pytest pytest-cov \$h ", s)


def test_runner_requirements_do_not_list_httpx_so_the_script_must():
    req = open(os.path.join(HERE, "..", "services", "calibration-runner-service", "requirements.txt"), encoding="utf-8").read().lower()
    assert "httpx" not in req and "fastapi==0.104" in req


def test_frontend_check_mounts_the_whole_repo_so_the_tests_see_the_files_they_read():
    """E285: swing / statusProbes / reconcile / calibration tests read services/*/ and frontend/src/ files through new URL("../../", import.meta.url).
    With only frontend/ mounted at /app that root is "/" and they died with ENOENT; the repo is mounted at /repo and the tests run from /repo/frontend."""
    s = _t("docker_frontend_check.sh")
    assert '-v "$root":/repo' in s and "-w /repo/frontend" in s
    assert "-w /app \"$test_tag\"" not in s
    front = os.path.join(HERE, "..", "frontend", "src")
    reads = set()
    for name in os.listdir(front):
        if name.endswith(".test.mjs"):
            reads.update(re.findall(r'new URL\("(services/[^"]+)", root\)', open(os.path.join(front, name), encoding="utf-8").read()))
    assert reads, "no test reads a service file any more: this guard can go"
    for rel in reads:
        assert os.path.exists(os.path.join(HERE, "..", rel)), rel      # every file a test reads exists in the repo, so a mounted repo satisfies it


def test_scripts_are_valid_bash():
    for n in ("docker_test.sh", "docker_frontend_check.sh", "vm_runjob.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(HERE, n)]).returncode == 0, n


# ── E285 round 124 ───────────────────────────────────────────────────────────────────────────────────────────────────

def _fake_bin(tmp_path, docker_body, df_kb=None):
    """A PATH directory with a fake `docker` (and optionally `df`) so the scripts run without Docker."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text("#!/usr/bin/env bash\n" + textwrap.dedent(docker_body))
    if df_kb is not None:
        (b / "df").write_text(f"#!/usr/bin/env bash\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\necho '/dev/x 100 1 {df_kb} 1% /'\n")
    for f in b.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return b


def _docker_test_sandbox(tmp_path, requirements):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "services" / "svc").mkdir(parents=True)
    shutil.copy(os.path.join(HERE, "docker_test.sh"), root / "scripts" / "docker_test.sh")
    (root / "services" / "svc" / "Dockerfile").write_text("FROM python:3.11-slim\n")
    (root / "services" / "svc" / "requirements.txt").write_text(requirements)
    return root


def test_docker_test_installs_the_cpu_torch_wheel_first_when_the_service_lists_torch():
    s = _t("docker_test.sh")
    assert 'grep -qiE "^torch([<>=!~ ]|$)" requirements.txt' in s
    assert "https://download.pytorch.org/whl/cpu" in s and "-e TORCH_INDEX_URL" in s
    assert s.index("${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}") < s.index("ignore -r requirements.txt")       # torch first, then the requirements
    req = open(os.path.join(HERE, "..", "services", "kronos-forecast-service", "requirements.txt"), encoding="utf-8").read()
    assert re.search(r"(?mi)^torch([<>=!~ ]|$)", req)                                        # the line the script looks for really is in the kronos file
    assert "whl/cpu" in open(os.path.join(HERE, "..", "services", "kronos-forecast-service", "Dockerfile"), encoding="utf-8").read()


def test_docker_test_refuses_to_start_on_a_nearly_full_disk_with_a_clear_message(tmp_path):
    if not shutil.which("bash"):
        return
    root = _docker_test_sandbox(tmp_path, "fastapi\n")
    b = _fake_bin(tmp_path, 'if [ "$1" = info ]; then echo /var/lib/docker; exit 0; fi\necho "docker $*" >> "$DOCKER_LOG"\n', df_kb=500 * 1024)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", DOCKER_LOG=str(tmp_path / "log"))
    r = subprocess.run(["bash", str(root / "scripts" / "docker_test.sh"), "svc"], capture_output=True, text=True, env=env)
    assert r.returncode == 3 and "NOT STARTED - only 500 MB free" in r.stderr and "do NOT add --volumes" in r.stderr, r.stderr
    assert not (tmp_path / "log").exists()                                                   # docker run was never called


def test_docker_test_runs_when_there_is_room_and_the_check_can_be_switched_off(tmp_path):
    if not shutil.which("bash"):
        return
    root = _docker_test_sandbox(tmp_path, "torch>=2.0.0\n")
    body = 'if [ "$1" = info ]; then echo /var/lib/docker; exit 0; fi\necho "docker $*" >> "$DOCKER_LOG"\n'
    env0 = dict(os.environ, DOCKER_LOG=str(tmp_path / "log"))
    b = _fake_bin(tmp_path, body, df_kb=50 * 1024 * 1024)
    ok = subprocess.run(["bash", str(root / "scripts" / "docker_test.sh"), "svc", "-q"], capture_output=True, text=True, env=dict(env0, PATH=f"{b}:{os.environ['PATH']}"))
    assert ok.returncode == 0 and "docker run --rm" in (tmp_path / "log").read_text() and "whl/cpu" in (tmp_path / "log").read_text(), ok.stderr
    (tmp_path / "log").unlink()
    b2 = _fake_bin(tmp_path, body, df_kb=1024)
    off = subprocess.run(["bash", str(root / "scripts" / "docker_test.sh"), "svc"], capture_output=True, text=True,
                         env=dict(env0, PATH=f"{b2}:{os.environ['PATH']}", DOCKER_TEST_MIN_FREE_GB="0"))
    assert off.returncode == 0 and (tmp_path / "log").exists()


def _probe(tmp_path, answers, wait="3", poll="0"):
    """Run probe() from e277_vm_check.sh against a fake `docker compose exec` that prints the given answers in turn (the last one repeats)."""
    n = tmp_path / "n"
    n.write_text("0")
    lines = tmp_path / "answers"
    lines.write_text("\n".join(answers) + "\n")
    b = _fake_bin(tmp_path, f"""
        i=$(( $(cat {n}) + 1 )); echo $i > {n}
        total=$(wc -l < {lines}); [ "$i" -gt "$total" ] && i=$total
        sed -n "${{i}}p" {lines}
        """)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", E277_FUNCS_ONLY="1", E277_WAIT_S=wait, E277_POLL_S=poll)
    script = os.path.join(HERE, "e277_vm_check.sh")
    r = subprocess.run(["bash", "-c", 'source "$1"; probe "$2" "$3" "$4"', "x", script, "position-stocks-service", "8006", "/premarket/plan"],
                       capture_output=True, text=True, env=env, cwd=str(tmp_path))
    return r, int(n.read_text())


def test_e277_probe_retries_while_the_service_boots_and_names_the_service(tmp_path):
    if not shutil.which("bash"):
        return
    r, calls = _probe(tmp_path, ["ERR URLError", "ERR URLError", "HTTP 401"])
    assert r.returncode == 0 and calls == 3, (r.stdout, r.stderr)
    assert r.stdout.strip().startswith("PASS position-stocks-service :8006/premarket/plan answered HTTP 401"), r.stdout
    assert "ERR" not in r.stdout


def test_e277_probe_gives_up_after_the_wait_and_says_which_service_and_why(tmp_path):
    if not shutil.which("bash"):
        return
    r, calls = _probe(tmp_path, ["ERR URLError"], wait="1", poll="0.3")
    assert r.returncode == 1 and calls >= 2, (r.stdout, calls)
    assert r.stdout.startswith("FAIL position-stocks-service :8006/premarket/plan gave no answer in 1s - last: ERR URLError") and "docker compose logs" in r.stdout


def test_e277_probe_treats_a_5xx_as_not_ready_and_a_200_as_pass(tmp_path):
    if not shutil.which("bash"):
        return
    r, calls = _probe(tmp_path, ["HTTP 503", "OK 200"])
    assert r.returncode == 0 and calls == 2 and "PASS position-stocks-service :8006/premarket/plan answered 200" in r.stdout


def test_e277_script_uses_probe_for_both_services_and_stays_valid_bash():
    s = _t("e277_vm_check.sh")
    assert "probe real-trade-service 8005 /health" in s and "probe position-stocks-service 8006 /premarket/plan" in s
    assert s.index("probe(){") < s.index('echo "== 1.')
    for n in ("docker_test.sh", "e277_vm_check.sh", "e278_run.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(HERE, n)]).returncode == 0, n


def test_e278_run_refetches_old_history_and_reports_its_age():
    s = _t("e278_run.sh")
    assert 'MAX_AGE_DAYS="${MAX_AGE_DAYS:-5}"' in s and '--max-age-days "$MAX_AGE_DAYS"' in s and "--report-ages" in s
