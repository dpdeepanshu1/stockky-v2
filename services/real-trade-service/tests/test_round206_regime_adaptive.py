"""round 206: the regime constants adapt to the index over 7 horizons, volatility and recent closed trades - inside hard bands."""
import math

import pytest

import config
import regime_adaptive as ra

BASE = {"ENTRY_MIN_REWARD_RISK": 2.0, "CANDIDATE_MIN_CONVICTION": 55.0, "CANDIDATE_DOWNTREND_6M_PCT": -10.0, "CANDIDATE_MIN_BULLISH_TF": 4}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("REGIME_ADAPTIVE_MODE", raising=False)
    ra._reset_for_tests()
    yield
    ra._reset_for_tests()


def _closes(daily_pct, n=300, start=100.0, wiggle=0.6):
    """n closes drifting `daily_pct` per day with a small alternating swing."""
    out, p = [], start
    for i in range(n):
        p *= 1.0 + (daily_pct + (wiggle if i % 2 else -wiggle)) / 100.0
        out.append(p)
    return out


def test_horizon_returns_use_session_counts_and_none_when_short():
    c = [100.0] * 10 + [110.0]
    r = ra.horizon_returns(c)
    assert r["1d"] == pytest.approx(10.0) and r["7d"] == pytest.approx(10.0)
    assert r["1m"] is None and r["1y"] is None


def test_picture_needs_70_sessions():
    assert ra.market_picture(_closes(0.1, n=50)) is None
    assert ra.market_picture(_closes(0.1, n=80)) is not None


def test_uptrend_is_positive_tone_downtrend_negative():
    up = ra.market_picture(_closes(0.25))
    dn = ra.market_picture(_closes(-0.25))
    assert up["tone"] > 0.3 and dn["tone"] < -0.3


def test_recent_volatility_spike_raises_stress():
    calm = _closes(0.0, wiggle=0.3)
    loud = calm[:-20] + _closes(0.0, n=20, start=calm[-21], wiggle=2.5)
    assert ra.market_picture(calm)["stress"] < 0.1
    assert ra.market_picture(loud)["stress"] > 0.6


def test_feedback_needs_min_trades_and_scales_with_win_rate():
    assert ra.result_feedback([1.0] * 5, 15) == (0.0, 5, 1.0)
    fb, n, wr = ra.result_feedback([1.0] * 12 + [-1.0] * 8, 15)       # 60 % wins
    assert n == 20 and wr == pytest.approx(0.6) and fb == pytest.approx(0.6)
    fb, _, _ = ra.result_feedback([1.0] * 4 + [-1.0] * 16, 15)         # 20 % wins -> clipped
    assert fb == -1.0
    assert ra.result_feedback([0.0, 0.0, 1.0], 1)[1] == 1               # break-even trades are not counted


def test_neutral_inputs_return_the_owners_values():
    v = ra.compute_values(BASE, 0.0, 0.0, 0.0, 0.0)
    assert v["ENTRY_MIN_REWARD_RISK"] == 2.0 and v["CANDIDATE_MIN_CONVICTION"] == 55.0 and v["CANDIDATE_MIN_BULLISH_TF"] == 4
    assert v["score_delta"] == 0


def test_weak_stressed_poor_results_tighten_within_bands():
    v = ra.compute_values(BASE, -1.0, 1.0, -1.0, -20.0)
    assert v["ENTRY_MIN_REWARD_RISK"] == 2.5                      # +0.5 max, never more
    assert v["CANDIDATE_MIN_CONVICTION"] == 70.0                  # base + 15 max
    assert v["CANDIDATE_MIN_BULLISH_TF"] == 5
    assert v["score_delta"] == 10
    assert -20.0 <= v["CANDIDATE_DOWNTREND_6M_PCT"] <= -5.0


def test_strong_calm_good_results_loosen_but_never_the_rr_floor():
    v = ra.compute_values(BASE, 1.0, 0.0, 1.0, 25.0)
    assert v["ENTRY_MIN_REWARD_RISK"] == 2.0                      # the owner's 1:2 rule is a floor
    assert v["CANDIDATE_MIN_CONVICTION"] == 52.0                  # base - 3
    assert v["CANDIDATE_MIN_BULLISH_TF"] == 3
    assert v["score_delta"] == -6                                 # -4 tone, -3 feedback = -7, clipped at -6
    assert v["CANDIDATE_DOWNTREND_6M_PCT"] == -5.0                # 0.5 * -10 + 0.5 * (25 - 5) = +5, clipped to the -5 band edge


def test_downtrend_threshold_follows_the_index_six_month_return():
    falling = ra.compute_values(BASE, 0.0, 0.0, 0.0, -14.0)["CANDIDATE_DOWNTREND_6M_PCT"]   # index -14 % -> a stock may trail further
    rising = ra.compute_values(BASE, 0.0, 0.0, 0.0, 10.0)["CANDIDATE_DOWNTREND_6M_PCT"]
    assert falling < -10.0 < rising and rising <= -5.0 and falling >= -20.0


def test_off_and_no_snapshot_return_static_values(monkeypatch):
    assert ra.effective("CANDIDATE_MIN_CONVICTION", 55.0) == 55.0 and ra.score_delta() == 0
    ra.refresh(fetch=lambda: _closes(-0.3))
    assert ra.effective("CANDIDATE_MIN_CONVICTION", 55.0) > 55.0
    monkeypatch.setenv("REGIME_ADAPTIVE_MODE", "off")
    assert ra.effective("CANDIDATE_MIN_CONVICTION", 55.0) == 55.0 and ra.score_delta() == 0 and not ra.is_managed("ENTRY_MIN_REWARD_RISK")


def test_refresh_failure_keeps_static_and_records_the_error():
    out = ra.refresh(fetch=lambda: (_ for _ in ()).throw(RuntimeError("history down")))
    assert "error" in out and ra.effective("ENTRY_MIN_REWARD_RISK", 2.0) == 2.0
    assert "history down" in ra.status()["last_error"]
    out = ra.refresh(fetch=lambda: _closes(0.1, n=30))
    assert "error" in out and ra.score_delta() == 0


def test_stale_snapshot_falls_back(monkeypatch):
    ra.refresh(fetch=lambda: _closes(-0.3))
    assert ra.is_managed("CANDIDATE_MIN_CONVICTION")
    ra._state["ts"] -= 40 * 3600
    assert ra.effective("CANDIDATE_MIN_CONVICTION", 55.0) == 55.0 and not ra.is_managed("CANDIDATE_MIN_CONVICTION")


def test_refresh_uses_closed_trade_results():
    class _DB:
        def close(self):
            pass
    ra.refresh(session_factory=lambda: _DB(), fetch=lambda: _closes(0.0, wiggle=0.3), pnls=lambda db, days: [-5.0] * 20)
    poor = ra.effective("CANDIDATE_MIN_CONVICTION", 55.0)
    ra._reset_for_tests()
    ra.refresh(session_factory=lambda: _DB(), fetch=lambda: _closes(0.0, wiggle=0.3), pnls=lambda db, days: [5.0] * 20)
    good = ra.effective("CANDIDATE_MIN_CONVICTION", 55.0)
    assert poor > good
    assert ra.status()["feedback"]["trades"] == 20


def test_status_lists_static_and_effective_and_marks_the_dead_constant():
    ra.refresh(fetch=lambda: _closes(-0.3))
    st = ra.status()
    assert st["active"] and set(st["static"]) == set(BASE) and "ENTRY_REGIME_MIN_SCORE_shift" in st["effective"]
    assert "CANDIDATE_OVEREXTENDED_52W_TOP_PCT" in st["not_adapted"]
    assert st["picture"]["returns"].keys() >= {"1d", "7d", "1m", "2m", "3m", "6m", "1y"}


def test_score_threshold_is_shifted_by_the_delta(monkeypatch):
    import adaptive_thresholds as at
    monkeypatch.setattr(at, "_history_regime_threshold", lambda db: (25, "static"))
    assert at.adaptive_regime_threshold(None) == (25, "static")
    monkeypatch.setattr(ra, "score_delta", lambda: 7)
    assert at.adaptive_regime_threshold(None) == (32, "static+7tone")
    monkeypatch.setattr(ra, "score_delta", lambda: -6)
    assert at.adaptive_regime_threshold(None)[0] == 20


def test_stale_warning_is_muted_only_for_managed_constants(monkeypatch):
    import adaptive_thresholds as at
    monkeypatch.setattr(at, "_days_since", lambda d: 99)
    names = {s["constant"] for s in at.check_threshold_staleness()}
    assert "CANDIDATE_MIN_CONVICTION" in names
    ra.refresh(fetch=lambda: _closes(-0.3))
    names = {s["constant"] for s in at.check_threshold_staleness()}
    assert "CANDIDATE_MIN_CONVICTION" not in names
    assert "CANDIDATE_OVEREXTENDED_52W_TOP_PCT" not in names          # retired: nothing reads it, so it is never nagged about


def test_raised_floor_moves_the_target_so_setups_still_pass(monkeypatch):
    from entry_engine import entry
    monkeypatch.setattr(entry, "_range_adjusted_stop_target_core", lambda s, t, tick, ep: (s, t))
    assert entry._range_adjusted_stop_target(1.5, 3.0, None, 100.0) == (1.5, 3.0)           # floor == owner's value: untouched
    monkeypatch.setattr(entry, "_rr_floor", lambda base=None: 2.25)
    s, t = entry._range_adjusted_stop_target(1.5, 3.0, None, 100.0)
    assert s == 1.5 and t / s >= 2.25 and math.isclose(t, 3.38, abs_tol=0.01)


def test_candidate_engine_reads_the_adapted_values(monkeypatch):
    from candidate_engine import candidates as cand
    monkeypatch.setattr(config, "MARKET_STATE_CONVICTION_ADD", 0.0, raising=False)
    assert cand._min_conviction() == cand.MIN_CONVICTION                                   # nothing adapted: the owner's value
    monkeypatch.setattr(ra, "effective", lambda name, base: {"CANDIDATE_MIN_CONVICTION": 63.0}.get(name, base))
    assert cand._min_conviction() == 63.0
    assert cand._adapted("CANDIDATE_MIN_BULLISH_TF", 4) == 4
