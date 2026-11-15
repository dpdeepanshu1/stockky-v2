"""round 579: orders/candidate_log_writer.py no longer calls the deprecated datetime.utcnow(); its naive-UTC helper gives the same value."""
from __future__ import annotations

import os
import sys
import warnings
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orders import candidate_log_writer as w  # noqa: E402


def test_utcnow_helper_is_naive_and_is_utc():
    n = w._utcnow()
    assert n.tzinfo is None
    ref = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((ref - n).total_seconds()) < 5          # UTC, not IST (5.5 h) and not local time


def test_utcnow_helper_matches_the_old_call_to_the_second():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old = datetime.utcnow()
    assert abs((old - w._utcnow()).total_seconds()) < 5


def test_module_does_not_call_the_deprecated_function_any_more():
    src = open(w.__file__, encoding="utf-8").read()
    assert "datetime.utcnow(" not in src.replace("the deprecated utcnow() call", "")


def test_submit_stamps_a_naive_utc_created_at(monkeypatch):
    seen = []
    monkeypatch.setattr(w, "is_duplicate", lambda *a, **k: False)
    monkeypatch.setattr(w, "_ensure_thread", lambda: None)
    monkeypatch.setattr(w._q, "put_nowait", lambda item: seen.append(item))
    assert w.submit({"symbol": "ABC", "decision": "SKIPPED", "reason": "x"}) is True
    ts = seen[0]["created_at"]
    assert ts.tzinfo is None and abs((datetime.now(timezone.utc).replace(tzinfo=None) - ts).total_seconds()) < 5
