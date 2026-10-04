"""
tests/conftest.py — lets `pytest tests/` run every file in ONE process.

The service's sub-apps (news/, sentiment/, event/, ...) each have their own
top-level module called `main` (plus a few other shared names), and the test
files import them with a bare `import main as ...`. Python caches modules by
name, so in a single process the second file would silently get the first
file's `main`. Before each test module is collected we drop those cached
names, so every test module imports its own copy from the directory it put on
sys.path.

Per-file runs (`pytest tests/test_x.py`) are unaffected.
"""
from __future__ import annotations

import sys

import pytest

# Top-level module names that exist in more than one sub-app directory.
_COLLIDING_MODULES = (
    "main",
    "kv_cache",
    "rate_limiter",
    "oracle_compat",
    "shared_adaptive",
)


def pytest_collectstart(collector):
    if isinstance(collector, pytest.Module):
        for name in _COLLIDING_MODULES:
            sys.modules.pop(name, None)

import os as _os_logcfg
_os_logcfg.environ.setdefault("HTTPX_LOG_LEVEL", "INFO")  # 2026-10-04: tests assert on httpx INFO lines; production default is WARNING
