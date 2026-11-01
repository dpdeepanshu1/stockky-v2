"""E127: the VM test helpers keep the fixes the 24 Oct run needed (read as text; no Docker needed)."""
import os
import re
import subprocess

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
