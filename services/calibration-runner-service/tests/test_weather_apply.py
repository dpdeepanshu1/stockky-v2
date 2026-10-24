"""Part 1 step 6: the gated live apply path (weather_apply.py + its hook in weather.py). Offline: control services and market-data are fakes."""
from __future__ import annotations

import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))

import test_weather as T  # noqa: E402
import weather as W  # noqa: E402
import weather_apply as WA  # noqa: E402
from regime import profiles  # noqa: E402


class Ev:
    def __init__(self, text):
        self.text = text


def _ev(replay="VERDICT: USEFUL", shadow="REGIME_SHADOW: PASS"):
    docs = {"regime_history_replay": replay, "regime_shadow_report": shadow}
    return lambda job, age: (Ev(docs[job]) if docs.get(job) is not None else None)


class FakeControl:
    """flags(): the rows a service reports ('desired' = the SAVED value). overlay(): records the call, or refuses."""

    def __init__(self, modes=None, saved=None, refuse=()):
        self.modes = modes or {"rt": "live", "ps": "live"}
        self.saved = saved or {}
        self.refuse = set(refuse)
        self.flag_calls, self.overlays = [], []

    def flags(self, svc):
        self.flag_calls.append(svc)
        rows = {"MARKET_STATE_MODE": {"desired": self.modes[svc]}}
        for k in profiles.DIALS:
            rows[k] = {"desired": self.saved.get(svc, {}).get(k, DEFAULT[k])}
        return rows

    def overlay(self, svc, body):
        if svc in self.refuse:
            raise OSError("HTTP 409: refused")
        self.overlays.append((svc, body))
        return {}


DEFAULT = {"MARKET_STATE_RISK_MULT": "1", "MARKET_STATE_CONVICTION_ADD": "0", "OPENING_ENTRY_NOT_BEFORE_IST": "09:15", "RISK_MAX_ENTRIES_PER_DAY": "6",
           "ENTRY_LAST_TIME_IST": "14:00", "MAX_CONCURRENT_SCALP_POSITIONS": "5", "SCALP_ENTRY_CUTOFF_IST": "14:00",
           "MARKET_STATE_SHORT_MULT": "1"}
NOW = T._at(9, 14)


# ------------------------------------------------------------------------------------------------------ the evidence gate
def test_gate_needs_both_fresh_good_verdicts():
    assert WA.evidence_gate(_ev())[0] is True
    assert not WA.evidence_gate(lambda j, a: None)[0]
    assert not WA.evidence_gate(_ev(replay="VERDICT: USEFUL_NOT_BEYOND_ATR"))[0]      # a prefix match must not pass
    assert not WA.evidence_gate(_ev(replay="VERDICT: NO_EDGE"))[0]
    assert not WA.evidence_gate(_ev(shadow="REGIME_SHADOW: FAIL"))[0]
    assert not WA.evidence_gate(_ev(shadow="REGIME_SHADOW: NOT_ENOUGH_DATA"))[0]
    assert not WA.evidence_gate(_ev(shadow=None))[0]
    seen = {}
    WA.evidence_gate(lambda j, a: seen.setdefault(j, a) and Ev("VERDICT: USEFUL\nREGIME_SHADOW: PASS"))
    assert seen["regime_history_replay"] == 45 * 86400 and seen["regime_shadow_report"] == 14 * 86400


# ------------------------------------------------------------------------------------------------------ the applier
def test_closed_gate_sends_nothing_and_does_not_even_read_the_services():
    c = FakeControl()
    r = WA.Applier(c, lambda: (False, "no report"), "rt", "ps")("panic", NOW)
    assert r["applied"] is False and "no report" in r["why"] and c.flag_calls == [] and c.overlays == []
    r = WA.Applier(c, lambda: 1 / 0, "rt", "ps")("panic", NOW)
    assert r["applied"] is False and c.overlays == []


def test_open_gate_sends_only_tightenings_against_the_saved_values():
    c = FakeControl(saved={"rt": {"RISK_MAX_ENTRIES_PER_DAY": "2", "ENTRY_LAST_TIME_IST": "11:30"}})        # the person already set these tighter than panic
    r = WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("panic", NOW)
    assert r["applied"] is True
    sent = dict(c.overlays)
    rt, ps = sent["rt"], sent["ps"]
    assert rt["state"] == "panic" and rt["version"] == profiles.VERSION and rt["until"].endswith("15:30:00+05:30")
    assert rt["entries"]["MARKET_STATE_RISK_MULT"] == "0.4" and rt["entries"]["OPENING_ENTRY_NOT_BEFORE_IST"] == "10:15"
    assert "RISK_MAX_ENTRIES_PER_DAY" not in rt["entries"] and "ENTRY_LAST_TIME_IST" not in rt["entries"]   # never loosened toward the profile
    assert set(ps["entries"]) == {"MARKET_STATE_RISK_MULT", "MAX_CONCURRENT_SCALP_POSITIONS", "SCALP_ENTRY_CUTOFF_IST", "MARKET_STATE_SHORT_MULT"}   # real-trade-only dials are not sent to position-stocks; the short dial goes only there
    for body in (rt, ps):
        for k, v in body["entries"].items():
            assert profiles.compare(k, DEFAULT[k], v) == "TIGHTENS"


def test_a_service_not_in_live_mode_gets_nothing_and_the_other_still_does():
    c = FakeControl(modes={"rt": "shadow", "ps": "live"})
    r = WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("stressed", NOW)
    assert [s for s, _ in c.overlays] == ["ps"] and r["applied"] is True and "not live" in r["services"]["real-trade"]["why"]
    c = FakeControl(modes={"rt": "shadow", "ps": "off"})
    r = WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("stressed", NOW)
    assert c.overlays == [] and r["applied"] is False


def test_a_refusing_or_unreachable_service_is_reported_not_raised():
    c = FakeControl(refuse={"rt"})
    r = WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("stressed", NOW)
    assert r["services"]["real-trade"]["ok"] is False and "refused" in r["services"]["real-trade"]["why"] and r["services"]["position-stocks"]["ok"] is True

    class Down(FakeControl):
        def flags(self, svc):
            raise OSError("connection refused")
    r = WA.Applier(Down(), lambda: (True, "ok"), "rt", "ps")("stressed", NOW)
    assert r["applied"] is False and r["why"] == "no service accepted a dial"


def test_calm_and_normal_send_an_empty_stamp_and_nothing_after_1530():
    c = FakeControl()
    WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("normal", NOW)
    assert all(b["entries"] == {} for _, b in c.overlays) and len(c.overlays) == 2
    c = FakeControl()                                                       # calm: real-trade is still a bare stamp; position-stocks gets only the short dial (shorts off)
    WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("calm", NOW)
    got = dict(c.overlays)
    assert got["rt"]["entries"] == {} and got["ps"]["entries"] == {"MARKET_STATE_SHORT_MULT": "0"} and len(c.overlays) == 2
    c = FakeControl()
    r = WA.Applier(c, lambda: (True, "ok"), "rt", "ps")("panic", T._at(15, 40))
    assert c.overlays == [] and r["applied"] is False


# ------------------------------------------------------------------------------------------------------ the hook in weather.py
def _stress_engine(apply_fn, apply_on=True, **kw):
    n, v = T._history(vix_last=30.0, rng=1.0)
    md = T.FakeMD(n, v, price=n["candles"][-1]["close"] * 0.96)
    t = tempfile.mkdtemp()
    sent, stamps, calls = [], [], []

    def ap(state, now):
        calls.append((state, now))
        return apply_fn(state, now)
    w = W.Weather(t, "http://md", fetch=md, notify=lambda a, b, c: sent.append((a, b, c)), stamp=lambda s, b: stamps.append((s, b)), enabled=True,
                  apply=ap, apply_on=apply_on, **kw)
    return w, sent, stamps, calls


OK = lambda state, now: {"applied": True, "state": state, "why": "applied", "services": {"real-trade": {"ok": True, "entries": {"MARKET_STATE_RISK_MULT": "0.4"}}, "position-stocks": {"ok": False, "why": "x"}}}  # noqa: E731


def test_apply_runs_at_the_lock_and_on_a_state_change_only():
    w, sent, stamps, calls = _stress_engine(OK)
    w.run_stage("morning", T._at(8, 0))
    assert calls == []                                              # the morning only logs
    row = w.run_stage("lock", T._at(9, 14))
    assert row["ok"] and len(calls) == 1 and calls[0][0] == row["state"] and row["shadow"] is False
    la = w.status()["last_apply"]
    assert la["applied"] is True and la["stage"] == "lock" and w.status()["mode"] == "live" and w.status()["apply"] is True
    w.run_stage("tick", T._at(9, 20))
    assert len(calls) == 1                                          # same state: nothing new to send
    assert any("dials applied" in a for a, _, _ in sent)


def test_off_by_default_and_a_failed_stage_never_applies():
    w, _, _, calls = _stress_engine(OK, apply_on=False)
    w.run_stage("lock", T._at(9, 14))
    assert calls == [] and w.status()["mode"] == "shadow" and w.status()["apply"] is False
    w.configure(apply=True)
    n, v = T._history()
    bad = T.FakeMD(n, v)
    bad.fail = True
    w.fetch = bad
    row = w.run_stage("morning", T._at(8, 0))
    assert row["ok"] is False and calls == []


def test_an_apply_that_crashes_is_contained_and_the_stamp_still_goes_to_unapplied_services():
    def boom(state, now):
        raise RuntimeError("bug")
    w, _, stamps, calls = _stress_engine(boom, stamp_on=True)
    row = w.run_stage("lock", T._at(9, 14))
    assert row["ok"] and w.status()["last_apply"]["applied"] is False
    assert {s for s, _ in stamps} == {"real-trade", "position-stocks"}      # nothing applied, so both still stamped
    w2, _, stamps2, _ = _stress_engine(OK, stamp_on=True)
    w2.run_stage("lock", T._at(9, 14))
    assert {s for s, _ in stamps2} == {"position-stocks"}                    # real-trade's dial overlay already stamps it


def test_without_an_applier_the_switch_does_nothing():
    n, v = T._history(vix_last=30.0)
    w = W.Weather(tempfile.mkdtemp(), "http://md", fetch=T.FakeMD(n, v), enabled=True, apply_on=True)
    assert w.status()["mode"] == "shadow" and w.status()["apply"] is False


# ------------------------------------------------------------------------------------------------------ static guarantees
def test_only_weather_apply_can_build_a_dial_and_nothing_writes_a_switch():
    wsrc = (HERE / "weather.py").read_text(encoding="utf-8")
    assert "build_overlay" not in wsrc and "overlay_set" not in wsrc and ".set(" not in wsrc and "/control/flags" not in wsrc
    asrc = (HERE / "weather_apply.py").read_text(encoding="utf-8")
    assert asrc.count("build_overlay(") == 1
    assert ".set(" not in asrc and "/control/flags" not in asrc and "overlay_clear" not in asrc
    assert re.search(r'_b\("WEATHER_APPLY", False\)', (HERE / "config.py").read_text(encoding="utf-8"))
    assert "weather_apply.py" in (HERE / "Dockerfile").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------------ E98: WEATHER_GATE_MODE
def test_gate_mode_replay_only_needs_only_the_replay():
    assert WA.evidence_gate(_ev(shadow=None), "replay_only")[0] is True                       # no shadow report at all: open
    assert WA.evidence_gate(_ev(shadow="REGIME_SHADOW: FAIL"), "replay_only")[0] is True       # a failing shadow report does not matter here
    assert not WA.evidence_gate(_ev(replay="VERDICT: NO_EDGE"), "replay_only")[0]
    assert not WA.evidence_gate(_ev(replay="VERDICT: NOT_ENOUGH_DATA"), "replay_only")[0]
    assert not WA.evidence_gate(_ev(replay="VERDICT: USEFUL_NOT_BEYOND_ATR"), "replay_only")[0]
    assert not WA.evidence_gate(lambda j, a: None, "replay_only")[0]                           # no fresh replay: closed
    seen = {}
    WA.evidence_gate(lambda j, a: seen.setdefault(j, a) and Ev("VERDICT: USEFUL"), "replay_only")
    assert list(seen) == ["regime_history_replay"] and seen["regime_history_replay"] == 45 * 86400.0   # the shadow report is never even asked for


def test_gate_mode_strict_is_todays_behaviour():
    assert WA.evidence_gate(_ev(), "strict")[0] is True
    assert not WA.evidence_gate(_ev(shadow=None), "strict")[0]
    assert not WA.evidence_gate(_ev(shadow="REGIME_SHADOW: FAIL"), "strict")[0]
    assert not WA.evidence_gate(_ev(replay="VERDICT: NO_EDGE"), "strict")[0]
    assert WA.evidence_gate(_ev(shadow=None))[0] is False                                      # the default argument stays strict


def test_gate_mode_off_and_unknown_values():
    ok, why = WA.evidence_gate(lambda j, a: None, "off")
    assert ok and "off" in why
    assert WA.norm_gate_mode("") == "replay_only" and WA.norm_gate_mode(None) == "replay_only"
    assert WA.norm_gate_mode("REPLAY_ONLY") == "replay_only"
    assert WA.norm_gate_mode("sloppy") == "strict"                                             # a typo never loosens the gate
    assert not WA.evidence_gate(_ev(shadow=None), "sloppy")[0]


def test_ensure_replay_queues_only_when_needed():
    calls = []
    submit = lambda *a, **k: calls.append((a, k))
    assert "fresh" in WA.ensure_replay(_ev(), submit, "replay_only") and not calls
    assert "no replay needed" in WA.ensure_replay(lambda j, a: None, submit, "off") and not calls
    note = WA.ensure_replay(lambda j, a: None, submit, "replay_only")
    assert "queued" in note and calls[0][0][:2] == ("job", "regime_history_replay")
    assert WA.ensure_replay(lambda j, a: None, submit, "strict").startswith("no fresh replay")
    def boom(*a, **k):
        raise ValueError("already waiting in the queue")
    assert WA.ensure_replay(lambda j, a: None, boom, "replay_only").startswith("replay not queued")   # never raises


def test_gate_status_for_the_card_and_applier_follows_the_mode():
    mode = {"v": "replay_only"}
    gate = lambda: WA.evidence_gate(_ev(shadow=None), mode["v"])
    ap = WA.Applier(FakeControl(), gate, mode_fn=lambda: mode["v"])
    g = ap.gate_status()
    assert g["mode"] == "replay_only" and g["open"] is True and "replay USEFUL" in g["why"]
    assert ap(  "stressed", NOW)["applied"] is True                                            # opens with no shadow report
    mode["v"] = "strict"
    g = ap.gate_status()
    assert g["mode"] == "strict" and g["open"] is False and "regime_shadow_report" in g["why"]
    out = ap("stressed", NOW)
    assert out["applied"] is False and out["why"].startswith("not applied:")


def test_weather_status_carries_the_gate():
    src = (HERE / "weather.py").read_text(encoding="utf-8")
    assert '"gate": self._gate_status()' in src
    main = (HERE / "main.py").read_text(encoding="utf-8")
    assert "config.WEATHER_GATE_MODE" in main and "WA.ensure_replay" in main
