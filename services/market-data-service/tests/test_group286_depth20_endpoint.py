"""group286: GET /depth/{symbol} is always HTTP 200 with `available`."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("fastapi")


def test_depth_route_registered_and_fails_open(monkeypatch):
    import main
    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert "/depth/{symbol}" in paths
    import dhan_data
    monkeypatch.setattr(dhan_data.depth20, "get", lambda s, qty=None, slip_pct=None, wait_s=None:
                        {"symbol": s, "available": False, "reason": "depth20_disabled", "qty": qty, "slip": slip_pct})
    r = main.depth20_book("ABC", qty=10, slip_pct=0.3, wait_s=0)
    assert r["available"] is False and r["qty"] == 10 and r["slip"] == 0.3


def test_depth_route_swallows_errors(monkeypatch):
    import main
    import dhan_data
    monkeypatch.setattr(dhan_data.depth20, "get", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    r = main.depth20_book("abc")
    assert r == {"symbol": "ABC", "available": False, "reason": "error:ValueError"}
