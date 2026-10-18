"""
scripts/check_market_data_url_default.py - group379 (G2): no service may fall back to a public *.onrender.com market-data host.

Why: 16 modules carried `MARKET_DATA_URL` defaults of https://market-data-service-r6d7.onrender.com. On the Oracle VM that host is not ours to
depend on: if the variable was ever missing from a compose block, quotes, history and delivery calls quietly went out to a public host (or failed
on a dead one) instead of the in-network service, with nothing in the logs saying so. The default is now the compose service name,
http://market-data-service:8001, which can only ever resolve inside the compose network.

Checks (run from the repo root: python3 scripts/check_market_data_url_default.py, or python3 -m pytest scripts/check_market_data_url_default.py):
  1. no production *.py (tests, archive and virtualenvs excluded) contains an onrender.com host on a MARKET_DATA_URL default line,
  2. every compose service that reads MARKET_DATA_URL sets it to the in-network URL.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _repo_walk import walk_py  # noqa: E402

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_EXPECTED = "http://market-data-service:8001"
_MUST_SET = (
    "analysis-intelligence-service",
    "decision-prediction-service",
    "notification-scheduler-service",
    "api-gateway",
    "real-trade-service",
    "position-stocks-service",
    "kronos-forecast-service",
)
_RENDER_ON_DEFAULT = re.compile(r"MARKET_DATA_URL[^\n]*onrender\.com|onrender\.com[^\n]*MARKET_DATA_URL")


def _is_test(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py"


def render_defaults(root: Path = _ROOT) -> list[str]:
    """'relative/path.py:LINE' of every production line that defaults MARKET_DATA_URL to an onrender.com host."""
    bad = []
    for sub in ("services", "config", "shared", "scripts"):
        base = root / sub
        if not base.is_dir():
            continue
        for p in walk_py(base, extra_skip={"archive"}):
            if _is_test(p) or p.name == Path(__file__).name:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if line.lstrip().startswith("#") or '"""' in line:
                    continue
                if _RENDER_ON_DEFAULT.search(line):
                    bad.append(f"{p.relative_to(root).as_posix()}:{n}")
    return bad


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        out = {}
        for item in env:
            if "=" in item:
                k, v = item.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    return {str(k): ("" if v is None else str(v)) for k, v in env.items()}


def _services() -> dict:
    with open(_ROOT / "docker-compose.yml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


def test_no_production_default_points_at_a_public_render_host():
    bad = render_defaults()
    assert not bad, f"MARKET_DATA_URL must default to {_EXPECTED!r}, not an onrender.com host: {bad}"


def test_compose_services_use_the_in_network_market_data_url():
    services = _services()
    bad = {n: _env(services[n]).get("MARKET_DATA_URL") for n in _MUST_SET if _env(services[n]).get("MARKET_DATA_URL") != _EXPECTED}
    assert not bad, f"MARKET_DATA_URL must be {_EXPECTED!r}; got {bad}"


def test_no_compose_service_is_missing_from_the_list():
    services = _services()
    extra = [n for n, s in services.items() if "MARKET_DATA_URL" in _env(s) and n not in _MUST_SET]
    assert not extra, f"add to _MUST_SET: {extra}"


if __name__ == "__main__":
    test_no_production_default_points_at_a_public_render_host()
    test_compose_services_use_the_in_network_market_data_url()
    test_no_compose_service_is_missing_from_the_list()
    print("ok")
