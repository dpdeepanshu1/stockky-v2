"""Part 1 step 4: Market Weather jobs in shadow (weather.py). Offline: market-data is a fake, nothing touches a network. No pytest fixtures."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import weather as W  # noqa: E402
from regime import profiles  # noqa: E402

IST = W.IST
TODAY = "2026-10-23"                                   # a Friday


def _at(hh, mm, day=TODAY):
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _history(last_day="2026-10-22", n=300, rng=0.9, step=0.0, vix=13.0, vix_last=None):
    """(nifty_doc, vix_doc) ending on last_day (a weekday)."""
    days, d = [], datetime.fromisoformat(last_day)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.date().isoformat())
        d -= timedelta(days=1)
    days.reverse()
    nb, vb, c = [], [], 18000.0
    for i, day in enumerate(days):
        o, cl = c, c * (1 + step / 100)
        nb.append({"date": day, "open": o, "high": max(o, cl) * (1 + rng / 200), "low": min(o, cl) * (1 - rng / 200), "close": cl, "volume": 1})
        v = vix + (i % 3) * 0.3
        if vix_last is not None and i == len(days) - 1:
            v = vix_last
        vb.append({"date": day, "open": v, "high": v, "low": v, "close": v})
        c = cl
    return {"candles": nb}, {"candles": vb}


class FakeMD:
    def __init__(self, nifty, vix, price=None, vix_price=None, day_high=None, day_low=None, stale=False):
        self.nifty, self.vix = nifty, vix
        self.last = nifty["candles"][-1]["close"]
        self.price, self.day_high, self.day_low, self.stale = price or self.last, day_high, day_low, stale
        self.vix_price = vix_price
        self.calls = []
        self.fail = False

    def __call__(self, url):
        self.calls.append(url)
        if self.fail:
            raise OSError("market-data down")
        if "/history/^NSEI" in url:
            return self.nifty
        if "/history/^INDIAVIX" in url:
            return self.vix
        if "/quote/^NSEI" in url:
            pc = self.nifty["candles"][-2]["close"] if self.stale else self.last
            return {"symbol": "^NSEI", "price": self.price, "previous_close": pc, "day_high": self.day_high or self.price, "day_low": self.day_low or self.price}
        if "/quote/INDIAVIX" in url:
            v = self.vix_price or self.vix["candles"][-1]["close"]
            return {"symbol": "INDIAVIX", "price": v, "previous_close": self.vix["candles"][-1]["close"]}
        raise AssertionError(url)


def _engine(md, **kw):
    t = tempfile.mkdtemp()
    sent, stamps = [], []
    kw.setdefault("enabled", True)
    w = W.Weather(t, "http://md", fetch=md, notify=lambda a, b, c: sent.append((a, b, c)), stamp=lambda s, b: stamps.append((s, b)), **kw)
    return w, sent, stamps, t


# ------------------------------------------------------------------------------------------------------ timing
def test_due_stages_times_weekdays_and_no_catch_up():
    assert W.due_stages(_at(8, 0), {}) == ["morning"]
    assert W.due_stages(_at(8, 9), {}) == ["morning"]
    assert W.due_stages(_at(8, 10), {}) == []                                    # missed by 10 min: skipped, not caught up
    assert W.due_stages(_at(8, 50), {"morning": TODAY}) == ["refine"]
    assert W.due_stages(_at(9, 14), {}) == ["lock"] and W.due_stages(_at(9, 18), {}) == ["lock"] and W.due_stages(_at(9, 19), {}) == []
    assert W.due_stages(_at(8, 0), {"morning": TODAY}) == []                     # already fired today
    assert W.due_stages(_at(9, 20), {}) == ["tick"] and W.due_stages(_at(9, 25), {"tick": TODAY + " 09:20"}) == ["tick"]
    assert W.due_stages(_at(9, 20), {"tick": TODAY + " 09:20"}) == []
    assert W.due_stages(_at(9, 22), {}) == []                                    # not a 5-minute mark
    assert W.due_stages(_at(9, 15), {"lock": TODAY}) == []                       # first tick is 09:20
    assert W.due_stages(_at(15, 25), {}) == ["tick"] and W.due_stages(_at(15, 30), {}) == []
    assert W.due_stages(_at(8, 0, "2026-10-24"), {}) == []                       # Saturday
    assert W.due_stages(_at(9, 14, "2026-10-25"), {}) == []                      # Sunday
    assert W.due_stages(datetime(2026, 10, 23, 2, 30, tzinfo=timezone.utc), {}) == ["morning"]   # 08:00 IST given in UTC


def test_quote_is_today_rejects_a_holiday_or_frozen_feed():
    assert W.quote_is_today({"price": 100, "previous_close": 99.99}, 100.0)
    assert not W.quote_is_today({"price": 100, "previous_close": 98.0}, 100.0)
    assert not W.quote_is_today({"price": None, "previous_close": 100}, 100.0)
    assert not W.quote_is_today(None, 100.0) and not W.quote_is_today({"price": 1, "previous_close": 1}, None)


# ------------------------------------------------------------------------------------------------------ stages
def test_calm_market_morning_brief_and_log():
    md = FakeMD(*_history())
    w, sent, stamps, _ = _engine(md)
    row = w.run_stage("morning", _at(8, 0))
    assert row["ok"] and row["state"] in ("calm", "normal") and row["data_ok"] and row["shadow"] is True
    assert len(sent) == 1 and "Market Weather" in sent[0][0] and "SHADOW" in sent[0][1]
    assert w.status()["current"]["state"] == row["state"] and len(w.read_log()) == 1
    assert not any("/quote/" in u for u in md.calls)                              # morning uses history only


def test_stressed_morning_names_what_live_would_do_and_changes_nothing():
    md = FakeMD(*_history(rng=2.4, step=-0.35, vix=28.0, vix_last=33.0))
    w, sent, stamps, _ = _engine(md)
    row = w.run_stage("morning", _at(8, 0))
    assert row["state"] in ("stressed", "panic")
    assert row["live_would"]["MARKET_STATE_RISK_MULT"] in (0.6, 0.4)
    assert "Live would:" in sent[0][1] and "nothing was changed" in sent[0][1]
    assert stamps == []                                                          # stamping is off by default


def test_lock_adds_the_gap_and_a_big_gap_down_is_a_floor():
    md = FakeMD(*_history(), price=None)
    md.price = md.last * 0.965                                                    # -3.5% at 09:14
    w, sent, _, _ = _engine(md)
    w.run_stage("morning", _at(8, 0))
    row = w.run_stage("lock", _at(9, 14))
    assert row["ok"] and round(row["gap_pct"], 1) == -3.5 and row["state"] == "panic" and row["changed"]
    assert any("PANIC" in b for _, b, _ in sent) and any(u == "high" for *_, u in sent)


def test_tick_only_goes_up_and_only_tells_on_a_change():
    md = FakeMD(*_history())
    w, sent, _, _ = _engine(md)
    w.run_stage("morning", _at(8, 0))
    w.run_stage("refine", _at(8, 50))
    w.run_stage("lock", _at(9, 14))
    base = w.status()["current"]["state"]
    n_msgs = len(sent)
    md.price = md.last * 0.975                                                   # index -2.5% on the day
    md.day_high = md.last * 1.001
    r1 = w.run_stage("tick", _at(9, 20))
    assert r1["state"] in ("stressed", "panic") and r1["changed"] and len(sent) == n_msgs + 1
    md.price, md.day_high = md.last * 1.002, md.last * 1.003                      # recovers fully
    r2 = w.run_stage("tick", _at(9, 25))
    assert r2["state"] == r1["state"] and not r2["changed"] and len(sent) == n_msgs + 1     # held, no message
    assert r1["state"] != base


def test_stale_or_holiday_quote_does_not_move_the_state():
    md = FakeMD(*_history(step=0.2), stale=True)                                 # closes differ day to day, so a one-session-old previous close shows
    md.price = md.last * 0.90
    w, sent, _, _ = _engine(md)
    w.run_stage("morning", _at(8, 0))
    before = w.status()["current"]["state"]
    r = w.run_stage("lock", _at(9, 14))
    assert r["ok"] and "no live quote" in r["note"] and r["gap_pct"] is None and r["state"] == before
    r = w.run_stage("tick", _at(9, 20))
    assert r["ok"] and r["state"] == before and "no live quote" in r["note"]


def test_data_failure_keeps_the_last_good_state_and_never_raises():
    md = FakeMD(*_history(rng=2.4, step=-0.35, vix=28.0, vix_last=33.0))
    w, sent, _, _ = _engine(md)
    w.run_stage("morning", _at(8, 0))
    good = w.status()["current"]["state"]
    md.fail = True
    w2, sent2, _, _ = _engine(md)
    row = w2.run_stage("morning", _at(8, 0))
    assert row["ok"] is False and "error" in row and w2.status()["current"] is None and "failed" in sent2[0][0]
    md.fail = False
    row = w.run_stage("tick", _at(9, 20))                                         # cached features, quote fine
    assert row["ok"] and w.status()["current"]["state"] in ("stressed", "panic", good)


def test_no_vix_still_classifies_from_the_index():
    nd, vd = _history()
    md = FakeMD(nd, {"candles": []})
    w, _, _, _ = _engine(md)
    row = w.run_stage("morning", _at(8, 0))
    assert row["ok"] and row["state"]


def test_state_survives_a_restart_and_a_new_day_starts_clean():
    md = FakeMD(*_history())
    w, _, _, t = _engine(md)
    w.run_stage("morning", _at(8, 0))
    w2 = W.Weather(t, "http://md", fetch=md, enabled=False)
    assert w2.status()["current"] and w2.status()["day"] == TODAY and w2.status()["last_fired"]["morning"] == TODAY
    md2 = FakeMD(*_history(last_day=TODAY))
    w2.fetch = md2
    row = w2.run_stage("morning", _at(8, 0, "2026-10-26"))
    assert row["previous_state"] is None and w2.status()["day"] == "2026-10-26"


# ------------------------------------------------------------------------------------------------------ shadow guarantees
def test_scheduler_pass_respects_the_enabled_switch():
    md = FakeMD(*_history())
    w, sent, _, _ = _engine(md, enabled=False)
    assert w.tick_once(_at(8, 0)) == [] and w.status()["current"] is None
    w.configure(enabled=True)
    assert w.tick_once(_at(8, 0)) == ["morning"] and w.tick_once(_at(8, 1)) == []


def test_stamp_is_off_by_default_and_when_on_it_is_stamp_only():
    md = FakeMD(*_history())
    md.price = md.last * 0.96                                                     # panic at the lock
    w, _, stamps, _ = _engine(md)
    w.run_stage("morning", _at(8, 0))
    w.run_stage("lock", _at(9, 14))
    assert stamps == []
    w2, _, stamps2, _ = _engine(md, stamp_on=True)
    w2.run_stage("morning", _at(8, 0))
    assert stamps2 == []                                                          # morning never stamps
    w2.run_stage("lock", _at(9, 14))
    assert sorted(s for s, _ in stamps2) == ["position-stocks", "real-trade"]
    for _, body in stamps2:
        assert body["entries"] == {} and body["state"] == "panic" and body["version"] == profiles.VERSION and body["until"].endswith("15:30:00+05:30")
    assert W.Weather.message  # keep the import used


def test_no_code_path_can_send_a_dial():
    """Static guarantee: the only overlay body weather.py builds is _stamp_body, whose entries is the literal {}; nothing else mentions entries or build_overlay."""
    src = (HERE / "weather.py").read_text(encoding="utf-8")
    assert "build_overlay" not in src and "overlay_set" not in src
    assert re.findall(r'"entries"\s*:\s*(\S+)', src) == ["{},"] and len(re.findall(r"entries", src.replace("entries {}", ""))) >= 1
    assert ".set(" not in src and "/control/flags" not in src


def test_notify_off_and_manual_run_force():
    md = FakeMD(*_history())
    w, sent, _, _ = _engine(md, notify_on=False)
    w.run_stage("morning", _at(8, 0))
    assert sent == []
    w.configure(notify=True)
    w.run_stage("refine", _at(8, 50), force=True)
    assert len(sent) == 1


def test_log_is_bounded_and_status_is_json():
    md = FakeMD(*_history())
    w, _, _, _ = _engine(md)
    for i in range(W.LOG_MAX_LINES + 5):
        w._log({"i": i})
    assert len(w.read_log(5000)) <= W.LOG_MAX_LINES and w.read_log(1)[0]["i"] == W.LOG_MAX_LINES + 4
    json.dumps(w.status())


def test_unknown_stage_raises():
    w, _, _, _ = _engine(FakeMD(*_history()))
    try:
        w.run_stage("evening")
    except ValueError:
        return
    raise AssertionError("no error")


if __name__ == "__main__":
    try:
        import pytest
        sys.exit(pytest.main([__file__, "-q"]))
    except ImportError:
        fails = 0
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print("ok  ", name)
                except Exception as e:  # noqa: BLE001
                    import traceback
                    fails += 1
                    print("FAIL", name, repr(e)[:300])
                    traceback.print_exc(limit=3)
        sys.exit(1 if fails else 0)
