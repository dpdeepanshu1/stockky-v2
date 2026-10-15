"""group374: all-decision structure observations (training/structure_obs.py). Pure core + DB glue run against a tiny in-memory fake session, so the
file needs neither sqlalchemy nor fastapi. Plain asserts; runs with pytest or `python3 tests/test_group374_structure_obs.py`."""
from __future__ import annotations

import ast
import os
import sys
from datetime import date, datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import structure_obs as so  # noqa: E402


def _bars(start="2026-10-01", n=30, o=100.0, step=1.0):
    """ASCII: close drifts up `step` per session, high = close+0.5, low = open-0.5, sessions on consecutive ISO dates (weekends not modelled)."""
    d0 = date.fromisoformat(start)
    out = []
    for i in range(n):
        px = o + i * step
        out.append({"date": date.fromordinal(d0.toordinal() + i).isoformat(), "open": px, "high": px + step + 0.5, "low": px - 0.5, "close": px + step})
    return out


OBS = {"symbol": "abc", "obs_date": "2026-10-01", "asof_date": "2026-09-30", "engine_version": "1.0.0", "policy_mode": "full", "price": 100.0, "atr": 2.0,
       "final_decision": "buy now", "legacy_decision": "BUY NOW", "policy_applied": False, "rules": ["P2_CHASE", "", 5], "tqs": 71,
       "features": {"quality": "ok", "trend": "bullish", "rr": 1.9, "secret": "x", "chase_atr": float("nan")},
       "levels": {"stop": 96.0, "target": 112.0, "rr": 1.9}}


# ── sanitize ───────────────────────────────────────────────────────────────
def test_sanitize_normalises_and_allow_lists():
    o = so.sanitize(OBS)
    assert o["symbol"] == "ABC" and o["final_decision"] == "BUY NOW" and o["policy_applied"] == 0 and o["rules"] == ["P2_CHASE"]
    assert o["features"] == {"rr": 1.9, "quality": "ok", "trend": "bullish"} and "secret" not in o["features"]
    assert o["levels"] == {"stop": 96.0, "target": 112.0, "rr": 1.9} and o["tqs"] == 71.0


def test_sanitize_rejects_unusable_input():
    for bad in (None, [], "x", {}, {**OBS, "symbol": ""}, {**OBS, "obs_date": "nope"}, {**OBS, "final_decision": None}):
        assert so.sanitize(bad) is None


def test_applied_is_strictly_true():
    assert so.sanitize({**OBS, "policy_applied": True})["policy_applied"] == 1
    assert so.sanitize({**OBS, "policy_applied": "yes"})["policy_applied"] == 0


def test_bad_numbers_become_none_never_zero():
    o = so.sanitize({**OBS, "price": "x", "atr": -1, "levels": {"stop": 0, "target": float("inf")}})
    assert o["price"] is None and o["atr"] is None and o["levels"]["stop"] is None and o["levels"]["target"] is None


# ── evaluate ───────────────────────────────────────────────────────────────
def test_entry_is_next_session_open_with_slippage_and_horizons():
    r = so.evaluate(OBS, _bars(), slip_pct=0.0)   # obs 10-01 -> entry bar 10-02 (open 101)
    assert r["entry_date"] == "2026-10-02" and r["entry_price"] == 101.0
    assert r["ret_1"] == round((102 / 101 - 1) * 100, 4) and r["ret_5"] == round((106 / 101 - 1) * 100, 4)
    assert r["ret_10"] is not None and r["ret_20"] == round((121 / 101 - 1) * 100, 4)
    assert r["eval_status"] == "done" and r["bars_seen"] == 20
    assert abs(so.evaluate(OBS, _bars(), slip_pct=1.0)["entry_price"] - 102.01) < 1e-6


def test_no_bar_after_the_observation_day_is_pending():
    r = so.evaluate({**OBS, "obs_date": "2026-10-30"}, _bars(n=30, start="2026-10-01"))
    assert r["eval_status"] == "pending" and "entry_price" not in r
    assert so.evaluate(OBS, [])["eval_status"] == "pending"
    assert so.evaluate(None, None)["eval_status"] == "pending"


def test_partial_window_fills_only_reachable_horizons_and_leaves_hit_undecided():
    r = so.evaluate(OBS, _bars(n=7), slip_pct=0.0)   # entry bar + 5 more
    assert r["eval_status"] == "partial" and r["ret_5"] is not None and "ret_10" not in r and "ret_20" not in r
    flat = _bars(n=7, step=0.0)
    assert so.evaluate(OBS, flat, slip_pct=0.0)["hit_5pct_20"] is None      # no hit yet, window incomplete -> unknown, not 0
    assert so.evaluate(OBS, _bars(n=30, step=0.0), slip_pct=0.0)["hit_5pct_20"] == 0   # complete window, never +5% -> 0


def test_hit_5pct_within_20_when_a_high_reaches_it():
    assert so.evaluate(OBS, _bars(), slip_pct=0.0)["hit_5pct_20"] == 1


def test_target_before_stop_first_touch_and_same_bar_stop_wins():
    up = so.evaluate(OBS, _bars(), slip_pct=0.0)   # highs climb past 112 on day ~11, lows stay above 96
    assert up["target_before_stop"] == 1 and up["days_to_target"] is not None and up["days_to_target"] > 1
    bars = _bars(step=0.0)
    bars[3].update(high=113.0, low=95.0)           # one bar touches target AND stop
    assert so.evaluate(OBS, bars, slip_pct=0.0)["target_before_stop"] == 0
    bars = _bars(step=0.0)
    bars[3].update(high=113.0)
    r = so.evaluate(OBS, bars, slip_pct=0.0)
    assert r["target_before_stop"] == 1 and r["days_to_target"] == 3
    assert so.evaluate({**OBS, "levels": {}}, _bars(), slip_pct=0.0)["target_before_stop"] is None


def test_mfe_mae_and_realised_chase():
    r = so.evaluate(OBS, _bars(step=0.0), slip_pct=0.0)   # flat 100, high 100.5, low 99.5
    assert r["mfe_20"] == 0.5 and r["mae_20"] == -0.5 and r["entry_chase_atr"] == 0.0
    gap = _bars(step=0.0)
    gap[1]["open"] = 104.0
    gap[1]["high"], gap[1]["low"], gap[1]["close"] = 104.5, 103.5, 104.0
    assert so.evaluate(OBS, gap, slip_pct=0.0)["entry_chase_atr"] == 2.0   # (104-100)/2


def test_benchmark_alignment_and_missing_benchmark_is_none():
    b = _bars(o=200.0, step=2.0)
    r = so.evaluate(OBS, _bars(), bench=b, slip_pct=0.0)
    assert r["nifty_ret_5"] == round((b[5]["close"] / b[1]["open"] - 1) * 100, 4)
    assert "nifty_ret_5" not in so.evaluate(OBS, _bars(), bench=None, slip_pct=0.0)
    assert "nifty_ret_5" not in so.evaluate(OBS, _bars(), bench=[{"date": "2020-01-01", "open": 1, "high": 1, "low": 1, "close": 1}], slip_pct=0.0)


def test_bad_bars_are_dropped_and_dates_may_be_date_objects():
    bars = _bars()
    bars[2]["close"] = None
    bars.append({"date": "garbage", "open": 1, "high": 1, "low": 1, "close": 1})
    bars.append(dict(bars[0]))   # duplicate date
    for b in bars:
        if isinstance(b["date"], str) and b["date"][:2] == "20":
            b["date"] = date.fromisoformat(b["date"])
    r = so.evaluate(OBS, bars, slip_pct=0.0)
    assert r["entry_date"] == "2026-10-02" and r["bars_seen"] == 20     # 30 bars - 1 bad close: the window skips the bad session
    short = _bars(n=6)
    short[2]["close"] = None
    assert so.evaluate(OBS, short, slip_pct=0.0)["bars_seen"] == 4       # 5 usable after the obs day - 1 dropped


def test_slippage_env_default_and_bounds():
    os.environ.pop("STRUCTURE_OBS_SLIPPAGE_PCT", None)
    assert so.slippage_pct() == 0.1
    os.environ["STRUCTURE_OBS_SLIPPAGE_PCT"] = "0.25"
    assert so.slippage_pct() == 0.25
    for bad in ("x", "-1", "9"):
        os.environ["STRUCTURE_OBS_SLIPPAGE_PCT"] = bad
        assert so.slippage_pct() == 0.1
    os.environ.pop("STRUCTURE_OBS_SLIPPAGE_PCT", None)


# ── report ─────────────────────────────────────────────────────────────────
def _row(day, final, legacy=None, **kw):
    d = {"obs_date": day, "final_decision": final, "legacy_decision": legacy or final, "eval_status": "done", "ret_5": 1.0, "ret_20": 3.0,
         "hit_5pct_20": 1, "target_before_stop": 1, "mfe_20": 6.0, "mae_20": -2.0, "nifty_ret_20": 1.0, "entry_chase_atr": 0.3}
    d.update(kw)
    return d


def test_summarize_groups_demoted_and_no_signal_days():
    rows = [_row("2026-10-01", "BUY NOW"), _row("2026-10-01", "WAIT", "BUY NOW", hit_5pct_20=0, target_before_stop=0, ret_20=-2.0),
            _row("2026-10-02", "WAIT"), _row("2026-10-03", "DO NOT BUY")]
    s = so.summarize(rows, min_n=1)
    assert s["total"] == 4 and s["days_observed"] == 3 and s["no_buy_now_days"] == 2
    assert s["passed_buy_now"]["n"] == 1 and s["policy_demoted"]["n"] == 1
    assert s["passed_buy_now"]["hit_5pct_within_20d_pct"] == 100.0 and s["policy_demoted"]["hit_5pct_within_20d_pct"] == 0.0
    assert s["passed_buy_now"]["avg_rel_to_nifty_20"] == 2.0
    assert s["edge"]["hit_5pct_passed_minus_demoted_pts"] == 100.0 and s["edge"]["insufficient_sample"] is False
    assert set(s["by_decision"]) == {"BUY NOW", "DO NOT BUY", "WAIT"}


def test_small_samples_never_claim_an_edge():
    s = so.summarize([_row("2026-10-01", "BUY NOW"), _row("2026-10-01", "WAIT", "BUY NOW")], min_n=30)
    assert s["edge"]["insufficient_sample"] is True and s["edge"]["hit_5pct_passed_minus_demoted_pts"] is None
    e = so.summarize([], min_n=30)
    assert e["total"] == 0 and e["edge"]["insufficient_sample"] is True
    assert so.summarize(None)["total"] == 0


def test_undecided_rows_do_not_count_as_losses():
    s = so.summarize([_row("2026-10-01", "BUY NOW", eval_status="partial", hit_5pct_20=None, target_before_stop=None, ret_20=None)], min_n=1)
    g = s["passed_buy_now"]
    assert g["hit_5pct_within_20d_pct"] is None and g["target_before_stop_pct"] is None and g["avg_ret_20"] is None and g["n_done_20d"] == 0


# ── DB glue against a tiny fake session ──────────────────────────────────────
class _Col:
    def __init__(self, name):
        self.name = name

    def in_(self, vals):
        return ("in", self.name, tuple(vals))

    def desc(self):
        return ("desc", self.name)


class _Q:
    def __init__(self, rows):
        self.rows, self.conds = rows, []

    def filter_by(self, **kw):
        self.rows = [r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())]
        return self

    def filter(self, cond):
        _, name, vals = cond
        self.rows = [r for r in self.rows if getattr(r, name) in vals]
        return self

    def order_by(self, _):
        self.rows = sorted(self.rows, key=lambda r: -r.id)
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None


class _DB:
    def __init__(self):
        self.rows, self.commits = [], 0

    def query(self, M):
        return _Q(list(self.rows))

    def add(self, r):
        r.id = len(self.rows) + 1
        self.rows.append(r)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


class M:
    id = _Col("id")
    eval_status = _Col("eval_status")

    def __init__(self, **kw):
        self.eval_status, self.eval_attempts, self.id = "pending", 0, 0
        for k, v in kw.items():
            setattr(self, k, v)


def test_upsert_is_idempotent_and_updates_only_pending_rows():
    db = _DB()
    r1 = so.upsert_batch(db, M, [OBS, {"bad": 1}])
    assert r1 == {"received": 2, "inserted": 1, "updated": 0, "kept": 0, "invalid": 1} and len(db.rows) == 1
    r2 = so.upsert_batch(db, M, [{**OBS, "final_decision": "WAIT", "legacy_decision": "BUY NOW", "policy_applied": True}])
    assert r2["updated"] == 1 and len(db.rows) == 1 and db.rows[0].final_decision == "WAIT" and db.rows[0].policy_applied == 1
    db.rows[0].eval_status = "partial"
    r3 = so.upsert_batch(db, M, [{**OBS, "final_decision": "SELL"}])
    assert r3["kept"] == 1 and db.rows[0].final_decision == "WAIT"
    assert so.upsert_batch(db, M, "junk")["received"] == 0
    big = [{**OBS, "symbol": f"S{i}"} for i in range(150)]
    assert so.upsert_batch(_DB(), M, big)["received"] == so.MAX_BATCH


def test_evaluate_pending_scores_rows_gives_up_and_isolates_errors():
    db = _DB()
    so.upsert_batch(db, M, [OBS, {**OBS, "symbol": "NODATA", "obs_date": "2026-01-01"}, {**OBS, "symbol": "BOOM"}])
    calls = []

    def fetch(sym):
        calls.append(sym)
        if sym == "BOOM":
            raise RuntimeError("x")
        return [] if sym == "NODATA" else _bars()

    res = so.evaluate_pending(db, M, fetch, lambda: _bars(o=200.0, step=2.0), max_batch=10, today=date(2026, 10, 20))
    by = {r.symbol: r for r in db.rows}
    assert res["considered"] == 3 and res["evaluated"] == 1 and res["done"] == 1 and res["no_data"] == 1 and res["errors"] == 1
    assert by["ABC"].eval_status == "done" and by["ABC"].ret_20 is not None and by["ABC"].nifty_ret_5 is not None and by["ABC"].evaluated_at
    assert by["NODATA"].eval_status == "no_data" and by["BOOM"].eval_status == "pending"
    res2 = so.evaluate_pending(db, M, fetch, None, max_batch=10, today=date(2026, 10, 20))   # done / no_data rows are not re-scored
    assert res2["considered"] == 1


def test_report_filters_by_window():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "obs_date": "2026-10-10"}, {**OBS, "symbol": "OLD", "obs_date": "2025-01-01"}])
    out = so.report(db, M, days=30, min_n=1, today=date(2026, 10, 15))
    assert out["total"] == 1 and out["window_days"] == 30 and out["since"] == "2026-09-15"


# ── wiring (source inspection: app.py / models.py need sqlalchemy + fastapi) ──────────────────────────────────────────────────────────
def _src(name):
    return open(os.path.join(HERE, name), encoding="utf-8").read()


def test_model_routes_and_oracle_identity_are_wired():
    ast.parse(_src("models.py")), ast.parse(_src("app.py"))
    m, a = _src("models.py"), _src("app.py")
    assert "class StructureObservation(Base):" in m and '"decision_structure_observations",' in m and "uq_decision_struct_obs_key" in m
    for route in ('"/api/structure/observations"', '"/api/evaluate/structure"', '"/api/structure/report"'):
        assert route in a
    assert "import structure_obs as _so" in a and "StructureObservation" in a.split("from models import", 1)[1].splitlines()[0]
    for col in so._OUTCOME_COLS + so._INPUT_COLS:
        assert f"    {col} = Column(" in m, col


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
