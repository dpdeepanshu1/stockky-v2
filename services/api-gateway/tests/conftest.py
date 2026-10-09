"""Shared pytest setup for services/api-gateway.

* Puts the service directory on sys.path (the gateway is a flat set of top-level modules).
* Isolates every test from the developer's / VM's real environment: no real Redis, DB or
  QStash credentials can leak in, so nothing here ever touches the network or a database.
"""
import os
import sys

import pytest

_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

_ISOLATED_ENV = (
    "USE_REDIS", "DISABLE_REDIS", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "DATABASE_URL", "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_WALLET_PASSWORD",
    "QSTASH_TOKEN", "QSTASH_CURRENT_SIGNING_KEY", "QSTASH_NEXT_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in _ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    yield

import os as _os_logcfg
_os_logcfg.environ.setdefault("HTTPX_LOG_LEVEL", "INFO")  # 2026-10-04: tests assert on httpx INFO lines; production default is WARNING


@pytest.fixture(autouse=True)
def _reset_nse_api_block_and_movers_warning():
    """group178: the NSE api pause and the AngelOne-movers warning throttle are per process; never let one test's
    state leak into the next. Only touches the gateway module if some test already imported it."""
    def _clear():
        mod = sys.modules.get("main")
        if mod is not None and hasattr(mod, "_nse_api_block_until"):
            mod._nse_api_block_until = 0.0
            mod._nse_api_block_skipped = 0
            mod._angelone_movers_warned_at[0] = 0.0
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _g233_market_open_by_default(monkeypatch):
    """group233: with the market closed the gateway's bulk price helpers accept hours-old rows and skip the
    per-symbol /quote leftover. Existing tests were written for an open market and must not depend on the wall
    clock they run at, so the closed check is pinned to False; the group233 tests set it explicitly."""
    gw = sys.modules.get("main")
    if gw is not None and hasattr(gw, "_gw_quotes_closed"):
        monkeypatch.setattr(gw, "_gw_quotes_closed", lambda: False)
    yield


@pytest.fixture(autouse=True)
def _g278_indices_stay_on_yfinance_stub(monkeypatch):
    """group278: /market/indices now asks market-data first. Older tests stub yf.Ticker and must not make a network
    call, so the market-data read is off by default; the group278 tests turn it on explicitly."""
    monkeypatch.setenv("INDICES_VIA_MARKET_DATA", "0")
    monkeypatch.delenv("GATEWAY_DIRECT_YFINANCE_FALLBACK", raising=False)
    # group279: the surprise feed, premarket baselines and repair-RSI also ask market-data first now. Older tests stub
    # yfinance / httpx for the old order, so those reads are off by default; the group279 tests turn them on.
    monkeypatch.setenv("SURPRISE_FEED_VIA_MARKET_DATA", "0")
    monkeypatch.setenv("PREMARKET_BASELINES_VIA_MARKET_DATA", "0")
    monkeypatch.setenv("REPAIR_RSI_VIA_MARKET_DATA", "0")
    yield
