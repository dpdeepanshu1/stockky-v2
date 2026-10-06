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


def _clear_feed_caches(module) -> None:
    for v in list(vars(module).values()):
        cache = getattr(v, "_FEED_CACHE", None)
        if isinstance(cache, dict):
            cache.clear()


@pytest.fixture(autouse=True)
def _reset_event_feed_cache(request):
    """group 144: event/main.py caches the site-wide RSS feeds; every test starts with it empty,
    whichever name the test module imported event/main.py under."""
    _clear_feed_caches(request.module)
    yield
    _clear_feed_caches(request.module)


@pytest.fixture(autouse=True)
def _reset_md_guard():
    """group170: md_guard keeps a timeout streak / cool-down at module level; start every test clean."""
    mod = sys.modules.get("md_guard")
    if mod is not None:
        mod.reset_state()
    yield
    mod = sys.modules.get("md_guard")
    if mod is not None:
        mod.reset_state()


@pytest.fixture(autouse=True)
def _no_google_news_widening(monkeypatch):
    """group201: event/main.py makes a second Google News search when the first returns < 3 items. Existing tests
    mock feedparser.parse / the news sources for ONE call; they run with widening off. The widening itself is
    tested in test_group201_news_coverage.py, which sets EVENT_GN_THIN_BELOW itself."""
    monkeypatch.setenv("EVENT_GN_THIN_BELOW", "0")
