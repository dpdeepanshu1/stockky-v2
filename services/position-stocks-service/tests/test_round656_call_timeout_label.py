"""round 656: a lone Oracle call timeout (DPY-4024) in the trading loop is logged as a WARNING with the real pool numbers, not as
"DB pool exhausted" with a traceback; a genuine pool error keeps the old ERROR path."""
import pathlib
import re

SRC = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")


def _handler():
    i = SRC.index("_pool_err = ")
    return SRC[i:i + 1800]


def test_call_timeout_gets_a_warning_without_traceback_or_breaker_trip():
    h = _handler()
    head = h[:h.index("logger.error(\"position-stocks trading loop error")]
    assert 'if "DPY-4024" in str(e) and not _pool_err:' in head
    assert "logger.warning(" in head and "_pool_status_text()" in head
    assert "await asyncio.sleep(5)" in head and "continue" in head
    assert "record_failure" not in head and "exc_info" not in head


def test_real_pool_errors_keep_the_pool_exhausted_error_path():
    h = _handler()
    tail = h[h.index("logger.error(\"position-stocks trading loop error"):]
    assert "if _pool_err:" in tail
    assert re.search(r'DB pool exhausted: %s", _pool_status_text\(\)', tail)
    assert "circuit_breaker.record_failure()" in tail
