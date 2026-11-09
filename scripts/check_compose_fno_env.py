"""
scripts/check_compose_fno_env.py - every environment name fno-options-service reads must reach the container.

Why (round 254): the F&O service has NO env_file on purpose (it must not receive the Dhan / database secrets of the other services), so docker-compose.yml has to list
every name it reads. Only 8 of about 95 were listed: FNO_EXPIRY_REAL, the FNO_STRAT_* thresholds, FNO_SL_PCT ... set in .env were silently ignored, and
CAPITAL_ALLOC_MODE=live in .env switched Real Trade and Position Stocks (they have env_file) but not F&O.

Checks:
  1. every name read in config.py (and FNO_RUNNER in main.py) is a key of the service's `environment`, except the container-internal ones in INTERNAL_OK;
  2. the service has no `env_file` (that would hand it every secret in .env);
  3. config.py reads every FNO_* / CAPITAL_ALLOC_* name through the blank-safe helpers (_s/_f/_i/_b), because compose passes an unset name as an EMPTY string.

Run from the repo root:  python3 scripts/check_compose_fno_env.py   (or: python3 -m pytest scripts/check_compose_fno_env.py)
"""
from __future__ import annotations

import os
import re
import sys

import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SVC = os.path.join(_ROOT, "services", "fno-options-service")
SERVICE = "fno-options-service"
# read by config.py but fixed inside the container (a path), so intentionally not passed
INTERNAL_OK = {"CAPITAL_ALLOC_DIR"}
_READ = re.compile(r"""\b_[sfib]\(\s*["']([A-Z][A-Z0-9_]+)["']""")


def _read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _compose_service() -> dict:
    compose = yaml.safe_load(_read(_ROOT, "docker-compose.yml"))
    return compose["services"][SERVICE]


def _env_keys(svc: dict) -> set[str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return {str(x).split("=", 1)[0] for x in env}
    return {str(k) for k in env}


def names_read() -> set[str]:
    names = set(_READ.findall(_read(_SVC, "config.py")))
    if 'os.getenv("FNO_RUNNER"' in _read(_SVC, "main.py"):
        names.add("FNO_RUNNER")
    return names


def missing() -> list[str]:
    have = _env_keys(_compose_service())
    return sorted(n for n in names_read() if n not in have and n not in INTERNAL_OK)


def test_every_name_the_service_reads_reaches_the_container():
    miss = missing()
    assert not miss, f"{SERVICE}: read by config.py but not in docker-compose.yml environment: {miss}"


def test_the_service_has_no_env_file():
    assert not _compose_service().get("env_file"), f"{SERVICE} must not use env_file (it would receive every secret in .env)"


def test_names_exist_to_check():
    assert len(names_read()) >= 80


def test_fno_and_capital_names_are_read_blank_safe():
    src = _read(_SVC, "config.py")
    bad = [m.group(0) for m in re.finditer(r"os\.(?:getenv|environ)[^\n]*(?:FNO_|CAPITAL_ALLOC_)[^\n]*", src)]
    assert not bad, f"config.py reads these without the blank-safe helpers: {bad}"


if __name__ == "__main__":
    fails = 0
    for fn in (test_every_name_the_service_reads_reaches_the_container, test_the_service_has_no_env_file,
               test_names_exist_to_check, test_fno_and_capital_names_are_read_blank_safe):
        try:
            fn()
            print("ok  ", fn.__name__)
        except AssertionError as e:
            fails += 1
            print("FAIL", fn.__name__, "-", e)
    sys.exit(1 if fails else 0)
