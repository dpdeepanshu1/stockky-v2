"""2026-10-04: httpx -> WARNING and successful /health access lines dropped (the bulk of the log volume)."""
import logging, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402


def _rec(msg):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, msg, None, None)


def _filters():
    return [f for f in logging.getLogger("uvicorn.access").filters if type(f).__name__ == "_NoHealthAccess"]


def test_defaults_mute_httpx_and_drop_ok_health(monkeypatch):
    monkeypatch.delenv("HTTPX_LOG_LEVEL", raising=False)
    monkeypatch.delenv("ACCESS_LOG_HEALTH", raising=False)
    h, c = logging.getLogger("httpx"), logging.getLogger("httpcore")
    old = (h.level, c.level, list(logging.getLogger("uvicorn.access").filters))
    try:
        for f in _filters():
            logging.getLogger("uvicorn.access").removeFilter(f)
        m._quiet_noisy_loggers()
        assert h.level == logging.WARNING and c.level == logging.WARNING
        (flt,) = _filters()
        assert flt.filter(_rec('172.18.0.1:52036 - "GET /health HTTP/1.1" 200')) is False
        assert flt.filter(_rec('172.18.0.7:1 - "GET /fundamental/health?warm=true HTTP/1.1" 200')) is False
        assert flt.filter(_rec('127.0.0.1:1 - "GET /health HTTP/1.1" 503')) is True       # failures still log
        assert flt.filter(_rec('172.18.0.1:1 - "GET /market/indices?force_refresh=false HTTP/1.1" 200')) is True
        assert flt.filter(_rec('172.18.0.1:1 - "POST /health HTTP/1.1" 200')) is True
        m._quiet_noisy_loggers()
        assert len(_filters()) == 1                                                        # idempotent
    finally:
        h.setLevel(old[0]); c.setLevel(old[1])
        for f in _filters():
            logging.getLogger("uvicorn.access").removeFilter(f)


def test_env_overrides(monkeypatch):
    h = logging.getLogger("httpx")
    old = h.level
    try:
        monkeypatch.setenv("HTTPX_LOG_LEVEL", "info")
        monkeypatch.setenv("ACCESS_LOG_HEALTH", "1")
        for f in _filters():
            logging.getLogger("uvicorn.access").removeFilter(f)
        m._quiet_noisy_loggers()
        assert h.level == logging.INFO and not _filters()
        monkeypatch.setenv("HTTPX_LOG_LEVEL", "bogus")
        m._quiet_noisy_loggers()
        assert h.level == logging.WARNING
    finally:
        h.setLevel(old)
