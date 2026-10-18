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
    from screening import prev_day as _pd
    _pd.reset()
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _board_gate_off_by_default(monkeypatch):
    """group309: orders/board_gate.py queues a shadow row (background writer) for every entry it looks at. Switch it off for every
    older test; test_group309_board_log_gate_report.py turns it back on explicitly."""
    import config as _config
    monkeypatch.setattr(_config, "BOARD_GATE_MODE", "off", raising=False)
    # group354: both are LIVE by default now; older tests exercise the legacy board and an entry with no structure data
    monkeypatch.setattr(_config, "BOARD_USE_MS_ENGINE", False, raising=False)
    monkeypatch.setattr(_config, "STRUCTURE_GATE_MODE", "off", raising=False)
    # group377: production default is REQUIRE_DATA=true; older tests have no structure summary and expect no behaviour change (tests that want it set it)
    monkeypatch.setattr(_config, "STRUCTURE_REQUIRE_DATA", False, raising=False)
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
    # group341: older tests assume the pre-group341 behaviour; tests/test_group341.py turns these on.
    monkeypatch.setattr(_config, "SCALP_POOL_COMMITTED_MARGIN_FRAC", 1.0, raising=False)
    monkeypatch.setattr(_config, "SCALP_SLOT_NOTIONAL_CAP_MULT", 0.0, raising=False)
    monkeypatch.setattr(_config, "QUALITY_GATE_RESULT_TTL_S", 0.0, raising=False)
    monkeypatch.setattr(_config, "QUALITY_GATE_EVENT_MEMORY_H", 0.0, raising=False)
    # group344: older tests use cheap fixture prices (Rs10-30) and fake event calls that never time out; tests/test_group344.py turns these on.
    monkeypatch.setattr(_config, "PRICE_FLOOR_BEFORE_GATE", False, raising=False)
    monkeypatch.setattr(_config, "QUALITY_GATE_EVENT_FAIL_BLOCK", False, raising=False)
    from orders import adaptive as _ad
    _ad.reset_history_cache()
    yield
    _ad.reset_history_cache()


@_pytest_entry_pause.fixture(autouse=True)
def _event_shared_fetch_off_by_default(monkeypatch):
    """group356: quality_gate runs the event call as one shared in-flight request by default. Older tests patch _fetch_event_signal /
    the httpx client and count the group344 retry call directly, so they run the legacy path; test_group356_event_shared_fetch.py
    turns the shared path on explicitly."""
    import config as _config
    from screening import quality_gate as _qg
    monkeypatch.setattr(_config, "QUALITY_GATE_EVENT_SHARED_FETCH", False, raising=False)
    _qg._event_late.clear()
    _qg._event_inflight.clear()
    yield
    _qg._event_late.clear()
    _qg._event_inflight.clear()


@_pytest_entry_pause.fixture(autouse=True)
def _live_tick_requirement_off_by_default(monkeypatch):
    """group389: production default is ENTRY_REQUIRE_LIVE_TICK=on (an automatic entry with no tick for the symbol is skipped).
    Older entry tests build candidates without any tick buffer and expect no change; test_group389_require_live_tick.py turns it on."""
    import config as _config
    monkeypatch.setattr(_config, "ENTRY_REQUIRE_LIVE_TICK", False, raising=False)
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _opening_protect_off_by_default(monkeypatch):
    """group392: orders/opening_protect.py is time-of-day dependent (09:15-09:45 IST on a trading day: half size, pause after stop-outs).
    Off for every older test so none can flake inside that window; test_group392_opening_protect.py turns it on and pins the clock."""
    import config as _config
    monkeypatch.setattr(_config, "OPENING_PROTECT_ENABLED", False, raising=False)
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _bar_record_off_by_default(monkeypatch):
    """group441: feed/bar_recorder.py writes files under BAR_RECORD_DIR; no older test may touch the disk (test_group441_bar_recorder.py turns it on in a temp dir)."""
    import config as _config
    monkeypatch.setattr(_config, "BAR_RECORD_ENABLED", False, raising=False)
    from feed import bar_recorder as _br
    _br.reset()
    yield
