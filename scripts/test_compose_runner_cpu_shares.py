"""E285 round 171: the calibration runner has a low CPU weight, so the live services win when the 2-vCPU VM is busy."""
import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"


def _block(name: str) -> str:
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^[a-z]|\Z)", text, re.S | re.M)
    assert m, f"service {name} not found in docker-compose.yml"
    return m.group(1)


def test_runner_has_low_cpu_shares():
    m = re.search(r"^    cpu_shares:\s*(\d+)", _block("calibration-runner-service"), re.M)
    assert m, "calibration-runner-service needs cpu_shares"
    assert int(m.group(1)) <= 256


def test_runner_cpu_cap_unchanged():
    assert re.search(r'^    cpus:\s*"1\.50"', _block("calibration-runner-service"), re.M)


def test_live_services_keep_default_weight():
    for name in ("real-trade-service", "position-stocks-service", "market-data-service", "api-gateway"):
        assert "cpu_shares" not in _block(name), name
