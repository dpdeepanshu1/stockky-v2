import os as _os_logcfg
_os_logcfg.environ.setdefault("HTTPX_LOG_LEVEL", "INFO")  # 2026-10-04: tests assert on httpx INFO lines; production default is WARNING


import sys as _sys_g172
import pytest as _pytest_g172


@_pytest_g172.fixture(autouse=True)
def _reset_volume_shock_history_state():
    """group172: the volume_shock "no daily history" pause and failure reasons are per process; never let one
    test's entries leak into the next. Only touches the module if some test already imported it."""
    def _clear():
        mod = _sys_g172.modules.get("candidate_engine.candidates")
        if mod is not None and hasattr(mod, "clear_history_state"):
            mod.clear_history_state()
        if mod is not None and hasattr(mod, "_MCAP_LAST_GOOD"):
            mod._MCAP_LAST_GOOD.clear()     # group186: last-good market cap is per process
    _clear()
    yield
    _clear()


@_pytest_g172.fixture(autouse=True)
def _opening_entry_guard_off_by_default(monkeypatch):
    """group 220: the opening entry guard depends on the wall clock (09:15-09:30 IST). Every pre-existing test predates
    it and must not flake when the suite happens to run in that window, so it is off unless a test turns it on
    (tests/test_group220_opening_guard.py does)."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "OPENING_ENTRY_GUARD_ENABLED", False)



@_pytest_g172.fixture(autouse=True)
def _opening_protect_off_by_default(monkeypatch):
    """group 394: entry_engine/opening_protect.py depends on the wall clock (09:15-09:45 IST: half size, pause after stop-outs). Pre-existing
    tests predate it; tests/test_group394_opening_protect.py turns it back on and pins the clock."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "OPENING_PROTECT_ENABLED", False, raising=False)


@_pytest_g172.fixture(autouse=True)
def _opening_gate_off_by_default(monkeypatch):
    """group 268: entry_engine/opening_gate.py depends on the wall clock (09:15-10:00 IST). Pre-existing tests predate it;
    tests/test_group268_opening_gate.py turns it back on and pins the clock."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "OPENING_GATE_ENABLED", False, raising=False)
    from entry_engine import prev_day as _pd
    _pd.reset()
    yield


@_pytest_g172.fixture(autouse=True)
def _daily_report_off_by_default(monkeypatch):
    """group292: the end-of-day report step runs inside every schedule tick once the clock passes DAILY_REPORT_TIME_IST on a
    weekday. Older tests drive _schedule_tick_body with a patched clock and must not pick up an extra DB read / write (or a
    Telegram push) depending on the hour the suite happens to run, so it is off unless a test turns it on
    (tests/test_group292_daily_report*.py do)."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "DAILY_REPORT_ENABLED", False, raising=False)


@_pytest_g172.fixture(autouse=True)
def _group329_gates_off_by_default(monkeypatch):
    """group329: the results-day gates would call the event service over the network, and the weekend carry bar depends on the weekday
    the suite happens to run. Every older test predates them, so they are off unless a test turns them on
    (tests/test_group329_circuit_earnings.py and test_group329_depth_weekend.py do)."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "EARNINGS_DAY_BLOCK", False, raising=False)
    monkeypatch.setattr(_cfg, "EARNINGS_CARRY_BLOCK", False, raising=False)
    monkeypatch.setattr(_cfg, "OVERNIGHT_HOLD_EXTRA_GAIN_PER_CLOSED_DAY_PCT", 0.0, raising=False)
    yield


@_pytest_g172.fixture(autouse=True)
def _group330_gates_off_by_default(monkeypatch):
    """group330: the VIX / negative-news / carry-context checks call market-data, the event service and the gateway over the network, and the
    pre-open snapshot depends on the clock. Older tests predate them, so they are off unless a test turns them on
    (tests/test_group330_*.py do). The VWAP gate needs no switch: a tick without `vwap` never blocks."""
    import config as _cfg
    for name in ("VIX_GATE_ENABLED", "NEWS_NEGATIVE_BLOCK_ENABLED", "CARRY_CONTEXT_GATE_ENABLED", "PREOPEN_SNAPSHOT_ENABLED", "GLOBAL_CUES_ENABLED",
                 "ENTRY_LIQUIDITY_GATE_ENABLED",   # group333: reads the clock + prev-day candles
                 "CALENDAR_RULES_ENABLED"):   # group336: expiry / pre-holiday / Friday rules read the clock
        monkeypatch.setattr(_cfg, name, False, raising=False)
    yield


@_pytest_g172.fixture(autouse=True)
def _board_nudge_off_by_default(monkeypatch):
    """group332: the Market Board nudge makes an HTTP call to position-stocks-service; no pre-existing test wants (or can reach) it.
    tests/test_group332_shortlist.py turns it back on with a stubbed client."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ENTRY_BOARD_NUDGE_ENABLED", False, raising=False)


@_pytest_g172.fixture(autouse=True)
def _regime_gate_on_for_legacy_tests(monkeypatch):
    """group332: the market-regime gate is OFF by default now (a weak market must not stop BUYs). The pre-existing regime-gate tests
    exercise the gate's own logic, so they run with it switched back on; tests/test_group332_shortlist.py pins the new default."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ENTRY_REGIME_GATE_ENABLED", True, raising=False)


@_pytest_g172.fixture(autouse=True)
def _legacy_weak_market_blocks_on(monkeypatch):
    """group332: the VIX block, VIX carry block and Nifty-weak carry block default to OFF now (a weak / fearful market must not stop
    a BUY or force a sale). The pre-existing group330/331 tests exercise those rules' own logic, so they run with the old values;
    tests/test_group332_shortlist.py pins the new defaults from config.py's source."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "VIX_BLOCK_ABOVE", 24.0, raising=False)
    monkeypatch.setattr(_cfg, "VIX_CARRY_BLOCK_ABOVE", 20.0, raising=False)
    monkeypatch.setattr(_cfg, "VIX_CARRY_SPIKE_PCT", 6.0, raising=False)
    monkeypatch.setattr(_cfg, "CARRY_MIN_NIFTY_CHANGE_PCT", -0.8, raising=False)


@_pytest_g172.fixture(autouse=True)
def _g344_legacy_defaults(monkeypatch):
    """group344: the watchlist park memory is per process (row ids repeat across the tests' fresh databases) and the pre-open warm-up now stops
    at the 09:15 IST open (wall-clock dependent). Older tests assume neither; tests/test_group344_log_review.py sets what it needs."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "PREOPEN_WARM_STOP_AT_OPEN", False, raising=False)
    monkeypatch.setenv("WATCHLIST_PARK_S", "0")
    try:
        from entry_engine import entry as _entry
        _entry._wl_parked.clear()
        from watchlist_engine import sources as _src
        _src._warm_wait_fails[0] = 0
    except Exception:
        pass
    yield


@_pytest_g172.fixture(autouse=True)
def _structure_gate_no_network_by_default(monkeypatch):
    """group 351: entry_engine/structure_gate.py would fetch the daily structure from the api-gateway for each candidate. Pre-existing
    tests must never hit the network (and no summary = no opinion = no behaviour change); tests/test_group351_structure_gate.py turns it on."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "STRUCTURE_FETCH_ENABLED", False, raising=False)
    # group377: production default is REQUIRE_DATA=true (no structure = WAIT). Pre-existing entry tests have no summary and expect no behaviour change.
    monkeypatch.setattr(_cfg, "STRUCTURE_REQUIRE_DATA", False, raising=False)
    # group377: BROKER_STOP_ENABLED is ON by default; pre-existing exit tests must not run the broker-stop sync. test_group322 / test_group377 turn it on.
    monkeypatch.setattr(_cfg, "BROKER_STOP_ENABLED", False, raising=False)
    mod = _sys_g172.modules.get("entry_engine.structure_gate")
    if mod is not None:
        mod.reset()


@_pytest_g172.fixture(autouse=True)
def _kronos_gate_off_by_default(monkeypatch):
    """group 425: entry_engine/kronos_gate.py would call the kronos-forecast-service worker. Pre-existing tests must never hit the network or be
    changed by a KRONOS_* variable in the environment; tests/test_group425_* turn it on."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "KRONOS_GATE_MODE", "off", raising=False)
    monkeypatch.setattr(_cfg, "KRONOS_GATE_DEMO_MODE", "off", raising=False)
    mod = _sys_g172.modules.get("entry_engine.kronos_gate")
    if mod is not None:
        mod.reset()


@_pytest_g172.fixture(autouse=True)
def _timesfm_gate_off_by_default(monkeypatch):
    """group 434: entry_engine/timesfm_gate.py (Gate 4g, log only) would call the timesfm-forecast-service cache. Pre-existing tests must never hit the
    network or be changed by a TIMESFM_* variable in the environment; tests/test_group434_timesfm_gate.py turns it on."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "TIMESFM_GATE_MODE", "off", raising=False)
    monkeypatch.setattr(_cfg, "TIMESFM_GATE_DEMO_MODE", "off", raising=False)
    mod = _sys_g172.modules.get("entry_engine.timesfm_gate")
    if mod is not None:
        mod.reset()


@_pytest_g172.fixture(autouse=True)
def _exit_hold_audit_every_cycle(monkeypatch):
    """group368: existing exit tests expect one HOLD audit row per evaluation; the throttle is off unless a test turns it on."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "EXIT_HOLD_AUDIT_SECONDS", 0, raising=False)
    mod = _sys_g172.modules.get("exit_engine.exit")
    if mod is not None and hasattr(mod, "_HOLD_AUDIT_LAST"):
        mod._HOLD_AUDIT_LAST.clear()


@_pytest_g172.fixture(autouse=True)
def _late_entry_cutoff_off_by_default(monkeypatch):
    """group 470: calendar_gate.late_entry_block_reason() reads the wall clock (no new entry from ENTRY_LAST_TIME_IST, 14:00 IST). Older
    tests predate it and must not depend on the hour the suite runs; tests/test_group470_review_fixes.py pins the time and sets it itself."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ENTRY_LAST_TIME_IST", "", raising=False)


@_pytest_g172.fixture(autouse=True)
def _overnight_hold_on_for_older_tests(monkeypatch):
    """group 470: the shipped OVERNIGHT_HOLD_ENABLED default is now false. Carry tests written before that exercise the selection logic and
    assume it is on; tests/test_blank_flag_env.py and tests/test_group470_review_fixes.py check the real default."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "OVERNIGHT_HOLD_ENABLED", True, raising=False)


@_pytest_g172.fixture(autouse=True)
def _stop_overshoot_allowance_off_for_older_tests(monkeypatch):
    """group 470: older sizing tests assert exact share counts from the plain risk cap; the allowance is tested in test_group470_review_fixes.py."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "STOP_OVERSHOOT_ALLOWANCE_PCT", 0.0, raising=False)


@_pytest_g172.fixture(autouse=True)
def _stopout_guard_off_for_older_tests(monkeypatch):
    """group 471: the whole-day stop-out guard reads closed positions; older entry tests predate it. tests/test_group471_stopout_guard.py turns it on."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "STOPOUT_GUARD_ENABLED", False, raising=False)


@_pytest_g172.fixture(autouse=True)
def _min_qty_off_for_older_tests(monkeypatch):
    """group 472: older REAL entry tests use 1-2 share positions; the minimum is checked in tests/test_group472_min_qty.py."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "ENTRY_MIN_QTY", 0, raising=False)
