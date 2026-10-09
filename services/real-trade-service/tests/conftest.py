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
