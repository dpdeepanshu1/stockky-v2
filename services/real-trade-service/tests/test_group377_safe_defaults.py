"""group377: the live-money safety defaults (Group A of the 2026-10-15 log review).

BROKER_STOP_ENABLED defaults ON (a resting broker stop for CNC holdings); STRUCTURE_REQUIRE_DATA defaults ON (no usable daily structure = WAIT,
tag structure_nodata, never a silent pass). Both keep an env kill switch. conftest turns both off for the pre-existing tests; these read the source
defaults directly so a regression to the old fail-open values is caught.
"""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(HERE, "config.py"), encoding="utf-8").read()


def test_broker_stop_default_is_on():
    assert re.search(r'BROKER_STOP_ENABLED\s*=\s*_bs_bool\("BROKER_STOP_ENABLED",\s*"true"\)', _src())


def test_structure_require_data_default_is_on():
    assert re.search(r'STRUCTURE_REQUIRE_DATA\s*=\s*\(\(os\.getenv\("STRUCTURE_REQUIRE_DATA"\) or ""\)\.strip\(\) or "true"\)', _src())


def test_env_kill_switches_still_exist():
    s = _src()
    assert "BROKER_STOP_ENABLED" in s and "STRUCTURE_REQUIRE_DATA" in s and "STRUCTURE_GATE_MODE" in s


def test_env_example_matches_the_defaults():
    ex = open(os.path.join(HERE, "..", "..", ".env.example"), encoding="utf-8").read()
    assert re.search(r"(?m)^BROKER_STOP_ENABLED=true$", ex)
    assert "STRUCTURE_REQUIRE_DATA=true" in ex and "STRUCTURE_REQUIRE_FOR_BUY_NOW=true" in ex
