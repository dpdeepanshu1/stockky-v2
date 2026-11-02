"""group479: scripts/ run inside the runner image and some load files of other services by path (spec_from_file_location / sys.path).
The Dockerfile copies those files one by one; a missing one only shows up as a failed job on the VM (setup_backtest.py needed
services/position-stocks-service/orders/setup_engine.py). Offline, stdlib only."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "services" / "calibration-runner-service" / "Dockerfile"


def _copy_sources() -> set:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    out = set()
    for line in text.splitlines():
        if line.strip().upper().startswith("COPY "):
            out.update(tok for tok in line.split()[1:-1])
    return out


def test_setup_backtest_engine_file_is_copied():
    assert "services/position-stocks-service/orders/setup_engine.py" in _copy_sources()


def test_setup_engine_is_stdlib_only():
    import re
    text = (ROOT / "services/position-stocks-service/orders/setup_engine.py").read_text(encoding="utf-8")
    mods = {m.group(1) for m in re.finditer(r"^(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", text, re.M)}
    assert mods <= {"__future__", "datetime", "typing"}, "setup_engine imports %s: the runner image would need those too" % sorted(mods)


def test_setup_backtest_path_matches_the_copied_path():
    text = (ROOT / "scripts" / "setup_backtest.py").read_text(encoding="utf-8")
    assert '"position-stocks-service" / "orders" / "setup_engine.py"' in text


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
