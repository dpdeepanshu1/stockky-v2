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
