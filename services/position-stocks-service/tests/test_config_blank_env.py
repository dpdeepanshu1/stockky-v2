"""group 74: blank / whitespace-only env values in position-stocks-service/config.py.

`NAME=` in a .env / compose file gives an EMPTY STRING (not "unset"). Before this group:
  * `_get_bool(name, True)` returned False for a blank value, so a stray `LOSS_BRAKE_ENABLED=` /
    `MARKET_GATE_ENABLED=` / `QUALITY_GATE_ENABLED=` ... silently switched a default-True safety gate OFF;
  * ANGELONE_STATIC_IP was kept verbatim, so "   " (truthy) was sent to AngelOne as the client IP;
  * string settings read with os.getenv(NAME, default) (SCALP_PRODUCT_TYPE, EOD_SQUAREOFF_TIME_IST, LOG_LEVEL,
    ADMIN_USERNAME, ...) became "" instead of their default;
  * EOD_SELL_RETRY_DELAY_SECONDS used a bare float() and crashed the import on a blank value.

Run from services/position-stocks-service:  python3 -m pytest tests/test_config_blank_env.py -q
"""
import importlib
import os

import pytest

import config

_TRUE_BY_DEFAULT = (
    "RISK_PER_TRADE_PCT_CONFIRMED", "USE_SUPER_ORDER", "FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", "MARKET_GATE_ENABLED",
    "LOSS_BRAKE_ENABLED", "SYMBOL_BLOCK_AFTER_LOSS_TODAY", "QUALITY_GATE_ENABLED",
)
_STR_DEFAULTS = {
    "ANGELONE_STATIC_IP": "", "ANGELONE_WS_URL": "wss://smartapisocket.angelone.in/smart-stream",
    "SCAN_UNIVERSE_SOURCE": "all_nse_eq", "SCALP_PRODUCT_TYPE": "INTRADAY", "SCALP_EXCHANGE_SEGMENT": "NSE_EQ",
    "EOD_SQUAREOFF_TIME_IST": "15:00", "EDIS_MORNING_CHECK_TIME_IST": "09:00", "LOG_LEVEL": "INFO",
    "ADMIN_USERNAME": "admin",
}


class TestGetStr:
    @pytest.mark.parametrize("raw", [None, "", " ", "  \t\n"])
    def test_missing_blank_or_whitespace_gives_default(self, monkeypatch, raw):
        if raw is None:
            monkeypatch.delenv("X_STR", raising=False)
        else:
            monkeypatch.setenv("X_STR", raw)
        assert config._get_str("X_STR", "dflt") == "dflt"

    def test_value_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("X_STR", "  INTRADAY \n")
        assert config._get_str("X_STR", "dflt") == "INTRADAY"


class TestGetBool:
    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize("raw", ["", " ", "\t"])
    def test_blank_means_unset_so_the_default_applies(self, monkeypatch, raw, default):
        monkeypatch.setenv("X_BOOL", raw)
        assert config._get_bool("X_BOOL", default) is default

    @pytest.mark.parametrize("default", [True, False])
    def test_missing_gives_default(self, monkeypatch, default):
        monkeypatch.delenv("X_BOOL", raising=False)
        assert config._get_bool("X_BOOL", default) is default

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On"])
    def test_truthy_spellings(self, monkeypatch, raw):
        monkeypatch.setenv("X_BOOL", raw)
        assert config._get_bool("X_BOOL", False) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "nope"])
    def test_explicit_false_still_disables_a_default_true_flag(self, monkeypatch, raw):
        monkeypatch.setenv("X_BOOL", raw)
        assert config._get_bool("X_BOOL", True) is False


@pytest.fixture
def reloaded():
    saved = {k: os.environ.get(k) for k in (*_TRUE_BY_DEFAULT, *_STR_DEFAULTS, "EOD_SELL_RETRY_DELAY_SECONDS")}
    yield lambda **env: _reload(env)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(config)


def _reload(env):
    for k in (*_TRUE_BY_DEFAULT, *_STR_DEFAULTS, "EOD_SELL_RETRY_DELAY_SECONDS"):
        os.environ.pop(k, None)
    os.environ.update(env)
    return importlib.reload(config)


def test_blank_safety_gate_flags_stay_enabled(reloaded):
    cfg = reloaded(**{k: "" for k in _TRUE_BY_DEFAULT})
    for k in _TRUE_BY_DEFAULT:
        assert getattr(cfg, k) is True, k


def test_explicit_false_still_turns_a_gate_off(reloaded):
    cfg = reloaded(LOSS_BRAKE_ENABLED="false", MARKET_GATE_ENABLED=" 0 ")
    assert cfg.LOSS_BRAKE_ENABLED is False and cfg.MARKET_GATE_ENABLED is False
    assert cfg.QUALITY_GATE_ENABLED is True


def test_blank_string_settings_use_their_defaults(reloaded):
    cfg = reloaded(**{k: "  " for k in _STR_DEFAULTS})
    for k, default in _STR_DEFAULTS.items():
        assert getattr(cfg, k) == default, k


def test_padded_string_settings_are_trimmed(reloaded):
    cfg = reloaded(ANGELONE_STATIC_IP=" 203.0.113.7 ", SCALP_PRODUCT_TYPE=" CNC ", EOD_SQUAREOFF_TIME_IST=" 15:10")
    assert cfg.ANGELONE_STATIC_IP == "203.0.113.7"
    assert cfg.SCALP_PRODUCT_TYPE == "CNC" and cfg.EOD_SQUAREOFF_TIME_IST == "15:10"


@pytest.mark.parametrize("raw", ["", "  ", "abc"])
def test_blank_or_garbage_eod_retry_delay_no_longer_crashes_import(reloaded, raw):
    assert reloaded(EOD_SELL_RETRY_DELAY_SECONDS=raw).EOD_SELL_RETRY_DELAY_SECONDS == 2.0
    assert reloaded(EOD_SELL_RETRY_DELAY_SECONDS="3.5").EOD_SELL_RETRY_DELAY_SECONDS == 3.5


def test_angelone_session_ignores_a_whitespace_only_static_ip(monkeypatch):
    from feed import angelone_session as aosess
    monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", "   ")
    monkeypatch.setitem(aosess._outbound_ip_cache, "ip", "198.51.100.9")
    monkeypatch.setitem(aosess._outbound_ip_cache, "at", __import__("time").time())
    assert aosess._resolve_client_public_ip() == "198.51.100.9"
    monkeypatch.setattr(aosess.config, "ANGELONE_STATIC_IP", " 203.0.113.7 ")
    assert aosess._resolve_client_public_ip() == "203.0.113.7"
