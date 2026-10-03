"""group 75: blank / whitespace-only env flags in real-trade-service must mean "unset", not "false".

`NAME=` in a .env gives "". The default-on switches below were read as `os.getenv(NAME, "true").lower() == "true"`,
so a blank value silently turned them OFF (cost model, EDIS morning check, quality filters, overnight-hold rules,
limit-target exits, adaptive price ceiling). RISK_MAX_STOCK_PRICE="" also crashed the import (float("")) and, when
merely padded/blank-but-set, disabled the adaptive ceiling.

Run from services/real-trade-service:  python3 -m pytest tests/test_blank_flag_env.py -q
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from risk_engine import engine  # noqa: E402

CONFIG_FLAGS = (
    "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", "VOLUME_SHOCK_QUALITY_GATE_ENABLED", "COST_MODEL_ENABLED",
    "OVERNIGHT_HOLD_ENABLED", "OVERNIGHT_HOLD_REQUIRE_PROFITABLE", "OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS",
    "EDIS_MORNING_CHECK_ENABLED", "EXIT_TARGET_USE_LIMIT",
)
ENGINE_VARS = ("RISK_MAX_STOCK_PRICE", "RISK_MAX_STOCK_PRICE_ADAPTIVE")


@pytest.fixture
def reload_all():
    names = CONFIG_FLAGS + ENGINE_VARS
    saved = {k: os.environ.get(k) for k in names}

    def go(env):
        for k in names:
            os.environ.pop(k, None)
        os.environ.update(env)
        return importlib.reload(config), importlib.reload(engine)

    yield go
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(config)
    importlib.reload(engine)


@pytest.mark.parametrize("blank", ["", "  ", "\t"])
def test_blank_config_flags_keep_their_default_on(reload_all, blank):
    cfg, _ = reload_all({k: blank for k in CONFIG_FLAGS})
    for k in CONFIG_FLAGS:
        assert getattr(cfg, k) is True, k


def test_explicit_false_and_padded_true_still_work(reload_all):
    cfg, _ = reload_all({"COST_MODEL_ENABLED": "false", "EDIS_MORNING_CHECK_ENABLED": " TRUE ", "EXIT_TARGET_USE_LIMIT": "False"})
    assert cfg.COST_MODEL_ENABLED is False and cfg.EXIT_TARGET_USE_LIMIT is False
    assert cfg.EDIS_MORNING_CHECK_ENABLED is True


def test_blank_adaptive_flag_stays_on_and_explicit_false_turns_it_off(reload_all):
    _, eng = reload_all({"RISK_MAX_STOCK_PRICE_ADAPTIVE": " "})
    assert eng.RISK_MAX_STOCK_PRICE_ADAPTIVE is True
    _, eng = reload_all({"RISK_MAX_STOCK_PRICE_ADAPTIVE": "false"})
    assert eng.RISK_MAX_STOCK_PRICE_ADAPTIVE is False


@pytest.mark.parametrize("raw", ["", "   ", "abc"])
def test_blank_or_garbage_max_stock_price_no_longer_crashes_and_is_not_explicit(reload_all, raw):
    _, eng = reload_all({"RISK_MAX_STOCK_PRICE": raw})
    assert eng.MAX_STOCK_PRICE == 3000.0
    if raw.strip() == "":
        assert eng.MAX_STOCK_PRICE_EXPLICITLY_SET is False       # blank must not disable the adaptive ceiling


def test_real_max_stock_price_is_explicit(reload_all):
    _, eng = reload_all({"RISK_MAX_STOCK_PRICE": " 4500 "})
    assert eng.MAX_STOCK_PRICE == 4500.0 and eng.MAX_STOCK_PRICE_EXPLICITLY_SET is True
