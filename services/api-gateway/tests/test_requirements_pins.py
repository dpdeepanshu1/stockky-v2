"""Drift guard: dependencies that a fail-open code path silently depends on must be in the
PRODUCTION requirements.txt (not only requirements-test.txt), or the Docker image ships without them.

qstash_client.verify_signature() imports PyJWT lazily and, if it is missing, accepts every QStash
callback unverified. That went unnoticed because PyJWT was only listed in requirements-test.txt.
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _names(fname):
    out = set()
    with open(os.path.join(HERE, fname), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"([A-Za-z0-9_.\-]+)", line)
            if m:
                out.add(m.group(1).lower().replace("_", "-"))
    return out


def test_pyjwt_is_a_production_requirement():
    assert "pyjwt" in _names("requirements.txt")


def test_pyjwt_production_pin_is_exact():
    with open(os.path.join(HERE, "requirements.txt"), encoding="utf-8") as fh:
        lines = [l.strip() for l in fh if l.strip().lower().startswith("pyjwt")]
    assert lines and all("==" in l for l in lines)


def test_pyjwt_pin_matches_the_other_services():
    pins = set()
    for svc in ("real-trade-service", "position-stocks-service"):
        p = os.path.join(HERE, "..", svc, "requirements.txt")
        if not os.path.exists(p):
            continue
        for l in open(p, encoding="utf-8"):
            if l.strip().lower().startswith("pyjwt"):
                pins.add(l.split("#")[0].strip().lower())
    with open(os.path.join(HERE, "requirements.txt"), encoding="utf-8") as fh:
        mine = {l.split("#")[0].strip().lower() for l in fh if l.strip().lower().startswith("pyjwt")}
    assert not pins or mine <= pins
