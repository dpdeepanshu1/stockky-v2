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


def test_frontend_check_mounts_the_one_file_calibration_test_reads():
    s = _t("docker_frontend_check.sh")
    assert "services/calibration-runner-service/jobs.py\":/services/calibration-runner-service/jobs.py:ro" in s
    assert "../../services/calibration-runner-service/jobs.py" in open(os.path.join(HERE, "..", "frontend", "src", "calibration.test.mjs"), encoding="utf-8").read()


def test_scripts_are_valid_bash():
    for n in ("docker_test.sh", "docker_frontend_check.sh", "vm_runjob.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(HERE, n)]).returncode == 0, n
