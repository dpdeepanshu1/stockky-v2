"""E126: every Monday test that can run unattended has a timetable slot, a pack and a Telegram message."""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import jobs as J  # noqa: E402
import scheduler as S  # noqa: E402
import notify as N  # noqa: E402

IST = S.IST
CTX = {"python": "py", "scripts": "/s", "rt": "http://rt", "ps": "http://ps", "md": "http://md", "gw": "g", "analysis": "a", "decision": "d", "notify": "n",
       "kronos": "k", "tsfm": "t", "run": "/r", "exp": "/e", "cal": "/c"}


def slot(i):
    return next(e for e in J.SCHEDULE_DEFAULT if e["id"] == i)


def test_every_default_slot_points_at_a_real_pack_and_is_on():
    for e in J.SCHEDULE_DEFAULT:
        assert e["pack"] in J.PACK_MAP
    on = {e["id"] for e in J.SCHEDULE_DEFAULT if e["enabled"]}
    assert {"s_monday_open", "s_morning_open", "s_tm_open", "s_live_open", "s_live_1015", "s_shorts_flat", "s_overlay_clear", "s_daily_close", "s_friday_review"} <= on


def test_times_and_days_of_the_new_slots():
    assert (slot("s_morning_open")["time"], slot("s_morning_open")["days"]) == ("08:50", [1, 2, 3, 4])      # Monday has s_monday_open at 08:50
    assert slot("s_live_open")["time"] == "09:35" and slot("s_live_open")["days"] == [0, 1, 2, 3, 4]
    assert slot("s_live_1015")["time"] == "10:20"
    assert slot("s_shorts_flat")["time"] == "15:05"      # after the 14:55 cover
    assert slot("s_overlay_clear")["time"] == "15:35"    # after the 15:30 end of the overlay
    for d in (0, 1, 2, 3, 4):
        assert not (d == 0 and slot("s_morning_open")["days"].count(0)), "Monday would get two 08:50 packs"


def test_the_monday_pack_no_longer_runs_the_09_30_check_at_08_50():
    assert "monday_check_0930" not in [j for j, _ in J.PACK_MAP["monday_open"]["jobs"]]
    assert ["monday_check_0930", {}] in J.PACK_MAP["live_check_open"]["jobs"] and ["shadow_shorts_check", {}] in J.PACK_MAP["live_check_open"]["jobs"]
    assert ["monday_check_1015", {}] in J.PACK_MAP["live_check_1015"]["jobs"]
    assert ["wiring_check", {}] in J.PACK_MAP["morning_open"]["jobs"]
    assert ["short_shadow_report", {}] in J.PACK_MAP["friday_review"]["jobs"]


def test_no_pack_places_a_real_order():
    for pk in J.PACKS:
        assert all(j != "dhan_sell_probe" for j, _ in pk["jobs"])


def test_builders_pass_phase_urls_and_token_placeholder():
    for jid, phase in (("shadow_shorts_check", "shadow-shorts"), ("shorts_flat_check", "shorts-flat"), ("overlay_clear_check", "overlay-clear")):
        (st,) = J.JOBS[jid]["builder"](CTX, {})
        assert st["argv"][1].endswith("intraday_state_check.py") and st["argv"][2:4] == ["--phase", phase]
        assert "--md-url" in st["argv"] and st["env"] == {"TOKEN": "{token}"} and st["attention"] == [1] and st["ok"] == [0]
    assert J.JOBS["shadow_shorts_check"]["builder"](CTX, {"expect_short_mode": "live"})[0]["argv"][-2:] == ["--expect-short-mode", "live"]


def test_scheduler_fires_each_new_slot_at_its_time_and_not_on_the_wrong_day():
    got = []
    d = tempfile.mkdtemp()
    clock = {"t": datetime(2026, 10, 27, 8, 51, tzinfo=IST)}       # Tuesday
    s = S.Scheduler(os.path.join(d, "schedule.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a[1]), list(J.PACK_MAP), True, now_fn=lambda: clock["t"])
    seen = []
    for hh, mm in ((8, 51), (9, 21), (9, 36), (10, 21), (15, 6), (15, 36), (16, 6)):
        clock["t"] = datetime(2026, 10, 27, hh, mm, tzinfo=IST)
        seen += s.tick()
    assert seen == ["s_morning_open", "s_tm_open", "s_live_open", "s_live_1015", "s_shorts_flat", "s_overlay_clear", "s_daily_close"]
    assert got[0] == "morning_open" and got[-1] == "daily_close"
    got.clear()
    mon = S.Scheduler(os.path.join(d, "m.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a[1]), list(J.PACK_MAP), True, now_fn=lambda: datetime(2026, 10, 26, 8, 51, tzinfo=IST))
    assert mon.tick() == ["s_monday_open"] and got == ["monday_open"]
    sat = S.Scheduler(os.path.join(d, "s.json"), J.SCHEDULE_DEFAULT, lambda *a, **k: got.append(a[1]), list(J.PACK_MAP), True, now_fn=lambda: datetime(2026, 10, 24, 15, 6, tzinfo=IST))
    assert sat.tick() == []


def test_an_older_schedule_file_gets_the_new_slots_once():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "schedule.json")
    old = [e for e in J.SCHEDULE_DEFAULT if e["id"] in ("s_monday_open", "s_tm_open", "s_daily_close", "s_friday_review", "s_weekly_cal", "s_monthly")]
    import json
    json.dump({"enabled": True, "entries": old, "last_fired": {}, "seen_defaults": [e["id"] for e in old]}, open(p, "w"))
    s = S.Scheduler(p, J.SCHEDULE_DEFAULT, lambda *a, **k: None, list(J.PACK_MAP))
    ids = [e["id"] for e in s.get()["entries"]]
    assert {"s_morning_open", "s_live_open", "s_live_1015", "s_shorts_flat", "s_overlay_clear"} <= set(ids) and len(ids) == len(set(ids))
    assert len(S.Scheduler(p, J.SCHEDULE_DEFAULT, lambda *a, **k: None, list(J.PACK_MAP)).get()["entries"]) == len(ids)


def test_runner_default_is_on_and_compose_matches():
    import config
    assert config.SCHEDULER_ENABLED is True or os.environ.get("RUNNER_SCHEDULER_ENABLED")
    compose = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "docker-compose.yml"), encoding="utf-8").read()
    assert "RUNNER_SCHEDULER_ENABLED: ${RUNNER_SCHEDULER_ENABLED:-true}" in compose


def test_a_scheduled_pass_and_a_scheduled_fail_both_reach_telegram_with_the_verdict_line():
    sent = []
    notify = N.make_notifier("http://n", True, False, sender=lambda b, t, m, u: sent.append((t, m, u)) or "sent")
    base = {"kind": "pack", "target": "shorts_flat_close", "title": "Shorts flat check (T7)", "trigger": "schedule", "by": "scheduler", "started_at": 1.0, "finished_at": 5.0}
    assert notify({**base, "status": "success", "headline": ["SHORTS FLAT (T7): PASS"], "jobs": []}, "/nope") == "sent"
    assert notify({**base, "status": "attention", "headline": ["SHORTS FLAT (T7): FAIL - 1 short"], "jobs": []}, "/nope") == "sent"
    assert "SHORTS FLAT (T7): PASS" in sent[0][1] and sent[0][2] == "normal"
    assert "FAIL" in sent[1][1] and sent[1][2] == "high"
