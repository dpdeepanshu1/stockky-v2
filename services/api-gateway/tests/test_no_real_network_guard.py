"""group317: the conftest guard must really fail a test that reaches for the network (and let loopback through)."""
import shutil
import subprocess
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent


def _run(tmp_path, body, env_extra=None):
    shutil.copy(_TESTS / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(body)
    import os
    env = {**os.environ, **(env_extra or {})}
    env.pop("ALLOW_TEST_NETWORK", None) if not env_extra else None
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tmp_path / "test_probe.py")],
                          capture_output=True, text=True, cwd=str(tmp_path), env=env)


def test_a_dns_lookup_of_a_real_host_fails_the_test(tmp_path):
    r = _run(tmp_path, "import socket\n\ndef test_x():\n    try:\n        socket.getaddrinfo('market-data-service-r6d7.onrender.com', 443)\n    except OSError:\n        pass\n")
    assert r.returncode != 0 and "real network call" in r.stdout


def test_loopback_is_allowed(tmp_path):
    r = _run(tmp_path, "import socket\n\ndef test_x():\n    socket.getaddrinfo('127.0.0.1', 80)\n    socket.getaddrinfo('localhost', 80)\n")
    assert r.returncode == 0, r.stdout


def test_escape_hatch_allows_a_deliberate_live_run(tmp_path):
    r = _run(tmp_path, "import socket\n\ndef test_x():\n    try:\n        socket.getaddrinfo('example.invalid', 80)\n    except OSError:\n        pass\n", {"ALLOW_TEST_NETWORK": "1"})
    assert r.returncode == 0, r.stdout
