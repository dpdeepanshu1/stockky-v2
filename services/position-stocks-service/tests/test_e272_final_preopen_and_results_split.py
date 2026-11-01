"""E272: final pre-open read (09:12) and the results-day split in the quality gate.
Plain-function runner: python3 tests/test_e272_final_preopen_and_results_split.py (also collected by pytest). sqlalchemy / httpx are stubbed when missing."""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _m in ("httpx", "sqlalchemy", "sqlalchemy.orm"):
    if _m not in sys.modules:
        try:
            __import__(_m)
        except Exception:  # noqa: BLE001
            sys.modules[_m] = types.ModuleType(_m)
if not hasattr(sys.modules["sqlalchemy.orm"], "Session"):
    sys.modules["sqlalchemy.orm"].Session = object

if "models" not in sys.modules:
    _mm = types.ModuleType("models")
    _mm.ScalpQualityCache = type("ScalpQualityCache", (), {})
    sys.modules["models"] = _mm

import config
from screening import preopen, quality_gate as qg
from tz_utils import IST


def ist(h, m, s=0, day=(2026, 11, 2)):          # Monday 2 Nov 2026
    return datetime(*day, h, m, s, tzinfo=IST)


def _row(sym, price, prev, age=10):
    return {"symbol": sym, "price": price, "previous_close": prev, "age_s": age}


# ── final pre-open read ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_final_read_window():
    preopen.reset()
    config.PREOPEN_SNAPSHOT_ENABLED = True       # E284: conftest switches it off for older tests and restores it after each test
    assert not preopen.due_final(ist(9, 11, 59))
    assert preopen.due_final(ist(9, 12))
    assert preopen.due_final(ist(9, 14, 59))
    assert not preopen.due_final(ist(9, 15))                      # the bell
    assert not preopen.due_final(ist(9, 12, day=(2026, 11, 1)))   # Sunday


def test_final_read_runs_once_per_day_and_replaces_the_indicative_gap():
    preopen.reset()
    n = ist(9, 10)
    preopen.record([_row("AAA", 102.0, 100.0), _row("BBB", 105.0, 100.0)], now=n)
    assert preopen._snap["gaps"] == {"AAA": 2.0, "BBB": 5.0}
    changed = preopen.record_final([_row("AAA", 103.0, 100.0), _row("CCC", 99.0, 100.0), _row("BBB", 90.0, 100.0, age=9999)], now=ist(9, 12, 30))
    assert changed == 2
    g = preopen._snap["gaps"]
    assert g["AAA"] == 3.0 and g["CCC"] == -1.0                    # replaced / added
    assert g["BBB"] == 5.0                                         # stale final row: the 09:10 gap stays
    assert not preopen.due_final(ist(9, 13))                       # done for today
    assert preopen.status()["final_updated"] == 2


def test_final_read_switch_off():
    preopen.reset()
    config.PREOPEN_FINAL_READ_ENABLED = False
    try:
        assert not preopen.due_final(ist(9, 12))
    finally:
        config.PREOPEN_FINAL_READ_ENABLED = True


def test_final_read_with_no_rows_still_marks_the_day():
    preopen.reset()
    assert preopen.record_final([], now=ist(9, 12)) == 0
    assert not preopen.due_final(ist(9, 13))


# ── results-day split ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _utc(t):
    return t.astimezone(timezone.utc).isoformat()


def test_unknown_publication_time_stays_blocked():
    assert "unknown" in qg.earnings_split_reason(None, now=ist(10, 0))
    assert "unknown" in qg.earnings_split_reason("garbage", now=ist(10, 0))


def test_published_before_0900_is_a_known_event():
    assert qg.earnings_split_reason(_utc(ist(8, 20)), now=ist(9, 30)) is None
    assert qg.earnings_split_reason(_utc(ist(8, 59)), now=ist(9, 21)) is None


def test_published_in_session_waits_five_minutes():
    out = _utc(ist(11, 0))
    assert "waiting" in qg.earnings_split_reason(out, now=ist(11, 0, 30))
    assert "waiting" in qg.earnings_split_reason(out, now=ist(11, 4, 59))
    assert qg.earnings_split_reason(out, now=ist(11, 5, 1)) is None


def test_publication_from_another_day_does_not_count():
    assert "not from today" in qg.earnings_split_reason(_utc(ist(8, 0, day=(2026, 10, 30))), now=ist(10, 0))


def test_switch_off_blocks_every_results_day():
    config.EARNINGS_SPLIT_ENABLED = False
    try:
        assert qg.earnings_split_reason(_utc(ist(8, 0)), now=ist(10, 0)) is not None
    finally:
        config.EARNINGS_SPLIT_ENABLED = True


def test_passes_uses_the_split_only_for_results_dated_today():
    today = qg.ist_today_str()
    # results dated today, no time known -> EARNINGS_TODAY as before
    ok, why = qg.QualitySignal(symbol="X", earnings_date=today).passes()
    assert not ok and why.startswith("EARNINGS_TODAY")
    # results dated today, published long before the open -> passes this check
    early = (datetime.now(IST).replace(hour=8, minute=0, second=0, microsecond=0)).astimezone(timezone.utc).isoformat()
    ok2, why2 = qg.QualitySignal(symbol="X", earnings_date=today, result_out_at=early).passes()
    assert ok2, why2
    # results dated another day: never blocked by this rule
    assert qg.QualitySignal(symbol="X", earnings_date="2026-01-01").passes()[0]


def test_result_out_from_reads_only_results_events():
    body = {"events": [{"type": "order", "published": "2026-11-02T03:00:00+00:00"},
                       {"type": "results", "published": "2026-11-02T04:10:00+00:00"},
                       {"type": "results", "published": "2026-11-02T02:40:00+00:00"}]}
    assert qg._result_out_from(body) == "2026-11-02T02:40:00+00:00"
    assert qg._result_out_from({"events": [{"type": "order", "published": "2026-11-02T03:00:00+00:00"}]}) is None
    assert qg._result_out_from(None) is None and qg._result_out_from({"events": "x"}) is None


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
