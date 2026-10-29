"""E127: run the standalone short-selling behaviour scripts for real (own interpreter, cwd = services/position-stocks-service), so `pytest scripts` covers them."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "..", "services", "position-stocks-service")
CASES = [("test_plain_short.py", 61), ("test_short_exit_paths.py", 75), ("test_short_live.py", 93)]


@pytest.mark.parametrize("script,expected", CASES)
def test_script_passes_in_its_own_interpreter(script, expected):
    if not os.path.isdir(os.path.join(SERVICE, "orders")):
        pytest.skip("services/position-stocks-service is not in this checkout")
    r = subprocess.run([sys.executable, os.path.join(HERE, script)], cwd=SERVICE, capture_output=True, text=True, timeout=300)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    assert r.returncode == 0, (script, r.stdout[-600:], r.stderr[-600:])
    assert last == f"ALL {expected} OK", (script, last)
