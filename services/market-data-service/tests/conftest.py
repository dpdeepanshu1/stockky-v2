import os as _os_logcfg
import sys
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
def _g233_market_open_by_default(monkeypatch):
    """group233: /quote and /quotes/bulk answer a CLOSED market from the last close. Existing tests were written for
    an open market and must not depend on the wall clock they run at, so the closed check is pinned to False;
    the group233 tests set it to True explicitly."""
    _m = sys.modules.get("main")
    if _m is not None and hasattr(_m, "_quote_market_closed"):
        monkeypatch.setattr(_m, "_quote_market_closed", lambda: False)
    yield
