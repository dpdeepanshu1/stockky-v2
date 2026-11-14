"""Round 512: market research (review of the five regime constants), its review-date stamp, the weekly loop rules, the boot-time stale check that
waits for regime_adaptive's first snapshot, and the guard-rail copy kept equal to the scheduler's."""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_research as mr          # noqa: E402
import regime_adaptive as ra          # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _closes(n=300, drift=0.0004):
    """A calm, slowly rising index: enough sessions for market_picture()."""
    out, v = [], 20000.0
    for i in range(n):
        v *= 1.0 + drift + (0.002 if i % 2 else -0.002)
        out.append(v)
    return out


def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_RESEARCH_STATE_PATH", str(tmp_path / "market_research.json"))
    monkeypatch.setenv("MARKET_RESEARCH_NOTIFY", "false")
    mr._reset_cache()
    return tmp_path / "market_research.json"


def test_guardrails_equal_the_schedulers_copy():
    src = open(os.path.join(SERVICES, "notification-scheduler-service", "scheduler", "governance_check.py"), encoding="utf-8").read()
    for name in mr.CONSTANTS:
        m = re.search(r'"%s":\s*\("[^"]+",\s*"[^"]+",\s*([-\d.]+),\s*([-\d.]+)\)' % name, src)
        assert m, name
        assert (float(m.group(1)), float(m.group(2))) == tuple(float(x) for x in mr.GUARDRAILS[name]), name


def test_build_report_ok_and_attention():
    snap = {"values": {"score_delta": 2, "ENTRY_MIN_REWARD_RISK": 2.1, "CANDIDATE_MIN_CONVICTION": 57.0, "CANDIDATE_DOWNTREND_6M_PCT": -11.0, "CANDIDATE_MIN_BULLISH_TF": 4},
            "base": {"ENTRY_MIN_REWARD_RISK": 2.0, "CANDIDATE_MIN_CONVICTION": 55.0, "CANDIDATE_DOWNTREND_6M_PCT": -10.0, "CANDIDATE_MIN_BULLISH_TF": 4},
            "picture": {"tone": 0.1, "stress": 0.1, "returns": {"1m": 1.0}, "sessions": 300}, "feedback": {"trades": 0}}
    rep = mr.build_report(snap, True, "manual", now=datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc))
    assert rep["ok"] and rep["verdict"] == "OK" and rep["reviewed_date"] == "2026-11-14" and len(rep["constants"]) == 5
    row = {r["name"]: r for r in rep["constants"]}
    assert row["ENTRY_REGIME_MIN_SCORE"]["suggested"] == 27 and row["ENTRY_REGIME_MIN_SCORE"]["changed"]
    assert row["CANDIDATE_MIN_BULLISH_TF"]["changed"] is False and all(r["in_band"] for r in rep["constants"])
    # a stressed market, or adaptive OFF, is ATTENTION with a reason, never an error
    snap["picture"]["stress"] = 0.8
    assert mr.build_report(snap, True, "manual")["verdict"] == "ATTENTION"
    off = mr.build_report(snap, False, "manual")
    assert off["verdict"] == "ATTENTION" and any("REGIME_ADAPTIVE_MODE=off" in x for x in off["reasons"])
    snap["values"]["ENTRY_MIN_REWARD_RISK"] = 5.0
    assert "ENTRY_MIN_REWARD_RISK" in " ".join(mr.build_report(snap, True, "manual")["reasons"])


def test_run_research_stamps_the_review_date_and_clears_the_clock(tmp_path, monkeypatch):
    path = _state(tmp_path, monkeypatch)
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")      # computed directly from the closes
    assert mr.last_reviewed("ENTRY_MIN_REWARD_RISK", "2026-10-04") == "2026-10-04"
    rep = mr.run_research(None, "manual", fetch=lambda: _closes())
    assert rep["ok"] and rep["adaptive_live"] is False and rep["state_saved"] is True
    doc = json.loads(path.read_text())
    assert doc["reviewed_date"] == rep["reviewed_date"] and doc["last_ok"]["at"] == rep["at"]
    assert mr.last_reviewed("ENTRY_MIN_REWARD_RISK", "2026-10-04") == rep["reviewed_date"]
    assert mr.last_reviewed("SOME_OTHER_CONSTANT", "2026-10-04") == "2026-10-04"      # only the five regime constants move
    assert mr.last_reviewed("ENTRY_MIN_REWARD_RISK", "2999-01-01") == "2999-01-01"    # never moves a date backwards


def test_failed_research_stamps_nothing(tmp_path, monkeypatch):
    path = _state(tmp_path, monkeypatch)
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")

    def boom():
        raise RuntimeError("index history HTTP 502")
    rep = mr.run_research(None, "manual", fetch=boom)
    assert rep["ok"] is False and "502" in rep["error"]
    doc = json.loads(path.read_text())
    assert "reviewed_date" not in doc and doc["last"]["ok"] is False and "last_ok" not in doc
    assert mr.last_reviewed("ENTRY_MIN_REWARD_RISK", "2026-10-04") == "2026-10-04"
    short = mr.run_research(None, "manual", fetch=lambda: _closes(30))
    assert short["ok"] is False and "not enough index history" in short["error"]


def test_scheduled_triggers_skip_a_recent_good_run_but_the_button_never_does(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")
    first = mr.run_research(None, "weekly", fetch=lambda: _closes())
    assert first["ok"] and not first.get("skipped")
    again = mr.run_research(None, "runner", fetch=lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert again["ok"] and again["skipped"] is True
    assert mr.run_research(None, "manual", fetch=lambda: _closes())["ok"]


def test_live_mode_uses_the_adaptive_snapshot(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "live")
    ra._reset_for_tests()
    rep = mr.run_research(None, "manual", fetch=lambda: _closes())
    assert rep["ok"] and rep["adaptive_live"] is True
    assert ra._fresh_snapshot() is not None      # research also freshens the snapshot the trading rules read
    ra._reset_for_tests()


def test_telegram_message_is_sent_once_and_a_telegram_failure_does_not_fail_research(tmp_path, monkeypatch):
    _state(tmp_path, monkeypatch)
    monkeypatch.setenv("MARKET_RESEARCH_NOTIFY", "true")
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")
    fake = MagicMock()
    with patch.dict(sys.modules, {"notifier": fake}):
        rep = mr.run_research(None, "manual", fetch=lambda: _closes())
    fake.notify_sync.assert_called_once()
    text = fake.notify_sync.call_args[0][0]
    assert "Weekly market research report" in text and "ENTRY_REGIME_MIN_SCORE" in text and rep["reviewed_date"] in text
    bad = MagicMock()
    bad.notify_sync.side_effect = RuntimeError("telegram down")
    with patch.dict(sys.modules, {"notifier": bad}):
        assert mr.run_research(None, "manual", fetch=lambda: _closes())["ok"]


def test_a_busy_run_is_reported_not_queued():
    assert mr._run_lock.acquire(blocking=False)
    try:
        r = mr.run_research(None, "manual")
        assert r["ok"] is False and r["busy"] is True
    finally:
        mr._run_lock.release()


def _doc(age_days, now):
    at = (now - timedelta(days=age_days)).isoformat(timespec="seconds")
    return {"last_ok": {"ok": True, "at": at}, "last": {"ok": True, "at": at}}


def test_weekly_due_rules():
    sat_10 = datetime(2026, 11, 14, 10, 0, tzinfo=IST)         # Saturday after 09:00 IST
    sat_08 = datetime(2026, 11, 14, 8, 0, tzinfo=IST)
    tue = datetime(2026, 11, 17, 10, 0, tzinfo=IST)
    assert mr.weekly_due({}, sat_10) is True                       # never run: catch up
    assert mr.weekly_due(_doc(7, sat_10), sat_10) is True          # Saturday, a week old
    assert mr.weekly_due(_doc(7, sat_08), sat_08) is False         # Saturday but before 09:00 IST
    assert mr.weekly_due(_doc(2, sat_10), sat_10) is False         # a recent run (the runner already did it)
    assert mr.weekly_due(_doc(6, tue), tue) is False               # a weekday inside 8 days waits for Saturday
    assert mr.weekly_due(_doc(9, tue), tue) is True                # catch-up after a missed Saturday
    failed = {"last": {"ok": False, "at": (tue - timedelta(hours=2)).isoformat(timespec="seconds")}}
    assert mr.weekly_due(failed, tue) is False                     # one failure message, not one per hour
    failed["last"]["at"] = (tue - timedelta(hours=7)).isoformat(timespec="seconds")
    assert mr.weekly_due(failed, tue) is True


def test_next_weekly_is_a_saturday_0900():
    n = datetime(2026, 11, 14, 10, 0, tzinfo=IST)
    assert mr.next_weekly_ist(None, n) == "2026-11-21 09:00"
    assert mr.next_weekly_ist(None, datetime(2026, 11, 14, 8, 0, tzinfo=IST)) == "2026-11-14 09:00"


def test_weekly_switch(monkeypatch):
    monkeypatch.delenv("MARKET_RESEARCH_WEEKLY", raising=False)
    assert mr._weekly_on() is True
    monkeypatch.setenv("MARKET_RESEARCH_WEEKLY", "off")
    assert mr._weekly_on() is False
