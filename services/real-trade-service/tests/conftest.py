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
def _opening_gate_off_by_default(monkeypatch):
    """group 268: entry_engine/opening_gate.py depends on the wall clock (09:15-10:00 IST). Pre-existing tests predate it;
    tests/test_group268_opening_gate.py turns it back on and pins the clock."""
    import config as _cfg
    monkeypatch.setattr(_cfg, "OPENING_GATE_ENABLED", False, raising=False)
    monkeypatch.setattr(_cfg, "OPENING_GATE_SHADOW", False, raising=False)
    from entry_engine import opening_gate as _og
    _og.reset_shadow()
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
                 "ENTRY_LIQUIDITY_GATE_ENABLED"):   # group333: reads the clock + prev-day candles
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
