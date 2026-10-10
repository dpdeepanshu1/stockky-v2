import os as _os_logcfg
import sys
_os_logcfg.environ.setdefault("BHAVCOPY_PREWARM", "0")  # group234: TestClient startup must not download bhavcopy
_os_logcfg.environ.setdefault("HTTPX_LOG_LEVEL", "INFO")  # 2026-10-04: tests assert on httpx INFO lines; production default is WARNING


import pytest as _pytest_g188


@_pytest_g188.fixture(autouse=True)
def _g188_clear_bo_miss_memory():
    """group188: the Yahoo .BO miss memory is per process; a test that simulates a miss must not leak into the next."""
    _m = sys.modules.get("main")
    if _m is not None and hasattr(_m, "_YF_BO_MISS"):
        _m._YF_BO_MISS.clear()
    yield
    _m = sys.modules.get("main")
    if _m is not None and hasattr(_m, "_YF_BO_MISS"):
        _m._YF_BO_MISS.clear()


@_pytest_g188.fixture(autouse=True)
def _g211_reset_angelone_budget():
    """group211: the AngelOne budget (global cooldown, lane counts, held/hot symbol caches) and the feed's cold-poll
    clock are per process; one test tripping a cooldown must not make the next test's AngelOne call skip."""
    def _reset():
        _b = sys.modules.get("angelone_budget")
        if _b is not None:
            _b._reset()
        _f = sys.modules.get("angelone_ws_feed")
        if _f is not None and hasattr(_f, "_last_cold_poll"):
            _f._last_cold_poll = 0.0
    _reset()
    yield
    _reset()


@_pytest_g188.fixture(autouse=True)
def _g299_reset_rate_limiter_cooldowns():
    """group299: rate_limiter's provider cooldown map is per process. A test that simulates a 403/429 (for example
    test_group211's candle 403) sets a cooldown that outlives the test, and the next test that walks the quote
    waterfall then skips Yahoo/AngelOne for up to its length - the group233/group235 tests failed in a full run only."""
    def _reset():
        _r = sys.modules.get("rate_limiter")
        if _r is not None and hasattr(_r, "_cooldowns"):
            _r._cooldowns.clear()
    _reset()
    yield
    _reset()


@_pytest_g188.fixture(autouse=True)
def _g233_market_open_by_default(monkeypatch):
    """group233: /quote and /quotes/bulk answer a CLOSED market from the last close. Existing tests were written for
    an open market and must not depend on the wall clock they run at, so the closed check is pinned to False;
    the group233 tests set it to True explicitly."""
    _m = sys.modules.get("main")
    if _m is not None and hasattr(_m, "_quote_market_closed"):
        monkeypatch.setattr(_m, "_quote_market_closed", lambda: False)
    yield


@_pytest_g188.fixture(autouse=True)
def _g270_dhan_off_by_default(monkeypatch):
    """group270: the Dhan Data API stage is ON by default in production. Existing tests were written for the
    AngelOne/yfinance paths and must not start Dhan threads or touch the network, so it is switched off for every
    test; the group270 tests turn it on explicitly with their own monkeypatch."""
    monkeypatch.setenv("DHAN_DATA_ENABLED", "0")
    yield
