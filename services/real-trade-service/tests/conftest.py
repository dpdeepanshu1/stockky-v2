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
