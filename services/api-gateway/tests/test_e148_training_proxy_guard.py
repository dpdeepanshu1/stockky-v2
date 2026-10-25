"""E148 (F-12): the gateway guarded POST /training/train, but the generic /training/{path} proxy forwarded the training service's other
system-level actions (train, train/run, models/promote, lock clears, admin, debug/abort) with no login. They now need the admin session too.
Run from services/api-gateway: python3 -m pytest tests/test_e148_training_proxy_guard.py -v"""
import os
import sys
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parent.parent
if str(_SERVICE) not in sys.path:
    sys.path.insert(0, str(_SERVICE))

import admin_guard  # noqa: E402

needs = admin_guard.training_path_needs_admin


@pytest.mark.parametrize("method,path", [
    ("POST", "api/train"), ("POST", "train/run"), ("POST", "train"), ("POST", "train/clear-lock"),
    ("POST", "api/lock/clear"), ("DELETE", "lock"), ("POST", "api/models/promote/v12"), ("POST", "api/admin/init-schema"),
    ("POST", "debug/abort"),
    # disguises: case, double slash, dot segment, trailing slash, parent segment
    ("POST", "API/Train"), ("POST", "api//train"), ("POST", "api/./train"), ("POST", "api/train/"), ("POST", "api/../api/train"),
    ("PUT", "api/train"), ("PATCH", "api/lock/clear"),
])
def test_system_actions_need_admin(method, path):
    assert needs(method, path) is True


@pytest.mark.parametrize("method,path", [
    # reads never need it (the Training Lab polls these)
    ("GET", "api/train"), ("GET", "api/train/progress"), ("GET", "api/models"), ("OPTIONS", "api/train"), ("HEAD", "lock"),
    # routes the UI uses without a login stay open: paper trades, scan ingest, evaluation, predictions
    ("POST", "api/universe/ingest"), ("POST", "api/universe/train-from-universe"), ("POST", "api/trades/manual"),
    ("POST", "api/portfolio/deposit"), ("POST", "api/predictions"), ("POST", "api/evaluate/t1"), ("POST", "api/actionable/commit"),
    ("POST", "api/train/progress"),
])
def test_other_routes_stay_open(method, path):
    assert needs(method, path) is False


def test_proxy_calls_the_check_before_forwarding():
    src = (_SERVICE / "gateway_support" / "bound_routes_h.py").read_text(encoding="utf-8")
    i_check = src.index("training_path_needs_admin(request.method, path)")
    i_forward = src.index("client.request(", i_check)
    assert i_check < i_forward
    assert "ref._require_admin(authorization=" in src[i_check:i_forward]


def test_empty_and_none_inputs_do_not_crash():
    assert needs("POST", "") is False and needs(None, None) is False
