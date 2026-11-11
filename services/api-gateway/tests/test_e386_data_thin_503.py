"""E386: technical's own 'history thin' 503 must not count against the circuit breaker; an outage 503 still does."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from circuit_breaker import CircuitBreaker, is_data_thin_503  # noqa: E402


class _R:
    def __init__(self, status, body=None, raises=False):
        self.status_code, self._b, self._r = status, body, raises

    def json(self):
        if self._r:
            raise ValueError("no json")
        return self._b


def test_thin_history_503_is_recognised():
    assert is_data_thin_503(_R(503, {"detail": "structure unavailable (history thin or engine error)"}))
    assert is_data_thin_503(_R(503, {"detail": "chart context unavailable (history thin or engine error)"}))


def test_real_outages_and_other_shapes_are_not():
    assert not is_data_thin_503(_R(503, {"detail": "market-structure engine not available"}))
    assert not is_data_thin_503(_R(503, "Service Unavailable"))
    assert not is_data_thin_503(_R(503, None, raises=True))
    assert not is_data_thin_503(_R(500, {"detail": "structure unavailable (history thin or engine error)"}))
    assert not is_data_thin_503(_R(200, {"detail": "structure unavailable (history thin or engine error)"}))
    assert not is_data_thin_503(None) and not is_data_thin_503(object())


def test_main_cb_get_uses_it():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8").read()
    body = src.split("async def _cb_get(")[1].split("async def _cb_post(")[0]
    assert "resp.status_code >= 500 and not is_data_thin_503(resp)" in body
    assert "is_data_thin_503" in src.split("from circuit_breaker import")[1].split("\n")[0]


def test_twelve_thin_symbols_no_longer_open_the_breaker():
    resp = _R(503, {"detail": "structure unavailable (history thin or engine error)"})
    br = CircuitBreaker("t", failure_threshold=12)
    for _ in range(30):                      # the _cb_get rule
        (br.record_failure("HTTP 503") if resp.status_code >= 500 and not is_data_thin_503(resp) else br.record_success())
    assert br.state() == "closed"
    out = _R(503, {"detail": "upstream down"})
    for _ in range(12):
        (br.record_failure("HTTP 503") if out.status_code >= 500 and not is_data_thin_503(out) else br.record_success())
    assert br.state() == "open"
