"""
scripts/check_compose_gateway_url.py — every compose service that calls the API gateway must have
API_GATEWAY_URL set to the in-network gateway.

Why: decision-prediction-service, position-stocks-service (and analysis-intelligence-service's
rate-limit reporter) fall back to a hard-coded *.onrender.com gateway when API_GATEWAY_URL is unset.
On the Oracle VM that host is dead, so the decision engine's /market/indices call failed on every
request and every stock was scored with a neutral market_score of 50.

Run from the repo root:  python3 scripts/check_compose_gateway_url.py   (or: python3 -m pytest scripts/check_compose_gateway_url.py)
"""
from __future__ import annotations

import os
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPECTED = "http://api-gateway:8000"
_MUST_SET = (
    "decision-prediction-service",
    "position-stocks-service",
    "analysis-intelligence-service",
    "market-data-service",
    "notification-scheduler-service",
    "real-trade-service",
)


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
    with open(os.path.join(_ROOT, "docker-compose.yml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)["services"]


def test_gateway_callers_point_at_in_network_gateway():
    services = _services()
    bad = {}
    for name in _MUST_SET:
        got = _env(services[name]).get("API_GATEWAY_URL")
        if got != _EXPECTED:
            bad[name] = got
    assert not bad, f"API_GATEWAY_URL must be {_EXPECTED!r}; got {bad}"


def test_no_gateway_caller_is_missing_from_the_list():
    """A new service that mentions API_GATEWAY_URL in compose must be added to _MUST_SET."""
    services = _services()
    extra = [n for n, s in services.items() if "API_GATEWAY_URL" in _env(s) and n not in _MUST_SET]
    assert not extra, f"add to _MUST_SET: {extra}"


if __name__ == "__main__":
    test_gateway_callers_point_at_in_network_gateway()
    test_no_gateway_caller_is_missing_from_the_list()
    print("ok")
    sys.exit(0)
