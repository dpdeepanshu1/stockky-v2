"""E115: the Time Machine in LIVE stock picking (timemachine/gate_core.py, orders/tm_gate.py). No network, no database, no broker: the data fetch and the charges are injected."""
import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from timemachine import data as tm_data  # noqa: E402
from timemachine import engine as E  # noqa: E402
from timemachine import gate_core as g  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=IST)        # a Monday, entry bar 10:00


def fake_charges(entry, exit_, qty, short):
    return 20.0 + 0.0002 * (entry + exit_) * qty


def trend_rows(n_days=24, step=0.0006):
    """Every session climbs (or falls, step < 0) steadily: a long always reaches a 2% target; a short always meets its stop first."""
    rows = []
    for d in range(1, n_days + 1):
        px, day = 100.0, f"2026-09-{d:02d}"
        for s in range(75):
            o = px
            c = o * (1 + step)
            h = max(o, c) * 1.0001
            l = min(o, c) * 0.9999
            m = 555 + 5 * s
            rows.append({"date": f"{day} {m // 60:02d}:{m % 60:02d}", "open": o, "high": h, "low": l, "close": c, "volume": 1000})
            px = c
    return rows


RISING, FALLING = trend_rows(step=0.0006), trend_rows(step=-0.0006)


def fetch_of(rows):
    return lambda sym, lb: rows


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    g.reset()
    tm_data.clear_cache()
    g.configure(charges_fn=fake_charges, service="test")
    for k, v in (("TM_GATE_MODE", "live"), ("TM_GATE_BLOCK", "against"), ("TIME_MACHINE_ENABLED", True), ("TM_GATE_INLINE_BUDGET_S", 5.0), ("TM_PICK_NUDGE_PTS", 6.0),
                 ("TM_GATE_FAIL_TTL_S", 120.0), ("TM_AUTO_ENABLED", True)):
        monkeypatch.setattr(config, k, v, raising=False)
    yield
    g.reset()
    tm_data.clear_cache()


def test_long_in_a_rising_window_passes_and_is_stamped():
    ev, reason = g.check("ABC", "LONG", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    assert reason is None and ev["opinion"] == "ok" and ev["v"]["code"] == "FAVOURS"
    import json
    assert json.loads(g.attach('{"x":1}', ev))["tm"]["code"] == "FAVOURS" and json.loads(g.attach('{"x":1}', ev))["x"] == 1


def test_short_in_a_rising_window_is_refused_in_live():
    ev, reason = g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    assert ev["v"]["code"] == "AGAINST" and reason and reason.startswith("TIME_MACHINE:")
    assert g.status()["counters"]["rejects"] == 1


def test_log_mode_records_but_never_blocks(monkeypatch):
    monkeypatch.setattr(config, "TM_GATE_MODE", "log")
    ev, reason = g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    assert ev["would_reject"] is True and reason is None
    assert g.status()["counters"]["would_reject_log"] == 1 and g.status()["counters"]["rejects"] == 0


def test_off_mode_and_master_switch_run_nothing(monkeypatch):
    calls = []
    for k, v in (("TM_GATE_MODE", "off"), ("TIME_MACHINE_ENABLED", False)):
        monkeypatch.setattr(config, "TM_GATE_MODE", "live")
        monkeypatch.setattr(config, "TIME_MACHINE_ENABLED", True)
        monkeypatch.setattr(config, k, v)
        ev, reason = g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=lambda s, l: calls.append(1) or RISING)
        assert reason is None and ev["opinion"] == "no_opinion"
    assert not calls


def test_unknown_mode_value_is_off(monkeypatch):
    monkeypatch.setattr(config, "TM_GATE_MODE", "enforce")
    assert g.mode() == "off"


def test_missing_data_is_no_opinion_and_the_failure_is_remembered():
    n = []

    def boom(sym, lb):
        n.append(1)
        raise tm_data.DataError("DATA_UNREACHABLE", "down")
    for _ in range(3):
        ev, reason = g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=boom)
        assert reason is None and ev["opinion"] == "no_opinion"
    assert len(n) == 1, "a dead market-data must cost one call, not one per candidate"


def test_too_few_sessions_is_no_verdict_so_nothing_is_refused():
    ev, reason = g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(trend_rows(n_days=5)))
    assert reason is None


def test_past_flat_by_and_bad_input_are_no_opinion():
    late = datetime(2026, 10, 26, 15, 10, tzinfo=IST)
    assert g.evaluate("ABC", "LONG", 1.0, 2.0, now=late, fetch=fetch_of(RISING))["opinion"] == "no_opinion"
    for bad in (("ABC", "LONG", 0, 2.0), ("ABC", "SIDEWAYS", 1.0, 2.0), ("!!", "LONG", 1.0, 2.0), (None, None, None, None)):
        assert g.evaluate(*bad, now=MON_10, fetch=fetch_of(RISING))["opinion"] == "no_opinion"


def test_slow_data_waits_only_the_budget_then_no_opinion(monkeypatch):
    monkeypatch.setattr(config, "TM_GATE_INLINE_BUDGET_S", 0.2)

    def slow(sym, lb):
        time.sleep(1.0)
        return RISING
    t = time.monotonic()
    ev = g.evaluate("SLOWCO", "SHORT", 1.0, 2.0, now=MON_10, fetch=slow)
    assert time.monotonic() - t < 0.9 and ev["opinion"] == "no_opinion"
    time.sleep(1.4)                                           # the answer finished in the background: the next attempt is a cache read
    ev2 = g.evaluate("SLOWCO", "SHORT", 1.0, 2.0, now=MON_10, fetch=slow)
    assert ev2["source"] == "cache" and ev2["would_reject"] is True


def test_cache_only_mode_queues_and_the_worker_warms_it(monkeypatch):
    monkeypatch.setattr(config, "TM_GATE_INLINE_BUDGET_S", 0.0)
    ev = g.evaluate("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    assert ev["opinion"] == "no_opinion" and "queued" in ev["no_data"]
    assert g.process_queue_once(fetch=fetch_of(RISING), max_jobs=5) >= 0       # the queued job slot is the CURRENT bar; run it
    assert g.status()["pending_jobs"] == 0


def test_note_then_warm_then_gate_is_a_cache_read():
    slot = g.entry_slot("SHORT", MON_10)
    assert g.note("ABC", "SHORT", 1.0, 2.0, None, 100.0, source="shortlist", enqueue=False) is False
    g._enqueue("ABC", "SHORT", 1.0, 2.0, 20000.0, 100.0, "test", slot)
    assert g.process_queue_once(fetch=fetch_of(RISING)) >= 1
    ev = g.evaluate("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=lambda s, l: (_ for _ in ()).throw(AssertionError("no fetch expected")))
    assert ev["source"] == "cache" and ev["would_reject"] is True


def test_nudge_up_down_and_zero(monkeypatch):
    assert g.nudge("ABC", "LONG") == 0.0                                       # no verdict yet
    g.check("ABC", "LONG", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    g.check("ABC", "SHORT", 1.0, 2.0, 100.0, now=MON_10, fetch=fetch_of(RISING))
    assert g.nudge("ABC", "LONG") == 6.0 and g.nudge("ABC", "SHORT") == -6.0
    monkeypatch.setattr(config, "TM_PICK_NUDGE_PTS", 0.0)
    assert g.nudge("ABC", "LONG") == 0.0
    monkeypatch.setattr(config, "TM_PICK_NUDGE_PTS", 6.0)
    monkeypatch.setattr(config, "TM_GATE_MODE", "off")
    assert g.nudge("ABC", "LONG") == 0.0
    assert g.nudge(object(), object()) == 0.0                                  # never raises


def test_near_breakeven_blocks_only_when_asked(monkeypatch):
    v = {"code": "NEAR_BREAKEVEN", "side": "LONG", "symbol": "X", "entry_time": "10:00", "stop_pct": 1.0, "target_pct": 2.0, "line": "near"}
    assert g._decide(v)[0] is False
    monkeypatch.setattr(config, "TM_GATE_BLOCK", "against_or_near")
    assert g._decide(v)[0] is True
    assert g._decide(dict(v, code="MIXED"))[0] is False and g._decide(dict(v, code="NOT_ENOUGH_DATA"))[0] is False


def test_timer_triggers_prefetch_then_scans_each_boundary_once():
    day = lambda h, m: datetime(2026, 10, 26, h, m, tzinfo=IST)
    assert g.due_trigger(day(8, 59)) is None
    assert g.due_trigger(day(9, 5)) == "prefetch" and g.due_trigger(day(9, 6)) is None
    assert g.due_trigger(day(9, 16)) == "scan:09:16" and g.due_trigger(day(9, 17)) is None
    assert g.due_trigger(day(9, 31)) == "scan:09:31" and g.due_trigger(day(9, 40)) is None
    assert g.due_trigger(day(14, 16)) == "scan:14:16"
    assert g.due_trigger(day(14, 31)) is None, "no scan after TM_AUTO_LAST_IST"
    assert g.due_trigger(day(15, 5)) is None
    assert g.due_trigger(datetime(2026, 10, 24, 9, 16, tzinfo=IST)) is None          # Saturday
    g.reset()
    assert g.due_trigger(day(9, 40)) == "scan:09:31", "after a restart only the CURRENT boundary runs, older ones are not caught up"
    assert g.due_trigger(day(9, 41)) is None


def test_tick_runs_a_scan_for_noted_picks_and_respects_the_switches(monkeypatch):
    g.note("ABC", "LONG", 1.0, 2.0, None, 100.0, source="entry", enqueue=False)
    out = g.tick(MON_10.replace(hour=9, minute=16), fetch=fetch_of(RISING), wait=True)
    assert out and out["reason"].startswith("timer") and out["targets"] == 1 and out["queued"] == 1 and out["ran"] == 1
    g.reset()
    g.note("ABC", "LONG", 1.0, 2.0, None, 100.0, source="entry", enqueue=False)
    monkeypatch.setattr(config, "TM_AUTO_ENABLED", False)
    assert g.tick(MON_10.replace(hour=9, minute=16), fetch=fetch_of(RISING), wait=True) is None


def test_manual_scan_queues_and_wait_computes():
    g.note("ABC", "SHORT", 1.0, 2.0, None, 100.0, source="shortlist", enqueue=False)
    r = g.scan("runner test", MON_10, wait=True, fetch=fetch_of(RISING))
    assert r["ok"] and r["ran"] == 1 and g.status()["counters"]["manual_scans"] == 1
    assert g.peek("ABC", "SHORT")["code"] == "AGAINST"


def test_scan_when_off_says_so(monkeypatch):
    monkeypatch.setattr(config, "TM_GATE_MODE", "off")
    r = g.scan("manual")
    assert r["ok"] is False and r["queued"] == 0


def test_threads_start_once_and_stop():
    assert g.start() is True and g.start() is False
    assert g.status()["timer"]["threads_running"] is True
    g.stop()
    time.sleep(0.1)
    g._threads["stop"].clear()
    for t in (g._threads["worker"], g._threads["ticker"]):
        t.join(timeout=3)


def test_status_shape():
    s = g.status()
    for k in ("mode", "timer", "settings", "counters", "refusal_rate", "recent_checks", "recent_scans", "active", "cached_verdicts"):
        assert k in s


def test_the_vendored_files_are_byte_identical_in_both_services():
    for name in ("gate_core.py", "engine.py", "data.py", "service.py", "__init__.py"):
        a = (REPO / "services/position-stocks-service/timemachine" / name).read_bytes()
        b = (REPO / "services/real-trade-service/timemachine" / name).read_bytes()
        assert a == b, f"{name} differs between the two services"


def test_the_gate_module_places_nothing():
    src = (ROOT / "timemachine" / "gate_core.py").read_text()
    for banned in ("dhan_client", "place_order", "sqlalchemy", "db.add", "db.commit", "Session"):
        assert banned not in src, banned


# ------------------------------------------------------------------ wiring guards (source level)

def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8")


def test_position_stocks_wiring_points_exist():
    e = _read("services/position-stocks-service/orders/entry.py")
    assert "tm_gate.entry_check(" in e and e.index("tm_gate.entry_check(") < e.index("_circuit_reject = circuit_gate.reject_reason") and '"TIME_MACHINE"' in e
    s = _read("services/position-stocks-service/orders/short_entry.py")
    assert "tm_gate.check_ev(" in s and "SHORT_TIME_MACHINE" in s and "tm_gate.note_picks(" in s
    assert "tm_gate.stamp(" in _read("services/position-stocks-service/orders/short_live.py")
    sl = _read("services/position-stocks-service/screening/shortlist.py")
    assert sl.count("_tm_nudge(") >= 3
    m = _read("services/position-stocks-service/main.py")
    assert "_tm_gate_boot.start()" in m and '"/time-machine/gate"' in m and '"/time-machine/scan"' in m and "_tm_gate.note_picks(quality_passers" in m


def test_real_trade_wiring_points_exist():
    e = _read("services/real-trade-service/entry_engine/entry.py")
    assert "Gate 4h (E115)" in e and e.index("Gate 4h (E115)") < e.index("Gate 5: reward:risk floor") and "tm_gate.evaluate" in e and "tm_gate.rank_points(_tm_ev)" in e
    assert "tm_gate.note_shortlist(" in e
    m = _read("services/real-trade-service/main.py")
    assert "_tmg.start()" in m and '"/time-machine/gate"' in m and '"/time-machine/scan"' in m


def test_switches_are_on_both_calibration_pages_and_protected():
    import re
    runner = _read("services/calibration-runner-service/autocalib.py")
    keys = ("TM_GATE_MODE", "TM_GATE_BLOCK", "TM_GATE_SIMS", "TM_GATE_INLINE_BUDGET_S", "TM_GATE_TTL_S", "TM_PICK_NUDGE_PTS", "TM_AUTO_ENABLED", "TM_AUTO_INTERVAL_MIN")
    for svc in ("position-stocks-service", "real-trade-service"):
        cat = _read(f"services/{svc}/control/catalog.py")
        for k in keys:
            assert f'Flag("{k}"' in cat, (svc, k)
    for k in keys:
        assert re.search(rf'"{k}":\s*"', runner), k


def test_runner_trigger_job_and_schedule():
    jobs = _read("services/calibration-runner-service/jobs.py")
    assert '_job("tm_gate_check"' in jobs and "s_tm_open" in jobs and '"time_machine_open"' in jobs
    assert "after_lock" in _read("services/calibration-runner-service/weather.py") and "time_machine_scan" in _read("services/calibration-runner-service/main.py")
