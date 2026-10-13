import os as _os_logcfg
_os_logcfg.environ.setdefault("HTTPX_LOG_LEVEL", "INFO")  # 2026-10-04: tests assert on httpx INFO lines; production default is WARNING


import pytest as _pytest_entry_pause  # noqa: E402


@_pytest_entry_pause.fixture(autouse=True)
def _reset_entry_pause():
    """group209: orders/entry_pause.py keeps process-local pause state; never let one test's rejection
    pause (or rest a symbol for) the next test's entry."""
    from orders import entry_pause
    entry_pause.reset()
    yield
    entry_pause.reset()


@_pytest_entry_pause.fixture(autouse=True)
def _opening_gate_off_by_default(monkeypatch):
    """group 268: screening/opening_gate.py is time-of-day dependent (active 09:15-10:00 IST on a trading day). Switch it off
    for every older test so none of them can flake when the suite runs inside that window; test_group268_opening_gate.py
    turns it back on and pins the clock explicitly."""
    import config as _config
    monkeypatch.setattr(_config, "OPENING_GATE_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "OPENING_GATE_SHADOW", False, raising=False)
    from screening import opening_gate as _og
    _og.reset_shadow()
    from screening import prev_day as _pd
    _pd.reset()
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _board_gate_off_by_default(monkeypatch):
    """group309: orders/board_gate.py queues a shadow row (background writer) for every entry it looks at. Switch it off for every
    older test; test_group309_board_log_gate_report.py turns it back on explicitly."""
    import config as _config
    monkeypatch.setattr(_config, "BOARD_GATE_MODE", "off", raising=False)
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _reset_depth_proven():
    """group322: orders/depth_gate remembers (process-wide) that depth data has been seen; never let one test's reading
    switch on the 'refuse a name without depth' rule for the next test."""
    from orders import depth_gate as _dg
    _dg._depth_proven_at = 0.0
    yield
    _dg._depth_proven_at = 0.0


@_pytest_entry_pause.fixture(autouse=True)
def _group330_gates_off_by_default(monkeypatch):
    """group330: the VIX / negative-news checks call market-data and the event service over the network and the pre-open snapshot depends on
    the clock. Older tests predate them, so they are off unless a test turns them on (tests/test_group330_*.py do)."""
    import config as _config
    for name in ("VIX_GATE_ENABLED", "NEWS_NEGATIVE_BLOCK_ENABLED", "PREOPEN_SNAPSHOT_ENABLED", "ENTRY_VWAP_GATE_ENABLED",
                 "ENTRY_LIQUIDITY_GATE_ENABLED",   # group333: VWAP reads the clock
                 "CALENDAR_RULES_ENABLED", "WINDOW_AUTODISABLE_ENABLED"):   # group336: calendar reads the clock, window health reads trade history
        monkeypatch.setattr(_config, name, False, raising=False)
    monkeypatch.setattr(_config, "CATALYST_NO_NEWS_ACTION", "off", raising=False)
    from screening import preopen as _po, vix_gate as _vg
    from orders import window_health as _wh
    _wh.reset()
    _po.reset()
    _vg.reset()
    yield
    _po.reset()
    _vg.reset()


@_pytest_entry_pause.fixture(autouse=True)
def _group340_off_by_default(monkeypatch):
    """group340: these change numbers or call the network, so older tests run with them off (tests/test_group340.py turns them on)."""
    import config as _config
    monkeypatch.setattr(_config, "SCALP_TARGET_CAP_PCT", 0.0, raising=False)
    monkeypatch.setattr(_config, "ADAPTIVE_HISTORY_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "SIZING_ROUNDING_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "STOP_VERIFY_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "STOP_WATCHDOG_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "ENTRY_MIN_PRICE", 0.0, raising=False)
    monkeypatch.setattr(_config, "SCALP_ENTRY_CUTOFF_IST", "", raising=False)
    from orders import adaptive as _ad
    _ad.reset_history_cache()
    yield
    _ad.reset_history_cache()
