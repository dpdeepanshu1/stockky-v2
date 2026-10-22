"""E29: scheduler/pattern_alerts.py - daily watchlist chart-signal digest (alert only). No network, no clock, no real files outside a temp dir.

Written as plain functions (pytest runs them unchanged; `python3 tests/test_pattern_alerts.py` runs them without pytest)."""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

try:
    import httpx  # noqa: F401
except ImportError:                      # a bare interpreter without the service requirements: the module only needs the name at import
    import types
    sys.modules["httpx"] = types.ModuleType("httpx")

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler", "pattern_alerts.py")
_n = itertools.count()
TODAY = date(2026, 10, 23)
NOW = datetime(2026, 10, 23, 16, 10)


def _load(env=None):
    """Fresh module copy with a private state dir; `env` entries are set (None = removed) before import."""
    env = dict(env or {})
    env.setdefault("SCHEDULER_STATE_DIR", tempfile.mkdtemp())
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(f"pattern_alerts_t{next(_n)}", _PATH)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
        return m
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


class _env:
    """Set / remove environment variables for the duration of a `with` block (the module reads them at call time)."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _payload(*badges, ok=True):
    return {"ok": ok, "badges": list(badges)}


CONFIRMED = {"key": "double_bottom", "kind": "chart", "label": "Double bottom", "tone": "good", "status": "confirmed"}
FORMING = {"key": "bull_flag", "kind": "chart", "label": "Bull flag", "tone": "good", "status": "forming"}
GOLDEN = {"key": "ma50_200_golden_cross", "kind": "signal", "label": "Golden cross 50/200", "tone": "good", "bars_ago": 0}
OLD_CROSS = {"key": "ma20_50_death_cross", "kind": "signal", "label": "Death cross 20/50", "tone": "bad", "bars_ago": 4}
SQUEEZE = {"key": "squeeze", "kind": "signal", "label": "Squeeze", "tone": "warn"}
RELEASE = {"key": "squeeze_release", "kind": "signal", "label": "Squeeze broke up", "tone": "good"}


def test_extract_only_fresh_confirmed_signals():
    m = _load()
    ev = m.extract_events("TCS", _payload(CONFIRMED, FORMING, GOLDEN, OLD_CROSS, SQUEEZE, RELEASE, {"key": "regime_trend", "kind": "regime", "tone": "good"}))
    assert [e["type"] for e in ev] == ["breakout", "cross", "squeeze", "squeeze_release"]     # forming pattern, 4-bar-old cross and regime chip are out
    assert ev[0]["id"] == "TCS:breakout:double_bottom" and "breakout above" in ev[0]["text"] and ev[1]["text"].endswith("(today)")
    bear = m.extract_events("X", _payload({"key": "double_top", "kind": "chart", "label": "Double top", "tone": "bad", "status": "confirmed"}))
    assert "breakdown below" in bear[0]["text"]


def test_events_list_wins_over_the_capped_badges():
    m = _load()
    capped = {"ok": True, "badges": [{"key": "regime_trend", "kind": "regime", "tone": "good"}], "events": [GOLDEN, CONFIRMED]}
    assert [e["type"] for e in m.extract_events("TCS", capped)] == ["cross", "breakout"]      # badges had lost both; events still has them
    assert m.extract_events("TCS", {"ok": True, "badges": [GOLDEN], "events": []}) == []        # an empty events list is an answer, not a fallback
    assert len(m.extract_events("TCS", {"ok": True, "badges": [GOLDEN]})) == 1                  # an older payload without events falls back to badges


def test_unusable_payloads_give_nothing_and_never_raise():
    m = _load()
    for p in (None, {}, "x", [], _payload(CONFIRMED, ok=False), {"ok": True, "badges": None}, {"ok": True, "badges": ["x", 3, None]},
              _payload({"key": "a_golden_cross", "kind": "signal", "bars_ago": "n/a"})):
        assert m.extract_events("X", p) == []


def test_quiet_period_by_kind():
    m = _load()
    ev = m.extract_events("TCS", _payload(CONFIRMED, GOLDEN, SQUEEZE, RELEASE))
    state = {e["id"]: (TODAY - timedelta(days=3)).isoformat() for e in ev}
    left = {e["type"] for e in m.filter_new(ev, state, TODAY)}
    assert left == {"squeeze_release"}           # sent 3 days ago: breakout (10 d), cross and squeeze (5 d) are still quiet; release (3 d) is due again
    old = {e["id"]: (TODAY - timedelta(days=11)).isoformat() for e in ev}
    assert len(m.filter_new(ev, old, TODAY)) == 4
    assert len(m.filter_new(ev, {ev[0]["id"]: "not-a-date"}, TODAY)) == 4     # unreadable state = send


def test_digest_is_sorted_capped_and_honest():
    m = _load()
    evs = []
    for i in range(6):
        evs += m.extract_events(f"S{i}", _payload(SQUEEZE))
    evs += m.extract_events("ZZZ", _payload(CONFIRMED))
    d = m.format_digest(evs, TODAY, max_lines=4)
    lines = d["message"].splitlines()
    assert "ZZZ" in lines[1] and "S0" in lines[2]                 # breakout first, then squeezes by symbol
    assert any("and 3 more" in ln for ln in lines) and d["title"].startswith("📈 7 chart signals")
    assert "not yet proven" in d["message"] and "does not trade" in d["message"]
    assert m.format_digest([], TODAY, 10) is None
    assert m.format_digest(m.extract_events("A", _payload(SQUEEZE)), TODAY, 10)["title"].startswith("📈 1 chart signal ")


def test_state_roundtrip_prunes_old_and_survives_garbage():
    m = _load()
    assert m.load_state() == {}
    m.save_state({"A:squeeze": TODAY.isoformat(), "B:squeeze": (TODAY - timedelta(days=90)).isoformat(), "C:x": "junk"}, TODAY)
    assert m.load_state() == {"A:squeeze": TODAY.isoformat()}
    with open(m.STATE_PATH, "w") as f:
        f.write("{not json")
    assert m.load_state() == {}


def test_config_is_blank_safe_and_bounded():
    m = _load()
    with _env(PATTERN_ALERTS_ENABLED="", PATTERN_ALERTS_TIME="", PATTERN_ALERTS_MAX_LINES="  "):
        assert m.enabled() is True and m.run_time() == "16:05" and m._int_env("PATTERN_ALERTS_MAX_LINES", 25, 3, 100) == 25
    with _env(PATTERN_ALERTS_ENABLED="false", PATTERN_ALERTS_TIME="25:99", PATTERN_ALERTS_MAX_LINES="many"):
        assert m.enabled() is False and m.run_time() == "16:05" and m._int_env("PATTERN_ALERTS_MAX_LINES", 25, 3, 100) == 25
    with _env(PATTERN_ALERTS_TIME="9:5", PATTERN_ALERTS_MAX_LINES="9999"):
        assert m.run_time() == "09:05" and m._int_env("PATTERN_ALERTS_MAX_LINES", 25, 3, 100) == 100


def test_due_only_on_weekdays_after_the_time_once_a_day():
    m = _load()
    assert m.due(datetime(2026, 10, 23, 16, 5), None) is True            # Friday 16:05
    assert m.due(datetime(2026, 10, 23, 16, 4), None) is False
    assert m.due(datetime(2026, 10, 23, 18, 0), "2026-10-23") is False   # already done today
    assert m.due(datetime(2026, 10, 24, 17, 0), None) is False           # Saturday
    with _env(PATTERN_ALERTS_ENABLED="0"):
        assert m.due(datetime(2026, 10, 23, 17, 0), None) is False


def _wire(m, watch, charts, accept=True):
    sent = []
    m.fetch_watchlist = lambda: list(watch)
    m.fetch_chart = lambda s: charts[s] if not isinstance(charts[s], Exception) else (_ for _ in ()).throw(charts[s])
    m.send = lambda title, msg: (sent.append((title, msg)) or accept)
    m.time.sleep = lambda *_: None
    return sent


def test_run_sends_once_then_stays_quiet_and_dry_run_sends_nothing():
    m = _load()
    sent = _wire(m, ["TCS", "INFY", "BAD"], {"TCS": _payload(CONFIRMED, GOLDEN), "INFY": _payload(SQUEEZE), "BAD": RuntimeError("boom")})
    dry = m.run_once(dry_run=True, now=NOW)
    assert dry["ok"] and dry["new"] == 3 and dry["sent"] is False and sent == [] and m.load_state() == {}
    r = m.run_once(now=NOW)
    assert r["ok"] and r["sent"] is True and r["symbols"] == 3 and len(sent) == 1 and len(m.load_state()) == 3     # one digest, the failing symbol skipped
    r2 = m.run_once(now=NOW)
    assert r2["ok"] and r2["new"] == 0 and r2["sent"] is False and len(sent) == 1                                 # nothing repeats the same day
    r3 = m.run_once(now=datetime(2026, 10, 27, 16, 10))                                                            # 4 days later: everything still quiet
    assert r3["new"] == 0


def test_rejected_digest_is_not_marked_sent_and_errors_are_reported():
    m = _load()
    sent = _wire(m, ["TCS"], {"TCS": _payload(CONFIRMED)}, accept=False)
    r = m.run_once(now=NOW)
    assert r["ok"] is False and r["sent"] is False and "did not accept" in r["error"] and m.load_state() == {}
    sent.clear()
    m.send = lambda t, msg: True
    assert m.run_once(now=NOW)["sent"] is True                      # the retry sends it
    m.fetch_watchlist = lambda: (_ for _ in ()).throw(RuntimeError("gateway down"))
    r = m.run_once(now=NOW)
    assert r["ok"] is False and "gateway down" in r["error"] and m.status()["last"]["status"] == "error"


def test_status_says_alert_only_and_symbol_cap_applies():
    m = _load()
    with _env(PATTERN_ALERTS_MAX_SYMBOLS="2"):
        assert m.status()["alert_only"] is True and m.status()["max_symbols"] == 2
        _wire(m, ["A", "B", "C", "D"], {k: _payload() for k in "ABCD"})
        assert m.run_once(dry_run=True, now=NOW)["symbols"] == 2


if __name__ == "__main__":
    import traceback
    ok = bad = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            try:
                f(); ok += 1
            except Exception:
                bad += 1; print(k); traceback.print_exc()
    print(f"{ok} passed, {bad} failed")
    sys.exit(1 if bad else 0)
