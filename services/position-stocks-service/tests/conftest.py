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
