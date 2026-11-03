"""E285 round 154 (plan item 8): Real Trade waits 15 minutes after the open by default; records_report has an entry-minute breakdown."""
from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _report():
    p = os.path.join(ROOT, "..", "..", "scripts", "records_report.py")
    spec = importlib.util.spec_from_file_location("records_report_r154", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_default_wait_is_15_minutes_in_source_and_env_override_is_read():
    src = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert '_og_float("OPENING_GATE_MIN_MINUTES_AFTER_OPEN", 15.0)' in src


def test_entry_minute_buckets_are_ist_quarter_hours():
    m = _report()
    f = m.entry_minute_bucket
    assert f({"opened_at": "2026-11-03T03:46:10+00:00"}) == "09:15"     # 09:16 IST
    assert f({"opened_at": "2026-11-03T04:05:00"}) == "09:30"           # naive = UTC -> 09:35 IST
    assert f({"opened_at": "2026-11-03T04:15:00Z"}) == "09:45"
    assert f({"opened_at": None}) == "unknown" and f({"opened_at": "junk"}) == "unknown"


def test_report_has_the_entry_minute_group():
    m = _report()
    recs = [{"opened_at": "2026-11-03T03:50:00+00:00", "closed_at": "2026-11-03T05:00:00+00:00", "net_pnl": -10.0, "gross_pnl": -8.0, "return_pct": -1.0, "charges": 2.0, "origin": "system"},
            {"opened_at": "2026-11-03T04:20:00+00:00", "closed_at": "2026-11-03T05:00:00+00:00", "net_pnl": 5.0, "gross_pnl": 7.0, "return_pct": 1.0, "charges": 2.0, "origin": "system"}]
    rep = m.build_report(recs, min_n=1)
    g = rep["groups"]["entry minute IST (15-min slots, use --origin system)"]
    assert set(g) == {"09:15", "09:45"}


if __name__ == "__main__":
    f = 0
    for n, fn in sorted(globals().items()):
        if n.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", n)
            except Exception as ex:  # noqa: BLE001
                f += 1; print("FAIL", n, type(ex).__name__, ex)
    sys.exit(1 if f else 0)
