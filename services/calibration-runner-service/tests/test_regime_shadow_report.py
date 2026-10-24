"""Part 1 step 6 prerequisites: regime_shadow_report.py (offline, plain asserts) and the protected-list guard for the structure / Market Weather keys."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import autocalib as A  # noqa: E402
import jobs as J  # noqa: E402
import regime_shadow_report as R  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _log(days, state):
    rows = []
    for d in days:
        for stage, hh, mm in (("morning", 8, 0), ("lock", 9, 14)):
            rows.append({"at": datetime.fromisoformat(f"{d}T{hh:02d}:{mm:02d}:00+05:30").isoformat(), "day": d, "stage": stage, "ok": True, "state": state})
    return rows


def _rec(day, hhmm, pnl, i):
    t = datetime.fromisoformat(f"{day}T{hhmm}:00+05:30").astimezone(timezone.utc).isoformat()
    return {"position_id": i, "symbol": f"S{i}", "opened_at": t, "net_pnl": pnl}


def _days(n):
    out, d = [], datetime(2026, 9, 1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def _run(log, recs):
    d = tempfile.mkdtemp()
    lp, rp = Path(d, "log.jsonl"), Path(d, "rec.json")
    lp.write_text("\n".join(json.dumps(r) for r in log) + "\n")
    rp.write_text(json.dumps({"records": recs}))
    return R.replay(R.load_log(str(lp)), R.load_trades([str(rp)]))


def test_late_losers_skipped_on_stressed_days_pass():
    days = _days(25)
    recs, i = [], 0
    for d in days:                      # stressed: ENTRY_LAST 13:00, earliest 09:45. Late (13:30) trades lose, mid-day trades win
        recs += [_rec(d, "11:00", 300.0, i), _rec(d, "13:30", -400.0, i + 1)]
        i += 2
    r = _run(_log(days, "stressed"), recs)
    assert r["verdict"] == "PASS", r
    assert r["tight_skipped"] == 25 and r["skipped_pnl"] == -10000.0
    assert r["skip_excess"] > 0 and r["change"] > 0


def test_skipping_ordinary_trades_is_not_an_edge():
    """The base strategy loses on average. A rule that skips trades at random 'improves' the total but must NOT pass: the skipped ones are average, not worse."""
    days = _days(25)
    recs, i = [], 0
    for d in days:                      # every trade is -100 whenever it happens, so skipping late ones removes only average trades
        recs += [_rec(d, "11:00", -100.0, i), _rec(d, "13:30", -100.0, i + 1)]
        i += 2
    r = _run(_log(days, "stressed"), recs)
    assert r["change"] > 0 and r["skip_excess"] == 0
    assert r["verdict"] == "FAIL"


def test_too_little_data_is_not_enough_data():
    r = _run(_log(_days(5), "stressed"), [_rec(d, "13:30", -50.0, i) for i, d in enumerate(_days(5))])
    assert r["verdict"] == "NOT_ENOUGH_DATA"


def test_entry_cap_and_risk_multiplier_and_state_lookup():
    d = _days(1)[0]
    log = _log([d], "panic")                                   # panic: earliest 10:15, last 12:00, 2 entries, risk 0.4
    recs = [_rec(d, "09:30", -10.0, 1),                        # before 10:15 -> skipped
            _rec(d, "10:20", 100.0, 2), _rec(d, "10:40", 50.0, 3), _rec(d, "11:00", 25.0, 4)]   # 3rd kept entry -> over the cap of 2
    r = _run(log, recs)
    b = r["by_state"]["panic"]
    assert b["trades"] == 4 and b["skipped"] == 2 and b["kept"] == 2
    assert b["profile"] == round((100.0 + 50.0) * 0.4, 2)


def test_state_is_the_same_days_lock_or_later_never_yesterdays_or_before_the_lock():
    d1, d2 = _days(2)
    log = _log([d1], "panic")
    tr = R.load_trades([_write([_rec(d2, "10:00", 5.0, 1), _rec(d1, "09:00", 5.0, 2), _rec(d1, "10:00", 5.0, 3)])])
    lg = R.load_log(_write_log(log))
    by = {(t["day"], t["min"]): t for t in tr}
    assert R.state_at(lg, by[(d1, 10 * 60)]["at"]) == "panic"
    assert R.state_at(lg, by[(d2, 10 * 60)]["at"]) is None      # d2 has no row: yesterday's state is never reused
    assert R.state_at(lg, by[(d1, 9 * 60)]["at"]) is None       # 09:00 is before the 09:14 lock


def _write(recs):
    p = Path(tempfile.mkdtemp(), "r.json")
    p.write_text(json.dumps({"records": recs}))
    return str(p)


def _write_log(rows):
    p = Path(tempfile.mkdtemp(), "l.jsonl")
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def test_script_runs_and_exit_code_follows_the_verdict():
    days = _days(25)
    recs = []
    for i, d in enumerate(days):
        recs += [_rec(d, "11:00", 300.0, 2 * i), _rec(d, "13:30", -400.0, 2 * i + 1)]
    out = tempfile.mkdtemp()
    assert R.main(["--log", _write_log(_log(days, "stressed")), _write(recs), "--out", out]) == 0
    assert json.loads(Path(out, "regime_shadow.json").read_text())["verdict"] == "PASS"
    assert R.main(["--log", "/nonexistent", _write(recs)]) == 1


def test_job_is_registered_and_points_at_the_log_and_records():
    j = J.JOBS["regime_shadow_report"]
    steps = j["builder"]({"python": sys.executable, "scripts": str(REPO / "scripts"), "exp": "/data/runner/exports", "run": "/tmp/run"}, {})
    argv = steps[0]["argv"]
    assert argv[1].endswith("regime_shadow_report.py") and "/data/runner/weather/log.jsonl" in argv and "/data/runner/exports/rt_latest.json" in argv
    assert j["needs"] == ["export_records"]


def test_every_structure_and_market_state_key_is_either_a_rule_or_protected():
    """Plan step 6: the calibrator may never touch a gate. A new STRUCTURE_* switch must be a deliberate rule or protected, never silently loose."""
    keys = set()
    for svc in ("real-trade-service", "position-stocks-service"):
        src = (REPO / "services" / svc / "control" / "catalog.py").read_text(encoding="utf-8")
        keys |= set(re.findall(r'Flag\("((?:PS_)?STRUCTURE_[A-Z0-9_]+|MARKET_STATE_[A-Z_]+|SHORT_[A-Z_]+)"', src))
    ruled = {r.key for r in A.RULES}
    assert keys, "no keys found: the catalog format changed"
    loose = sorted(k for k in keys if k not in ruled and k not in A.PROTECTED)
    assert not loose, f"structure / market-state switches neither calibrated nor protected: {loose}"
    for k in ("MARKET_STATE_MODE", "MARKET_STATE_RISK_MULT", "MARKET_STATE_CONVICTION_ADD"):
        assert k in A.PROTECTED and k not in ruled
