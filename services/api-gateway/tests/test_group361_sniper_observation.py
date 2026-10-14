"""tests/test_group361_sniper_observation.py -- Buy Sniper outcome tracking (WS-6 for the sniper, group 361).

No network, no real kv store, no real clock. main.py cannot be imported without fastapi, so its wiring is checked as source / AST.
Run from services/api-gateway:  python3 -m pytest tests/test_group361_sniper_observation.py -v
"""
from __future__ import annotations

import ast
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import buy_sniper  # noqa: E402
import sniper_observation as so  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 10, 13, 11, 0, tzinfo=IST).timestamp()     # Tue 13 Oct 2026, 11:00 IST
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("SNIPER_OBS_") or k.startswith("SNIPER_STRUCTURE") or k.startswith("STRUCTURE_"):
            monkeypatch.delenv(k, raising=False)
    so.reset_stats()
    yield
    so.reset_stats()


class FakeKv:
    def __init__(self):
        self.d, self.ttls, self.writes = {}, {}, 0
        self.store = so.KvStore(self.get, self.set)

    def get(self, k):
        v = self.d.get(k)
        return json.loads(json.dumps(v)) if v is not None else None   # like a real durable store: a copy, not the live object

    def set(self, k, v, ttl=None):
        self.d[k] = json.loads(json.dumps(v))
        self.ttls[k] = ttl
        self.writes += 1


def summary(**kw):
    base = {"ms_quality": "ok", "ms_trend": "bullish", "ms_asof": "2026-10-13", "ms_price": 100.0, "ms_atr": 2.0, "ms_stop": 95.0,
            "ms_target": 112.0, "ms_rr": 2.4, "ms_chase_atr": 0.4, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0, "ms_rr_flags": [],
            "ms_bos_dir": None, "ms_bos_bars": None, "ms_bos_failed": None}
    base.update(kw)
    return base


def scan_row(sym, conv=90, px=100.0, **sm):
    return {"symbol": sym, "decision": "BUY", "conviction": conv, "price": px, "technical_score": 80, "fundamental_score": 70,
            "structure_summary": summary(**sm)}


def sniper(rows):
    return buy_sniper.suggestions_from_scan_payload({"stocks": rows, "target_count": 4})


# ------------------------------------------------------------------ build_rows (with the REAL sniper)
class TestBuildRows:
    def test_enforce_mode_passed_and_rejected(self):
        rows = [scan_row("GOOD"), scan_row("CHASE", ms_chase_atr=4.0)]
        payload = {"stocks": rows}
        res = sniper(rows)
        assert [c["symbol"] for c in res["suggestions"]] == ["GOOD"] and [r["symbol"] for r in res["structure_rejected"]] == ["CHASE"]
        out = {r["symbol"]: r for r in so.build_rows(payload, res, T0)}
        assert out["GOOD"]["verdict"] == "passed" and out["CHASE"]["verdict"] == "rejected_enforced"
        assert out["CHASE"]["price"] == 100.0 and out["CHASE"]["stop"] == 95.0 and out["CHASE"]["target"] == 112.0
        assert out["GOOD"]["source"] == "sniper" and out["GOOD"]["obs_day"] == "2026-10-13" and out["GOOD"]["gate_mode"] == "enforce"
        assert "chase" in json.loads(out["CHASE"]["reasons_json"])[0]
        assert json.loads(out["GOOD"]["summary_json"])["ms_trend"] == "bullish"

    def test_shadow_mode_surfaced_but_flagged(self, monkeypatch):
        monkeypatch.setenv("SNIPER_STRUCTURE_MODE", "shadow")
        rows = [scan_row("CHASE", ms_chase_atr=4.0)]
        res = sniper(rows)
        assert len(res["suggestions"]) == 1 and res["structure_rejected"] == []
        out = so.build_rows({"stocks": rows}, res, T0)
        assert [r["verdict"] for r in out] == ["rejected_shadow"] and out[0]["gate_mode"] == "shadow"

    def test_gate_off_stores_nothing(self, monkeypatch):
        monkeypatch.setenv("SNIPER_STRUCTURE_MODE", "off")
        rows = [scan_row("GOOD")]
        assert so.build_rows({"stocks": rows}, sniper(rows), T0) == []

    def test_no_opinion_card_is_skipped_and_counted(self):
        r = scan_row("NOOP")
        r["structure_summary"] = {"ms_quality": "unavailable"}
        res = sniper([r])
        assert so.build_rows({"stocks": [r]}, res, T0) == [] and so.stats()["skipped_no_opinion"] == 1

    def test_one_row_per_symbol(self):
        rows = [scan_row("GOOD")]
        res = sniper(rows)
        res["suggestions"] = res["suggestions"] * 2
        assert len(so.build_rows({"stocks": rows}, res, T0)) == 1

    def test_rejected_without_a_scan_row_is_skipped(self):
        res = {"structure_mode": "enforce", "suggestions": [], "structure_rejected": [{"symbol": "GHOST", "reasons": ["x"]}]}
        assert so.build_rows({"stocks": []}, res, T0) == [] and so.stats()["skipped_bad"] == 1

    def test_rejected_price_prefers_explicit_price(self):
        rows = [scan_row("CHASE", px=100.0, ms_chase_atr=4.0)]
        res = sniper(rows)
        res["structure_rejected"][0]["price"] = 101.5
        assert so.build_rows({"stocks": rows}, res, T0)[0]["price"] == 101.5

    @pytest.mark.parametrize("payload,result", [(None, None), ({}, {}), ("x", 5), ([1, 2], {"suggestions": "bad"}),
                                                  ({"stocks": [None, 3]}, {"suggestions": [None, {"symbol": "A"}], "structure_rejected": [7]})])
    def test_garbage_never_raises(self, payload, result):
        assert so.build_rows(payload, result, T0) == []


# ------------------------------------------------------------------ record
class TestRecord:
    def pair(self, extra=()):
        rows = [scan_row("GOOD"), scan_row("CHASE", ms_chase_atr=4.0)] + list(extra)
        return {"stocks": rows}, sniper(rows)

    def test_writes_shard_and_index(self):
        kv = FakeKv()
        p, r = self.pair()
        assert so.record(p, r, kv.store, T0) == 2
        assert set(kv.d["stockky:sniper_obs:day:2026-10-13"]["rows"]) == {"GOOD", "CHASE"}
        assert kv.d[so.INDEX_KEY]["days"]["2026-10-13"] == {"n": 2, "open": 2}
        assert kv.ttls[so.INDEX_KEY] == 120 * 86400 and so.stats()["recorded"] == 2

    def test_first_verdict_of_the_day_wins(self):
        kv = FakeKv()
        p, r = self.pair()
        so.record(p, r, kv.store, T0)
        first = kv.d["stockky:sniper_obs:day:2026-10-13"]["rows"]["CHASE"]["verdict"]
        # a later call the same day where CHASE now passes must not overwrite the first record
        p2 = {"stocks": [scan_row("CHASE")]}
        assert so.record(p2, sniper(p2["stocks"]), kv.store, T0 + 3600) == 0
        assert kv.d["stockky:sniper_obs:day:2026-10-13"]["rows"]["CHASE"]["verdict"] == first == "rejected_enforced"
        assert so.stats()["skipped_dup"] == 1

    def test_new_symbol_same_day_is_added(self):
        kv = FakeKv()
        p, r = self.pair()
        so.record(p, r, kv.store, T0)
        p2 = {"stocks": [scan_row("NEWB")]}
        assert so.record(p2, sniper(p2["stocks"]), kv.store, T0 + 60) == 1
        assert kv.d[so.INDEX_KEY]["days"]["2026-10-13"]["n"] == 3

    def test_day_cap(self, monkeypatch):
        monkeypatch.setenv("SNIPER_OBS_MAX_PER_DAY", "1")
        kv = FakeKv()
        p, r = self.pair()
        assert so.record(p, r, kv.store, T0) == 1 and so.stats()["skipped_cap"] == 1

    def test_disabled_and_nothing_to_store_write_nothing(self, monkeypatch):
        kv = FakeKv()
        p, r = self.pair()
        monkeypatch.setenv("SNIPER_OBS_ENABLED", "0")
        assert so.record(p, r, kv.store, T0) == 0 and kv.writes == 0 and so.stats()["enabled"] is False
        monkeypatch.setenv("SNIPER_OBS_ENABLED", "1")
        assert so.record({"stocks": []}, {"suggestions": []}, kv.store, T0) == 0 and kv.writes == 0

    def test_kv_failure_never_raises(self):
        class Boom(FakeKv):
            def set(self, k, v, ttl=None):
                raise RuntimeError("neon down")
        kv = Boom()
        p, r = self.pair()
        assert so.record(p, r, kv.store, T0) == 0 and so.stats()["errors"] == 1 and "neon down" in so.stats()["last_error"]

    def test_index_retention_prunes_old_days(self, monkeypatch):
        monkeypatch.setenv("SNIPER_OBS_RETENTION_DAYS", "7")
        kv = FakeKv()
        kv.d[so.INDEX_KEY] = {"days": {f"2026-09-{d:02d}": {"n": 1, "open": 0} for d in range(1, 21)}}
        p, r = self.pair()
        so.record(p, r, kv.store, T0)
        days = sorted(kv.d[so.INDEX_KEY]["days"])
        assert len(days) == 7 and days[-1] == "2026-10-13"


# ------------------------------------------------------------------ evaluate
def bars_from(start_ts, closes, high_pad=0.5, low_pad=0.5):
    out, ts = [], start_ts
    for c in closes:
        ts += DAY
        out.append((ts, c, c + high_pad, c - low_pad, c, 1000.0))
    return out


class TestEvaluate:
    def seeded(self):
        kv = FakeKv()
        rows = [scan_row("GOOD"), scan_row("CHASE", ms_chase_atr=4.0)]
        so.record({"stocks": rows}, sniper(rows), kv.store, T0)
        return kv

    def test_same_day_is_not_due(self):
        kv = self.seeded()
        res = so.evaluate_pending(kv.store, T0 + 3600, fetch=lambda s, lb: pytest.fail("must not fetch"))
        assert res["pending"] == 2 and res["symbols"] == 0 and res["updated"] == 0

    def test_fills_outcomes_and_stamps(self):
        kv = self.seeded()
        calls = []

        def fetch(sym, lb):
            calls.append(sym)
            return bars_from(T0, [101, 102, 103, 104, 105])

        res = so.evaluate_pending(kv.store, T0 + 6 * DAY, fetch=fetch)
        assert sorted(calls) == ["CHASE", "GOOD"] and res["updated"] == 2 and res["completed"] == 0
        row = kv.d["stockky:sniper_obs:day:2026-10-13"]["rows"]["GOOD"]
        assert row["bars_seen"] == 5 and row["ret_1d"] == 1.0 and row["ret_5d"] == 5.0 and row["evaluated_at"]
        assert kv.d[so.INDEX_KEY]["days"]["2026-10-13"] == {"n": 2, "open": 2}

    def test_complete_at_t20_and_index_open_drops(self):
        kv = self.seeded()
        res = so.evaluate_pending(kv.store, T0 + 40 * DAY, fetch=lambda s, lb: bars_from(T0, [100 + i for i in range(1, 25)]))
        assert res["completed"] == 2
        assert kv.d[so.INDEX_KEY]["days"]["2026-10-13"] == {"n": 2, "open": 0}
        # a complete day is not read again
        res2 = so.evaluate_pending(kv.store, T0 + 41 * DAY, fetch=lambda s, lb: pytest.fail("must not fetch"))
        assert res2["pending"] == 0

    def test_recently_evaluated_row_is_not_refetched(self):
        kv = self.seeded()
        so.evaluate_pending(kv.store, T0 + 6 * DAY, fetch=lambda s, lb: bars_from(T0, [101, 102]))
        res = so.evaluate_pending(kv.store, T0 + 6 * DAY + 3600, fetch=lambda s, lb: pytest.fail("must not refetch within 20 h"))
        assert res["symbols"] == 0
        res = so.evaluate_pending(kv.store, T0 + 7 * DAY + 3600, fetch=lambda s, lb: bars_from(T0, [101, 102, 103]))
        assert res["updated"] == 2

    def test_fetch_failure_keeps_rows_open(self):
        kv = self.seeded()

        def boom(s, lb):
            raise RuntimeError("history down")
        res = so.evaluate_pending(kv.store, T0 + 6 * DAY, fetch=boom)
        assert res["fetch_fail"] == 2 and res["updated"] == 0 and kv.d[so.INDEX_KEY]["days"]["2026-10-13"]["open"] == 2

    def test_gives_up_only_after_the_window_and_only_on_an_answer(self):
        kv = self.seeded()
        # inside the 45-day window an empty answer just waits
        res = so.evaluate_pending(kv.store, T0 + 30 * DAY, fetch=lambda s, lb: [])
        assert res["gave_up"] == 0 and kv.d[so.INDEX_KEY]["days"]["2026-10-13"]["open"] == 2
        # a failed fetch (no answer) never closes a row, however old
        def boom(s, lb):
            raise RuntimeError("down")
        assert so.evaluate_pending(kv.store, T0 + 60 * DAY, fetch=boom)["gave_up"] == 0
        # past the window, history that answers with nothing after the day (delisted / halted) closes the row
        res = so.evaluate_pending(kv.store, T0 + 60 * DAY, fetch=lambda s, lb: bars_from(T0 - 30 * DAY, [90, 91]))
        assert res["gave_up"] == 2 and kv.d[so.INDEX_KEY]["days"]["2026-10-13"]["open"] == 0

    def test_batch_limits_symbols_per_pass(self):
        kv = self.seeded()
        res = so.evaluate_pending(kv.store, T0 + 6 * DAY, batch=1, fetch=lambda s, lb: bars_from(T0, [101, 102]))
        assert res["symbols"] == 1 and res["updated"] == 1

    def test_first_touch_uses_the_structure_stop_and_target(self):
        kv = self.seeded()
        res = so.evaluate_pending(kv.store, T0 + 6 * DAY, fetch=lambda s, lb: bars_from(T0, [100, 106, 113], high_pad=0.5))
        assert res["updated"] == 2
        assert kv.d["stockky:sniper_obs:day:2026-10-13"]["rows"]["GOOD"]["first_touch"] == "target"     # target 112 hit on bar 3

    def test_never_raises(self, monkeypatch):
        class Boom(FakeKv):
            def get(self, k):
                raise RuntimeError("x")
        res = so.evaluate_pending(Boom().store, T0 + DAY * 5, fetch=lambda s, lb: [])
        assert res["updated"] == 0   # unreadable index = nothing pending


# ------------------------------------------------------------------ report
class TestReport:
    def test_empty(self):
        out = so.report(FakeKv().store, now_ts=T0)
        assert out["stats"]["total"] == 0 and out["stats"]["conclusion"] == "insufficient_sample" and out["recent"] == []

    def test_aggregates_and_recent_newest_first(self):
        kv = FakeKv()
        for d in (0, 1):
            rows = [scan_row("GOOD"), scan_row("CHASE", ms_chase_atr=4.0)]
            so.record({"stocks": rows}, sniper(rows), kv.store, T0 + d * DAY)
        so.evaluate_pending(kv.store, T0 + 8 * DAY, fetch=lambda s, lb: bars_from(T0, [101, 102, 103, 104, 105, 106]))
        out = so.report(kv.store, days=60, recent=3, now_ts=T0 + 8 * DAY)
        assert out["stats"]["total"] == 4 and out["stats"]["by_verdict"]["passed"]["n"] == 2 and out["stats"]["by_verdict"]["rejected"]["n"] == 2
        assert len(out["recent"]) == 3 and out["recent"][0]["obs_day"] == "2026-10-14"
        assert out["recent"][0]["reasons"] is not None and "summary_json" not in out["recent"][0]

    def test_days_window_and_clamp(self):
        kv = FakeKv()
        rows = [scan_row("GOOD")]
        so.record({"stocks": rows}, sniper(rows), kv.store, T0)
        assert so.report(kv.store, days=1, now_ts=T0 + 10 * DAY)["stats"]["total"] == 0
        assert so.report(kv.store, days=9999, now_ts=T0 + 10 * DAY)["days"] == 365
        assert so.report(kv.store, days=-5, now_ts=T0)["days"] == 1

    def test_never_raises(self):
        class Boom(FakeKv):
            def get(self, k):
                raise RuntimeError("x")
        out = so.report(Boom().store, now_ts=T0)
        assert out["stats"]["total"] == 0          # unreadable = empty, not an exception


# ------------------------------------------------------------------ fetch_daily_bars
class TestFetch:
    def test_url_period_and_candles(self, monkeypatch):
        import httpx
        seen = {}

        class Resp:
            status_code = 200

            def json(self):
                return {"candles": [{"date": "2026-10-14", "open": 1, "high": 3, "low": 1, "close": 2}]}

        class Client:
            def __init__(self, timeout=None):
                seen["timeout"] = timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                seen["url"], seen["params"] = url, params
                return Resp()

        monkeypatch.setattr(httpx, "Client", Client)
        monkeypatch.setenv("MARKET_DATA_URL", "http://md/")
        bars = so.fetch_daily_bars("abc.ns", 100)
        assert seen["url"] == "http://md/history/ABC" and seen["params"]["period"] == "6mo" and len(bars) == 1
        so.fetch_daily_bars("ABC", 30)
        assert seen["params"]["period"] == "3mo"

    def test_http_error_raises(self, monkeypatch):
        import httpx

        class Client:
            def __init__(self, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                return type("R", (), {"status_code": 503, "json": lambda s: {}})()

        monkeypatch.setattr(httpx, "Client", Client)
        with pytest.raises(RuntimeError):
            so.fetch_daily_bars("ABC", 30)


# ------------------------------------------------------------------ main.py / kv wiring (source / AST)
MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
MAIN_TREE = ast.parse(MAIN_SRC)


def _fn(name):
    return next(n for n in ast.walk(MAIN_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


class TestWiring:
    def test_routes_with_aliases(self):
        routes = {}
        for n in ast.walk(MAIN_TREE):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list:
                    if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post") and d.args:
                        routes[(d.func.attr, d.args[0].value)] = n.name
        for m, p, fn in [("get", "/api/scan/find-buys/edge", "find_buys_edge"), ("get", "/scan/find-buys/edge", "find_buys_edge"),
                         ("post", "/api/scan/find-buys/edge/evaluate", "find_buys_edge_evaluate"),
                         ("post", "/scan/find-buys/edge/evaluate", "find_buys_edge_evaluate")]:
            assert routes.get((m, p)) == fn, (m, p)

    def test_route_records_after_the_response_is_final_and_before_return(self):
        src = ast.get_source_segment(MAIN_SRC, _fn("find_actionable_buys"))
        i_set, i_obs, i_ret = src.index('out.setdefault("count"'), src.index("_sniper_observe(payload, out)"), src.index("        return out\n")
        assert i_set < i_obs < i_ret

    def test_observe_helper_is_fire_and_forget_and_fail_open(self):
        src = ast.get_source_segment(MAIN_SRC, _fn("_sniper_observe"))
        assert "run_in_executor" in src and "except Exception" in src and "enabled()" in src
        assert "await" not in src

    def test_tick_action(self):
        assert 'payload.get("action") == "sniper-observe-eval"' in MAIN_SRC

    def test_durable_prefix_registered(self):
        kv = open(os.path.join(_SERVICE, "kv_cache.py"), encoding="utf-8").read()
        assert '"stockky:sniper_obs:"' in kv
        assert so.DAY_PREFIX.startswith("stockky:sniper_obs:") and so.INDEX_KEY.startswith("stockky:sniper_obs:")

    def test_env_documented(self):
        env = open(os.path.join(_SERVICE, "..", "..", ".env.example"), encoding="utf-8").read()
        for v in ("SNIPER_OBS_ENABLED", "SNIPER_OBS_MAX_PER_DAY", "SNIPER_OBS_RETENTION_DAYS", "SNIPER_OBS_BATCH", "SNIPER_OBS_HISTORY_TIMEOUT_S",
                  "SNIPER_OBS_GIVE_UP_DAYS"):
            assert v in env, v
