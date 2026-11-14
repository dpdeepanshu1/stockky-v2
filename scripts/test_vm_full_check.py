"""E516 (round 496): scripts/vm_full_check.sh - review items 9 and 10 of 13 Nov.

 9. the frontend step must see the files the frontend tests read (services/**.py), so the whole repo is mounted, and a checkout that lacks one of them is refused up front.
10. the summary must show the REAL exit code of each step: a failed frontend test printed exit=0 when the code was read after `| tail`.
The script runs here against a sandbox repo whose docker_test.sh / vm_frontend_check.sh are stubs, so no Docker is needed.
"""
import os
import re
import shutil
import site
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "vm_full_check.sh")


def _read(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def _sandbox(tmp_path, frontend_rc=0, service_rc=0, with_test_reads=None):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "vm_full_check.sh")
    for chk in ("check_merge_conflicts.py", "check_shared_copies.py", "gen_env_reference.py", "check_control_catalog.py", "check_stale_files.py"):
        (root / "scripts" / chk).write_text("import sys\nsys.exit(0)\n")
    for t in ("test_repo_scripts.py", "test_docker_helper_scripts.py", "test_vm_full_check.py"):
        (root / "scripts" / t).write_text("def test_ok():\n    assert True\n")
    (root / "scripts" / "docker_test.sh").write_text(f'#!/usr/bin/env bash\necho "suite $1"\nexit {service_rc}\n')
    # the stub fails the way the real one does: output first, then a non-zero code, so a `| tail` would have hidden it
    (root / "scripts" / "vm_frontend_check.sh").write_text(f'#!/usr/bin/env bash\necho "ENOENT /services/x.py"\necho "frontend check: result"\nexit {frontend_rc}\n')
    (root / "services" / "svc-a" / "tests").mkdir(parents=True)
    (root / "services" / "svc-a" / "Dockerfile").write_text("FROM python:3.11-slim\n")
    (root / "services" / "no-tests").mkdir(parents=True)
    (root / "services" / "no-tests" / "Dockerfile").write_text("FROM python:3.11-slim\n")
    (root / "frontend" / "src").mkdir(parents=True)
    if with_test_reads:
        (root / "frontend" / "src" / "x.test.mjs").write_text(with_test_reads)
    return root


def _run(root, tmp_path, **env):
    # round 500: no HOME override. The script gets OUT explicitly, and a changed HOME hides a `pip install --user` pytest from the inner `python -m pytest` step
    # (it passed here with a system-wide pytest and failed on the VM, where pytest is a user install). The user site is pinned as well, for a harness run with another HOME.
    e = dict(os.environ, PYTHON=sys.executable, OUT=str(tmp_path / "out"), DOCKER_BIN=sys.executable, PYTHONUSERBASE=site.getuserbase())
    e.update(env)
    return subprocess.run(["bash", str(root / "scripts" / "vm_full_check.sh")], capture_output=True, text=True, env=e, timeout=120)


def _summary_line(out, name):
    for ln in out.splitlines():
        if re.match(r"^\s+(PASS|FAIL|SKIP)\s+" + re.escape(name) + r"\s", ln):
            return ln
    raise AssertionError(f"no summary line for {name!r} in:\n{out}")


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", SCRIPT]).returncode == 0


def test_everything_green_exits_0_and_lists_every_step(tmp_path):
    root = _sandbox(tmp_path)
    r = _run(root, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RESULT: ALL PASSED" in r.stdout
    for name in ("merge-markers", "env-reference", "file-manifest", "tests:svc-a", "frontend"):
        ln = _summary_line(r.stdout, name)
        assert ln.strip().startswith("PASS") and "exit=0" in ln
    assert "tests:no-tests" not in r.stdout                      # a service without a tests/ folder is not run


def test_failed_frontend_shows_its_real_exit_code_and_fails_the_run(tmp_path):
    """Item 10: the frontend stub prints output and exits 1; the summary must say FAIL / exit=1 and the script must exit 1 (the old summary said exit=0)."""
    root = _sandbox(tmp_path, frontend_rc=1)
    r = _run(root, tmp_path)
    assert r.returncode == 1, r.stdout
    ln = _summary_line(r.stdout, "frontend")
    assert ln.strip().startswith("FAIL") and "exit=1" in ln and "exit=0" not in ln
    assert "RESULT: FAILED (1 step(s))" in r.stdout
    assert "ENOENT /services/x.py" in r.stdout                   # the tail of the failing step is shown, so the cause is on screen
    assert "RESULT: ALL PASSED" not in r.stdout


def test_other_exit_codes_are_kept_not_flattened(tmp_path):
    root = _sandbox(tmp_path, service_rc=3)
    r = _run(root, tmp_path)
    assert r.returncode == 1
    assert "exit=3" in _summary_line(r.stdout, "tests:svc-a")
    assert "exit=0" in _summary_line(r.stdout, "frontend")


def test_no_docker_is_skip_never_pass(tmp_path):
    root = _sandbox(tmp_path)
    r = _run(root, tmp_path, DOCKER_BIN="definitely-not-a-program-xyz")
    assert r.returncode == 0, r.stdout
    assert _summary_line(r.stdout, "service-suites").strip().startswith("SKIP")
    assert _summary_line(r.stdout, "frontend").strip().startswith("SKIP")
    assert "RESULT: ALL PASSED" not in r.stdout and "were skipped" in r.stdout


def test_a_checkout_missing_a_file_the_frontend_tests_read_is_refused_before_docker(tmp_path):
    """Item 9: a frontend test reads services/real-trade-service/main.py; if the checkout lacks it the container cannot see it either."""
    root = _sandbox(tmp_path, with_test_reads='const m = readFileSync(new URL("services/real-trade-service/main.py", root), "utf8");\n')
    r = _run(root, tmp_path)
    assert r.returncode == 1
    ln = _summary_line(r.stdout, "frontend")
    assert ln.strip().startswith("FAIL") and "exit=2" in ln
    assert "services/real-trade-service/main.py" in r.stdout
    (root / "services" / "real-trade-service").mkdir()
    (root / "services" / "real-trade-service" / "main.py").write_text("x = 1\n")
    assert _run(root, tmp_path).returncode == 0


def test_the_summary_is_built_from_recorded_codes_with_no_pipe_after_a_step():
    s = _read("vm_full_check.sh")
    # the exit code is read on the line right after the command, and the command's output goes to a file, never into a pipe
    assert re.search(r'"\$@" > "\$log" 2>&1\n\s+rc=\$\?', s)
    for ln in s.splitlines():
        if ln.startswith("step ") or ln.lstrip().startswith("TAIL_LINES=400 step "):
            assert "|" not in ln.split("#")[0], ln
    assert "PIPESTATUS" not in s                                  # nothing here needs it: no step runs through a pipe


def test_frontend_step_uses_the_whole_repo_mount():
    s = _read("vm_frontend_check.sh")
    assert '-v "$ROOT":/src:ro' in s and "cd /work/frontend" in s and "--exclude=./archive" in s
    assert 'step "frontend" bash scripts/vm_frontend_check.sh' in _read("vm_full_check.sh")
    assert "-v \"$ROOT/frontend\"" not in s
