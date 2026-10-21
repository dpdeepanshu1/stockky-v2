"""group478: the runner Dockerfile copies the service's .py files one by one - a module that is missing there crashes the container at
startup (autocalib.py was missing after group 476). Offline, stdlib only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DOCKERFILE = HERE / "Dockerfile"


def _copied_names() -> set:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    names = set()
    for line in text.splitlines():
        if line.strip().upper().startswith("COPY "):
            for tok in line.split()[1:-1]:
                if tok.startswith("services/calibration-runner-service/"):
                    names.add(tok.rsplit("/", 1)[1])
    return names


def test_every_service_module_is_copied_into_the_image():
    copied = _copied_names()
    modules = {p.name for p in HERE.glob("*.py")}
    missing = sorted(modules - copied)
    assert not missing, "Dockerfile does not COPY: %s" % ", ".join(missing)


def test_every_local_import_is_a_copied_module():
    copied = _copied_names()
    local = {p.stem for p in HERE.glob("*.py")}
    for p in HERE.glob("*.py"):
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", p.read_text(encoding="utf-8"), re.M):
            if m.group(1) in local:
                assert (m.group(1) + ".py") in copied, "%s imports %s, which the Dockerfile does not COPY" % (p.name, m.group(1))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
