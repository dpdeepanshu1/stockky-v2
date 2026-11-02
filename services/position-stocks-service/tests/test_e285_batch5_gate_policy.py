"""E285 batch 5: adaptive gate policy (HARD / SOFT / OFF), nightly job, entry wiring."""
import calendar
import types
from datetime import datetime, timedelta, timezone

import pytest

import config
from orders import gate_policy as gp
from orders import gate_policy_job as job
from screening import opening_gate as og, prev_day

G, B, N, E = gp.GAP_DOWN, gp.BELOW_OPEN, gp.NEAR_DAY_HIGH, gp.EVENT


# ------------------------------------------------------------------ state()

def test_no_row_follows_the_config(monkeypatch):
    monkeypatch.setattr(config, "OPENING_BELOW_OPEN_MODE", "veto")
    monkeypatch.setattr(config, "OPENING_PARTS_MODE", "penalty")
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", True)
    assert (gp.state(G), gp.state(B), gp.state(N), gp.state(E)) == (gp.HARD, gp.HARD, gp.SOFT, gp.SOFT)
    monkeypatch.setattr(config, "OPENING_BELOW_OPEN_MODE", "penalty")
    monkeypatch.setattr(config, "OPENING_PARTS_MODE", "veto")
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", False)
    assert (gp.state(B), gp.state(N), gp.state(E)) == (gp.SOFT, gp.HARD, gp.HARD)


def test_a_row_overrides_in_adaptive_and_modes_switch_it_off(monkeypatch):
    gp.set_cache({(G, "LONG"): "SOFT", (B, "LONG"): "HARD"})
    monkeypatch.setattr(config, "OPENING_BELOW_OPEN_MODE", "penalty")
    assert gp.state(G) == gp.SOFT and gp.state(B) == gp.HARD
    monkeypatch.setattr(config, "GATE_POLICY_MODE", "static")           # the rollback: rows ignored
    assert gp.state(G) == gp.HARD and gp.state(B) == gp.SOFT
    monkeypatch.setattr(config, "GATE_POLICY_MODE", "all_hard")
    assert all(gp.state(g) == gp.HARD for g in gp.GATES)
    monkeypatch.setattr(config, "GATE_POLICY_MODE", "nonsense")         # anything unknown reads as adaptive
    assert gp.state(G) == gp.SOFT


def test_the_news_gate_has_no_off_state_and_garbage_rows_are_ignored(monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", True)
    gp.set_cache({(E, "LONG"): "OFF", ("NOT_A_GATE", "LONG"): "SOFT", (G, "LONG"): "WEIRD"})
    assert gp.state(E) == gp.SOFT and gp.state(G) == gp.HARD and gp.event_half_size()
    gp.set_cache({(E, "LONG"): "HARD"})
    assert not gp.event_half_size()


def test_safety_gates_are_not_adaptable():
    names = " ".join(gp.GATES)
    for banned in ("CIRCUIT", "LIQUIDITY", "SPREAD", "DEPTH", "STALE", "CAPITAL", "MAX_POSITIONS", "SHORT_TOO_EARLY", "DAY_GAIN", "NEWS_NEGATIVE", "NEG_EVENT", "EARNINGS"):
        assert banned not in names.upper()
    assert set(gp.GATES) == {G, B, N, E}


# ------------------------------------------------------------------ statistics and rules

def _s(vals, days=6, syms=25):
    return [(f"2026-10-{20 + (i % days):02d}", f"S{i % syms:02d}", v) for i, v in enumerate(vals)]


def test_bootstrap_is_deterministic_and_loo_sees_a_single_day():
    a = job.summarize(_s([0.5] * 40))
    assert a == job.summarize(_s([0.5] * 40)) and a["mean"] == 0.5 and a["lb"] == 0.5 and a["days"] == 6 and a["symbols"] == 25 and a["loo_min"] == 0.5
    one_day = [(f"2026-10-{20 if i < 8 else 21 + i % 5}", f"S{i % 25:02d}", 9.0 if i < 8 else -0.1) for i in range(40)]
    st = job.summarize(one_day)
    assert st["mean"] > 0 and st["loo_min"] < 0                          # removing the big day leaves a loss
    assert job.summarize([])["n"] == 0 and job.bootstrap_lower_bound([1.0]) is None


def _ev(cur, st, last=None, gate=G, today="2026-11-02"):
    return job.evaluate_gate(gate, cur, st, today, last)


GOOD = {"n": 40, "days": 6, "symbols": 25, "mean": 0.8, "lb": 0.3, "loo_min": 0.5}


def test_hard_to_soft_only_when_every_condition_holds():
    r = _ev("HARD", GOOD)
    assert r["state"] == "SOFT" and r["changed"]
    for bad, text in (({"n": 29}, "n >= 30"), ({"days": 4}, "5 days"), ({"symbols": 19}, "20 symbols"), ({"lb": 0.0}, "lower bound"), ({"lb": None}, "lower bound"),
                      ({"loo_min": -0.01}, "one day"), ({"loo_min": 0.0}, "one day"), ({"loo_min": None}, "one day")):
        r = _ev("HARD", {**GOOD, **bad})
        assert r["state"] == "HARD" and not r["changed"] and text in r["note"], (bad, r)


def test_soft_to_hard_needs_n_and_a_clearly_negative_mean():
    neg = {"n": 35, "days": 6, "symbols": 22, "mean": -0.4, "lb": -0.7, "loo_min": -0.3}
    assert _ev("SOFT", neg, gate=E)["state"] == "HARD"
    assert _ev("SOFT", {**neg, "mean": -0.05}, gate=E)["state"] == "SOFT"          # inside the hysteresis band
    assert _ev("SOFT", {**neg, "mean": -0.10}, gate=E)["state"] == "SOFT"          # exactly on the band edge: not under it
    assert _ev("SOFT", {**neg, "n": 29}, gate=E)["state"] == "SOFT"
    assert _ev("SOFT", {**neg, "mean": 0.3}, gate=E)["state"] == "SOFT"


def test_one_step_per_week_and_off_is_left_alone():
    r = _ev("HARD", GOOD, last="2026-10-30")
    assert r["state"] == "HARD" and not r["changed"] and "one step per 7 days" in r["note"]
    assert _ev("HARD", GOOD, last="2026-10-26")["changed"]                          # 7 days ago: allowed
    assert _ev("OFF", GOOD)["state"] == "OFF" and not _ev("OFF", GOOD)["changed"]
    assert _ev("HARD", GOOD, gate="NO_SUCH_GATE")["state"] == "HARD"                 # no SOFT state for an unknown gate: never moves


def test_flat_in_the_middle_does_not_flip_flop():
    mid = {"n": 40, "days": 6, "symbols": 25, "mean": 0.02, "lb": -0.2, "loo_min": -0.1}
    assert _ev("HARD", mid)["state"] == "HARD" and _ev("SOFT", mid, gate=E)["state"] == "SOFT"


# ------------------------------------------------------------------ samples

def _bars(day, gain):
    """1-minute bars for the IST `day`: flat 100 until 10:00, linear to 100+gain at 11:00, flat after (a long entered at 10:00 gains `gain` % in 60 min)."""
    y, m, d = (int(x) for x in day.split("-"))
    t_open = calendar.timegm((y, m, d, 3, 45, 0))                      # 09:15 IST
    out = []
    for k in range(0, 376):                                            # to 15:30
        t = t_open + 60 * k
        frac = min(1.0, max(0.0, (t - (t_open + 45 * 60)) / 3600.0))
        px = 100.0 * (1 + gain / 100.0 * frac)
        out.append([t, px, px + 0.01, px - 0.01, px, 1000])
    return out


def _ts(day, hh=10, mm=0):
    y, m, d = (int(x) for x in day.split("-"))
    return calendar.timegm((y, m, d, hh, mm, 0)) - 19800


def test_samples_dedupe_cost_gate_keys_and_soft_rows():
    day = "2026-10-27"
    rows = [
        {"symbol": "AAA", "decision": "SKIPPED", "reason": "OPENING_GATE:GAP_DOWN:-1.50% < -1.00%", "created_ts": _ts(day)},
        {"symbol": "AAA", "decision": "SKIPPED", "reason": "OPENING_GATE:GAP_DOWN:-1.60% < -1.00%", "created_ts": _ts(day, 10, 10)},       # same 30-min bucket: dropped
        {"symbol": "AAA", "decision": "SOFT", "reason": "OPENING_GATE:GAP_DOWN:-1.60% < -1.00% (size penalty 1.0, SOFT)", "created_ts": _ts(day, 10, 40)},     # next bucket: kept
        {"symbol": "BBB", "decision": "SOFT", "reason": "QUALITY_GATE:EVENT_CHECK_UNAVAILABLE: the news service did not answer", "created_ts": _ts(day)},
        {"symbol": "CCC", "decision": "SKIPPED", "reason": "OPENING_GATE:BELOW_OPEN:0.40% under the open", "created_ts": _ts(day)},
        {"symbol": "DDD", "decision": "SKIPPED", "reason": "OPENING_GATE:NEAR_DAY_HIGH:range_pos=0.97 >= 0.90", "created_ts": _ts(day)},
        {"symbol": "EEE", "decision": "SKIPPED", "reason": "STOP_ATR_FLOOR:x", "created_ts": _ts(day)},                                                          # not an adaptive gate
        {"symbol": "FFF", "decision": "SKIPPED", "reason": "OPENING_GATE:GAP_DOWN:-2%", "created_ts": _ts("2026-10-28")},                                          # no bars file
    ]
    got, c = job.samples_from_rows(rows, lambda sym, d: _bars(d, 1.0) if sym != "FFF" else None, 0.15)
    assert sorted(got) == sorted([(G, "LONG"), (E, "LONG"), (B, "LONG"), (N, "LONG")])
    assert len(got[(G, "LONG")]) == 2 and c["deduped"] == 1 and c["no_bars"] == 1 and c["samples"] == 5
    assert abs(got[(G, "LONG")][0][2] - 0.85) < 1e-6                              # the 10:00 sample: +1.00 % move minus 0.15 % cost (the 10:40 one starts later up the ramp)
    assert all(abs(got[(g, "LONG")][0][2] - 0.85) < 1e-6 for g in (B, N, E))
    assert job.gate_key("QUALITY_GATE:EVENT_CHECK_UNAVAILABLE: x", "SKIPPED") == E


# ------------------------------------------------------------------ the job against a real (sqlite) database

@pytest.fixture()
def factory():
    import models
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng, tables=[models.ScalpCandidateLog.__table__, models.ScalpGatePolicy.__table__])
    return sessionmaker(bind=eng)


NOW = datetime(2026, 11, 2, 11, 30, tzinfo=timezone.utc)            # 17:00 IST, a Monday


def _fill(factory, gate_reason, n, gains, decision="SKIPPED", days=6, syms=25):
    import models
    s = factory()
    for i in range(n):
        day = (datetime(2026, 10, 27) + timedelta(days=i % days)).strftime("%Y-%m-%d")
        s.add(models.ScalpCandidateLog(symbol=f"S{i % syms:02d}", window_source="5m", pct_change=2.0, decision=decision, reason=gate_reason,
                                       created_at=datetime.utcfromtimestamp(_ts(day))))
    s.commit()
    s.close()
    return lambda sym, d: _bars(d, gains(d))


def test_first_run_relaxes_a_gate_only_when_the_evidence_passes_and_the_table_shows_it(factory):
    loader = _fill(factory, "OPENING_GATE:GAP_DOWN:-1.50% < -1.00%", 40, lambda d: 1.0)
    rep = job.run(factory, NOW, force=True, load_bars=loader)
    assert rep["ok"] and rep["gates"][f"{G}|LONG"]["state"] == "SOFT" and rep["gates"][f"{G}|LONG"]["changed"]
    for other in (B, N, E):                                                   # no evidence: stays at what its config gives, and is NOT an override
        assert not rep["gates"][f"{other}|LONG"]["changed"]
    assert gp.state(G) == gp.SOFT
    import models
    s = factory()
    row = s.query(models.ScalpGatePolicy).filter_by(gate=G, side="LONG").one()
    assert row.state == "SOFT" and row.last_change_day == "2026-11-02" and row.n_samples == 40 and row.n_days == 6 and row.n_symbols == 25 and row.lb_net60 > 0
    assert s.query(models.ScalpGatePolicy).count() == 4
    s.close()
    again = job.run(factory, NOW)                                              # same day, not forced
    assert again["skipped"] == "already evaluated today"


def test_a_failing_gate_is_never_relaxed_and_no_override_is_written(factory):
    loader = _fill(factory, "OPENING_GATE:GAP_DOWN:-1.50% < -1.00%", 40, lambda d: 9.0 if d == "2026-10-27" else -0.1)
    rep = job.run(factory, NOW, force=True, load_bars=loader)
    r = rep["gates"][f"{G}|LONG"]
    assert r["state"] == "HARD" and not r["changed"] and "one day" in r["note"]
    assert gp.state(G) == gp.HARD
    import models
    s = factory()
    assert s.query(models.ScalpGatePolicy).filter_by(gate=G).one().last_change_day is None
    s.close()
    gp.clear_cache()
    assert gp.refresh(factory) and gp.state(G) == gp.HARD


def test_soft_back_to_hard_on_negative_soft_rows_then_one_step_a_week(factory, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", True)     # the news gate starts SOFT
    loader = _fill(factory, "QUALITY_GATE:EVENT_CHECK_UNAVAILABLE: the news service did not answer (half size, SOFT)", 40, lambda d: -1.0, decision="SOFT")
    rep = job.run(factory, NOW, force=True, load_bars=loader)
    assert rep["gates"][f"{E}|LONG"]["from"] == "SOFT" and rep["gates"][f"{E}|LONG"]["state"] == "HARD"
    assert not gp.event_half_size()
    loader2 = lambda sym, d: _bars(d, 2.0)                                             # a week later it recovers: the bars now say +2 %, but the weekly limit holds on day 3
    rep2 = job.run(factory, NOW + timedelta(days=1), force=True, load_bars=loader2)
    assert rep2["gates"][f"{E}|LONG"]["state"] == "HARD"                               # (HARD -> SOFT also needs the 7-day gap)


def test_operator_set_and_off_and_refresh_reads_only_moved_rows(factory):
    assert job.set_state(factory, G, "OFF")["ok"] and gp.state(G) == gp.OFF
    assert not job.set_state(factory, E, "OFF")["ok"] and not job.set_state(factory, "X", "SOFT")["ok"]
    job.run(factory, NOW, force=True, load_bars=lambda s, d: None)
    assert gp.state(G) == gp.OFF                                                       # the job leaves OFF alone
    gp.clear_cache()
    assert gp.refresh(factory) and gp.state(G) == gp.OFF and gp.state(B) == gp.static_state(B)


def test_a_db_failure_changes_nothing():
    gp.set_cache({(G, "LONG"): "SOFT"})
    boom = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    assert job.run(boom, NOW, force=True)["ok"] is False
    assert not gp.refresh(boom) and gp.state(G) == gp.SOFT                              # cache kept
    assert job._retry_after > 0 and not job.due(datetime(2026, 11, 2, 11, 40, tzinfo=timezone.utc))


def test_due_is_once_per_trading_day_after_the_time():
    job._last_run_day, job._retry_after = None, 0.0                                   # module state: other tests (a failed run sets a retry delay) must not leak in
    at = lambda h, m, d=2: datetime(2026, 11, d, h, m, tzinfo=timezone.utc)            # UTC; 16:30 IST = 11:00 UTC
    assert not job.due(at(10, 59)) and job.due(at(11, 5)) and not job.due(at(11, 5, 1) .replace(day=7))   # Sat 7 Nov: weekend
    job._last_run_day = "2026-11-02"
    assert not job.due(at(11, 5))


# ------------------------------------------------------------------ the gates themselves

NOW_IST = datetime(2026, 11, 2, 9, 40)
ST = {"stats": (98.5, 99.5, 98.0, 100.0), "nifty": 0.2, "nprev": 0.3}


def _gate_env(parts="penalty", below="penalty"):
    fw = types.ModuleType("feed.ws_client")
    fw.get_day_stats = lambda s: ST["stats"]
    base = datetime(2026, 11, 2, 9, 15).timestamp()
    fw.get_tick_buffer = lambda s: [(base + 30 * i, 98.0 + (i % 5) * 0.4) for i in range(30)]
    import sys
    feed = sys.modules.get("feed") or types.ModuleType("feed")
    feed.ws_client = fw
    sys.modules["feed"], sys.modules["feed.ws_client"] = feed, fw
    tg = types.ModuleType("screening.trade_gates")
    tg.last_nifty_change_pct = lambda: ST["nifty"]
    tg.last_nifty_prev_close_pct = lambda: ST["nprev"]
    import screening
    screening.trade_gates = tg
    sys.modules["screening.trade_gates"] = tg
    pdv = types.SimpleNamespace(close_pos=0.8, atr_pct=2.0, date="2026-10-30", low=95.0, high=101.0, close=100.0)
    return pdv


@pytest.fixture()
def gate_env(monkeypatch):
    pdv = _gate_env()
    monkeypatch.setattr(og, "is_active", lambda now=None: True)
    monkeypatch.setattr(og, "is_market_open_ist", lambda now=None: True)
    monkeypatch.setattr(og, "ist_now", lambda now=None: NOW_IST)
    monkeypatch.setattr(prev_day, "peek", lambda s: pdv)
    monkeypatch.setattr(prev_day, "get", lambda s: pdv)
    for k, v in dict(OPENING_PARTS_MODE="penalty", OPENING_BELOW_OPEN_MODE="penalty", MARKET_GATE_ENABLED=True, SOFT_GATE_MAX_PENALTY=3.0, OPENING_NIFTY_FULL_PENALTY_PCT=0.5,
                     OPENING_GATE_MAX_GAP_DOWN_PCT=1.0, GATE_POLICY_SOFT_PENALTY=1.0).items():
        monkeypatch.setattr(config, k, v, raising=False)
    ST.update(stats=(98.5, 99.5, 98.0, 100.0), nifty=0.2, nprev=0.3)
    return pdv


def test_gap_down_hard_refuses_soft_becomes_a_half_size_penalty_off_ignores(gate_env):
    assert str(og.reject_reason("A", 98.8, NOW_IST)).startswith("OPENING_GATE:GAP_DOWN")           # -1.5 % gap, nothing in the table: refused, as always
    assert "gap_down" not in og.parts_penalties("A", 98.8, NOW_IST)
    gp.set_cache({(G, "LONG"): "SOFT"})
    assert og.reject_reason("A", 98.8, NOW_IST) is None
    p = og.parts_penalties("A", 98.8, NOW_IST)["gap_down"]
    assert p["penalty"] == 1.0 and p["why"].startswith("OPENING_GATE:GAP_DOWN:-1.50%")             # size / (1 + 1) = half
    gp.set_cache({(G, "LONG"): "OFF"})
    assert og.reject_reason("A", 98.8, NOW_IST) is None and "gap_down" not in og.parts_penalties("A", 98.8, NOW_IST)
    gp.set_cache({(G, "LONG"): "SOFT"})
    ST["stats"] = (99.6, 99.8, 99.0, 100.0)                                                        # -0.4 % gap: inside the limit, no penalty
    assert "gap_down" not in og.parts_penalties("A", 99.7, NOW_IST)


def test_a_gap_down_soft_gate_does_not_change_the_other_tests(gate_env):
    gp.set_cache({(G, "LONG"): "SOFT"})
    ST["stats"] = (96.0, 99.5, 95.0, 100.0)                                                        # open 4 % under; price 2.5 % under the previous close = still refused by BELOW_PREV_CLOSE
    assert str(og.reject_reason("A", 97.0, NOW_IST)).startswith("OPENING_GATE:BELOW_PREV_CLOSE")


def test_below_open_mode_follows_the_state(gate_env, monkeypatch):
    assert og.below_open_mode() == "penalty"
    gp.set_cache({(B, "LONG"): "HARD"})
    assert og.below_open_mode() == "veto" and og.below_open_penalty("A", 98.0, NOW_IST) is None
    gp.set_cache({(B, "LONG"): "OFF"})
    assert og.below_open_mode() == "off" and og.below_open_penalty("A", 98.0, NOW_IST) is None
    gp.clear_cache()
    monkeypatch.setattr(config, "OPENING_BELOW_OPEN_MODE", "veto")
    assert og.below_open_mode() == "veto"


def test_near_day_high_state_controls_refusal_and_penalty_independently_of_the_other_parts(gate_env, monkeypatch):
    ST["stats"] = (100.0, 104.0, 99.0, 99.0)                                                        # price 103.9 = range_pos 0.98
    assert "near_day_high" in og.parts_penalties("A", 103.9, NOW_IST) and "extended_or" in og.parts_penalties("A", 103.9, NOW_IST)
    assert not str(og.reject_reason("A", 103.9, NOW_IST)).startswith("OPENING_GATE:NEAR_DAY_HIGH")
    gp.set_cache({(N, "LONG"): "HARD"})                                                             # the policy tightens only this part
    assert str(og.reject_reason("A", 103.9, NOW_IST)).startswith("OPENING_GATE:NEAR_DAY_HIGH")
    assert "near_day_high" not in og.parts_penalties("A", 103.9, NOW_IST) and "extended_or" in og.parts_penalties("A", 103.9, NOW_IST)
    gp.clear_cache()
    monkeypatch.setattr(config, "OPENING_PARTS_MODE", "veto")                                       # config says refuse everything ...
    assert og.parts_penalties("A", 103.9, NOW_IST) == {}
    assert str(og.reject_reason("A", 103.9, NOW_IST)).startswith("OPENING_GATE:NEAR_DAY_HIGH")
    gp.set_cache({(N, "LONG"): "SOFT"})                                                             # ... and the policy relaxes just the day-high part, at the flat SOFT size
    assert set(og.parts_penalties("A", 103.9, NOW_IST)) == {"near_day_high"} and og.parts_penalties("A", 103.9, NOW_IST)["near_day_high"]["penalty"] == 1.0
    assert not str(og.reject_reason("A", 103.9, NOW_IST) or "").startswith("OPENING_GATE:NEAR_DAY_HIGH")


def test_quality_gate_news_check_follows_the_state(monkeypatch):
    from screening.quality_gate import QualitySignal
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True)
    sig = QualitySignal(symbol="X", event_unchecked=True, event_unverified=True)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", True)
    assert sig.passes()[0]                                                                          # config: half size (batch 4)
    gp.set_cache({(E, "LONG"): "HARD"})
    ok, why = sig.passes()
    assert not ok and why.startswith("EVENT_CHECK_UNAVAILABLE")                                     # the policy refuses again
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE", False)
    gp.set_cache({(E, "LONG"): "SOFT"})
    assert sig.passes()[0]
    neg = QualitySignal(symbol="X", event_unchecked=True, event_unverified=True, negative_news="NEWS_NEGATIVE:regulatory action")
    monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
    assert not neg.passes()[0]                                                                      # SOFT never lets a known negative through


# ------------------------------------------------------------------ entry wiring

def test_soft_row_helper_writes_a_soft_decision_through_the_batch_writer(monkeypatch):
    from orders import candidate_log_writer as clw, entry
    seen = []
    monkeypatch.setattr(clw, "enabled", lambda: True)
    monkeypatch.setattr(clw, "submit", lambda row: seen.append(row) or True)
    cand = types.SimpleNamespace(symbol="ABC", window_label="5m", pct_change=2.1, composite_score=7.0)
    entry._log_policy_soft(cand, "OPENING_GATE:GAP_DOWN:-1.50% < -1.00% (size penalty 1.0, SOFT)")
    assert seen and seen[0]["decision"] == "SOFT" and seen[0]["symbol"] == "ABC" and job.gate_key(seen[0]["reason"], "SOFT") == G
    monkeypatch.setattr(config, "GATE_POLICY_LOG_SOFT_ROWS", False)
    entry._log_policy_soft(cand, "x")
    assert len(seen) == 1
    monkeypatch.setattr(config, "GATE_POLICY_LOG_SOFT_ROWS", True)
    monkeypatch.setattr(clw, "enabled", lambda: False)
    entry._log_policy_soft(cand, "x")
    assert len(seen) == 1                                                                           # no writer = no row, and no error


def test_entry_and_main_are_wired():
    e = open("orders/entry.py").read()
    assert "gate_policy.event_half_size()" in e and "QUALITY_GATE_EVENT_UNCHECKED_HALF_SIZE" not in e
    i_parts = e.index("opening_gate.parts_penalties(")
    assert e.index('_k == "gap_down" and _pos_event') > i_parts and e.index("_log_policy_soft(candidate, f\"{_v['why']}") > i_parts
    assert e.index("_log_policy_soft(candidate, f\"OPENING_GATE:BELOW_OPEN:") < e.index("OPENING_GATE:BELOW_OPEN_TOO_FAR")
    m = open("main.py").read()
    assert "_gpj.tick_due()" in m and m.index("_gpj.tick_due()") < m.index("premarket plan failed") and '@app.get("/gate-policy")' in m
    q = open("screening/quality_gate.py").read()
    assert "gate_policy.event_half_size()" in q
