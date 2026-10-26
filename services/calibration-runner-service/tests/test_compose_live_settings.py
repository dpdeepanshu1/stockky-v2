"""E98 P0 item 1: docker-compose.yml ships the Market Weather / short-selling settings with the plan's defaults, every one overridable from .env.
Plain asserts, reads the compose file only (no services, no network)."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent.parent
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def _env(service: str) -> dict:
    e = COMPOSE[service]["environment"]
    if isinstance(e, list):  # position-stocks uses the "- KEY=value" list form
        return dict(x.split("=", 1) for x in e if "=" in x)
    return {k: str(v) for k, v in e.items()}


def _default_of(raw: str, key: str) -> str:
    """'${KEY:-default}' -> 'default'; also proves the value is overridable from .env under the SAME name."""
    m = re.fullmatch(r"\$\{" + re.escape(key) + r":-([^}]*)\}", raw)
    assert m, f"{key}: expected ${{{key}:-default}} (overridable from .env), got {raw!r}"
    return m.group(1)


def test_runner_weather_defaults():
    env = _env("calibration-runner-service")
    for key in ("WEATHER_ENABLED", "WEATHER_STAMP", "WEATHER_NOTIFY", "WEATHER_APPLY"):
        assert _default_of(env[key], key) == "true", key


def test_runner_gate_mode_defaults_to_replay_only():
    assert _default_of(_env("calibration-runner-service")["WEATHER_GATE_MODE"], "WEATHER_GATE_MODE") == "replay_only"


def test_both_trading_services_market_state_live():
    for svc in ("real-trade-service", "position-stocks-service"):
        assert _default_of(_env(svc)["MARKET_STATE_MODE"], "MARKET_STATE_MODE") == "live", svc


def test_position_stocks_short_mode_live_behind_the_probe():
    # E224: compose ships live; the real rails stay in code (SELL probe PASS required, 1-share cap, broker stop) and the compose file must not loosen them
    assert _default_of(_env("position-stocks-service")["SHORT_MODE"], "SHORT_MODE") == "live"
    assert "SHORT_MODE" not in _env("real-trade-service"), "Real Trade is long-only"


def test_nothing_else_is_switched_on_by_this_change():
    for svc in COMPOSE:
        env = _env(svc) if "environment" in COMPOSE[svc] else {}
        for key in env:
            if key.startswith("SHORT_") and key != "SHORT_MODE":
                raise AssertionError(f"{svc} must not set {key} in compose")
        assert "SHORT_LIVE_QTY_CAP" not in env


def test_catalog_recommended_matches_compose():
    cat = {s: (REPO / "services" / s / "control" / "catalog.py").read_text(encoding="utf-8") for s in ("real-trade-service", "position-stocks-service")}
    for s, txt in cat.items():
        m = re.search(r'Flag\("MARKET_STATE_MODE".*?recommended="(\w+)"', txt, re.S)
        assert m and m.group(1) == "live", s
    m = re.search(r'Flag\("SHORT_MODE".*?recommended="(\w+)"', cat["position-stocks-service"], re.S)
    assert m and m.group(1) == "live"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
