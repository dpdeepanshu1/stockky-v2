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
def _e266_conviction_sizing_off_by_default(monkeypatch):
    """E266: conviction sizing changes the rupees of an entry (x0.5 .. x3 of the fixed size), which older tests assert on. They run at the fixed size;
    tests/test_e266_conviction_sizing.py turns it on explicitly."""
    import config as _config
    monkeypatch.setattr(_config, "CONVICTION_SIZING_ENABLED", False, raising=False)
    from screening import shortlist as _sl
    _sl.reset_recent()
    yield


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


@_pytest_entry_pause.fixture(autouse=True)
def _group479_guards_off_by_default(monkeypatch):
    """group479: the whole-day stop-out guard, the minimum-quantity check and the ORB recorder are new live rules; older tests predate them and
    expect no behaviour change. tests/test_group479_*.py turn them on explicitly."""
    import config as _config
    monkeypatch.setattr(_config, "STOPOUT_GUARD_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "MANUAL_BUY_WINDOW_GUARD", False, raising=False)   # E220: older manual-BUY tests use fixed dates; test_e220 turns it on itself
    monkeypatch.setattr(_config, "ENTRY_MIN_QTY", 0, raising=False)
    monkeypatch.setattr(_config, "ENTRY_MIN_REWARD_RISK", 0.0, raising=False)   # 2026-10-22 RR_FLOOR is tested in test_rr_floor_1_2.py
    monkeypatch.setattr(_config, "ORB_MODE", "off", raising=False)
    monkeypatch.setattr(_config, "TRADE_ODDS_MODE", "off", raising=False)   # E103: tests that need it turn it on themselves
    monkeypatch.setattr(_config, "PREOPEN_GAP_ATR_MULT", 0.0, raising=False)   # E107: the stock's-own-ATR gap test; tests/test_e107_gap_vix_decisions.py turns it on itself
    monkeypatch.setattr(_config, "VIX_CAUTION_PCTILE", 0.0, raising=False)     # E107: same for the VIX 12-month percentile
    from orders import orb_shadow as _orb
    _orb._reset_for_tests()
    yield
    _orb._reset_for_tests()


@_pytest_entry_pause.fixture(autouse=True)
def _e251_rules_off_by_default(monkeypatch):
    """E251: the stop floor, spike-chase guard, gain-room gate and reclaim re-entry are new live rules that read live feed / prev-day data and change the
    stop numbers; older tests predate them and expect no behaviour change. tests/test_e251_*.py turn them on explicitly (with the production defaults)."""
    import config as _config
    monkeypatch.setattr(_config, "ADAPTIVE_STOP_FLOOR_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "SPIKE_GUARD_MODE", "off", raising=False)
    monkeypatch.setattr(_config, "GAIN_ROOM_MODE", "off", raising=False)
    monkeypatch.setattr(_config, "REENTRY_RECLAIM_ENABLED", False, raising=False)
    # E252: adaptive trail / stagnation read live feed and the candidate log; older tests expect the fixed numbers. tests/test_e252_*.py turn them on explicitly.
    monkeypatch.setattr(_config, "TRAIL_ADAPTIVE_ENABLED", False, raising=False)
    monkeypatch.setattr(_config, "EXIT_ADAPTIVE_ENABLED", False, raising=False)
    yield


@_pytest_entry_pause.fixture(autouse=True)
def _reset_reservation_marker():
    """E212: capital/ledger.py remembers (process-wide) when capital was last reserved; never let one test's reservation block the next test's
    'no open positions -> reset available_capital' step."""
    from capital import ledger as _ledger
    _ledger.reset_reservation_marker()
    yield
    _ledger.reset_reservation_marker()


import pytest as _pytest_e241  # noqa: E402


@_pytest_e241.fixture(autouse=True)
def _reset_security_load_throttle():
    """E241b: dhan_client remembers when the Dhan security list last failed to load (to avoid a download per order); never let that leak between tests."""
    import sys
    def _clear():
        mod = sys.modules.get("execution.dhan_client")
        if mod is not None:
            mod._security_load_failed_at = 0.0
    _clear()
    yield
    _clear()


import pytest as _pytest_e258  # noqa: E402


@_pytest_e258.fixture(autouse=True)
def _circuit_band_lookup_off_by_default(request, monkeypatch):
    """E258: _place_overnight_stop reads the exchange circuit band from the cached /quote row (one HTTP call to market-data). Older tests
    must not make that call, so the lookup answers "unknown" for them; test_e258_overnight_stop_circuit_band.py sets the band itself."""
    if "test_e258" in request.node.nodeid:
        return
    from orders import circuit_gate
    monkeypatch.setattr(circuit_gate, "band_for", lambda symbol: (None, None, None))
