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
    # group302 staggers the boot warms (8 s market open / 90 s closed); tests await the warm for fractions of a second.
    # group 453: the admin guard is ON in production; the pre-existing route tests call the protected routes without a token, so they run
    # with it off. tests/test_admin_guard.py turns it back on explicitly.
    monkeypatch.setenv("GATEWAY_ADMIN_GUARD", "false")
    monkeypatch.setenv("GATEWAY_BOOT_WARM_DELAY_S", "0")
    monkeypatch.setenv("GATEWAY_BOOT_WARM_CLOSED_DELAY_S", "0")
    monkeypatch.setenv("SURPRISE_BOOT_WARM_DELAY_SEC", "0")
    monkeypatch.setenv("GATEWAY_BOOT_WARM_SURPRISE_WAIT_S", "0")   # the surprise warm waits up to 120 s for the movers warm; tests never run that one
    # the boot-warm asyncio.Event is a process-wide singleton bound to the first event loop that used it; every test
    # runs its own loop (asyncio.run), so drop it before each test
    _mod = sys.modules.get("main")
    if _mod is not None and hasattr(_mod, "_boot_warm_movers_done"):
        monkeypatch.setattr(_mod, "_boot_warm_movers_done", None)
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


# ---------------------------------------------------------------------------------------------------------------------
# group317: no real network from a unit test. 19 tests used to resolve the production hostnames (market-data, analysis-
# intelligence, Google News, NSE): on the VM that is a real request to a live service, in CI it is a flaky DNS lookup.
# A blocked attempt is recorded and fails the test that made it, even when the code under test swallows the exception.
# Escape hatch for a deliberate live run:  ALLOW_TEST_NETWORK=1 python -m pytest ...
# ---------------------------------------------------------------------------------------------------------------------
import socket as _socket

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "", "testserver", "test"}


def _host_of(addr):
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0])
    return str(addr)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    if os.environ.get("ALLOW_TEST_NETWORK") == "1":
        yield
        return
    blocked = []
    real_gai, real_connect = _socket.getaddrinfo, _socket.socket.connect

    def _gai(host, *a, **k):
        if host is not None and _host_of(host) not in _LOCAL_HOSTS:
            blocked.append(f"dns {host}")
            raise _socket.gaierror(_socket.EAI_NONAME, f"real network blocked in unit tests: {host}")
        return real_gai(host, *a, **k)

    def _connect(self, addr):
        if self.family in (_socket.AF_INET, _socket.AF_INET6) and _host_of(addr) not in _LOCAL_HOSTS:
            blocked.append(f"connect {_host_of(addr)}")
            raise OSError(f"real network blocked in unit tests: {_host_of(addr)}")
        return real_connect(self, addr)

    monkeypatch.setattr(_socket, "getaddrinfo", _gai)
    monkeypatch.setattr(_socket.socket, "connect", _connect)
    yield
    if blocked:
        pytest.fail("test made a real network call (stub it): " + ", ".join(sorted(set(blocked))), pytrace=False)
