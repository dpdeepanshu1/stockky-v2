"""tests/test_group360_structure_refresh.py -- post-close `structure_only` refresh of feed rows' ms_* keys (group 360).

No network, no real feed store, no real clock. `main.py` cannot be imported without fastapi, so its wiring is checked as source text / AST.
Run from services/api-gateway:  python3 -m pytest tests/test_group360_structure_refresh.py -v
"""
from __future__ import annotations

import ast
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import structure_refresh as sr  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ist(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def no_holidays(_d):
    return False


# 2026-10-14 is a Wednesday.
WED = date(2026, 10, 14)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("STRUCTURE_REFRESH_") or k in ("STRUCTURE_PASSTHROUGH", "STRUCTURE_BAR_CLOSE_BUFFER_MIN"):
            monkeypatch.delenv(k, raising=False)
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


def block(asof="2026-10-14", h="h1", q="ok"):
    return {"market_structure": {"schema_version": 1, "params_hash": h, "quality": q, "data_quality": {"last_bar_ts": asof}}}


class FakeStore:
    def __init__(self, rows=None):
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.calls = []

    def list_symbols(self):
        return list(self.rows)

    def get_symbols_bulk(self, syms):
        return {s: dict(self.rows[s]) for s in syms if s in self.rows}

    def get_symbol(self, s):
        r = self.rows.get(s)
        return dict(r) if r else None

    def replace_fields(self, s, set_fields, drop_keys=(), ttl=None):
        self.calls.append((s, dict(set_fields), tuple(drop_keys)))
        if s not in self.rows:
            return False
        row = self.rows[s]
        for k in drop_keys:
            row.pop(k, None)
        row.update({k: v for k, v in set_fields.items() if v is not None})
        return True


# ------------------------------------------------------------------ expected_bar_date
class TestExpectedBarDate:
    def test_after_close_plus_buffer_is_today(self):
        assert sr.expected_bar_date(ist(2026, 10, 14, 15, 51), no_holidays, 20) == WED

    def test_inside_buffer_is_previous_day(self):
        assert sr.expected_bar_date(ist(2026, 10, 14, 15, 49), no_holidays, 20) == date(2026, 10, 13)

    def test_before_close_is_previous_day(self):
        assert sr.expected_bar_date(ist(2026, 10, 14, 7, 3), no_holidays, 20) == date(2026, 10, 13)

    def test_monday_morning_is_friday(self):
        assert sr.expected_bar_date(ist(2026, 10, 12, 9, 0), no_holidays, 20) == date(2026, 10, 9)

    def test_saturday_is_friday(self):
        assert sr.expected_bar_date(ist(2026, 10, 17, 18, 0), no_holidays, 20) == date(2026, 10, 16)

    def test_holiday_today_after_close_is_previous_trading_day(self):
        assert sr.expected_bar_date(ist(2026, 10, 14, 17, 0), lambda d: d == WED, 20) == date(2026, 10, 13)

    def test_holiday_run_skipped(self):
        hol = {date(2026, 10, 13), date(2026, 10, 14)}
        assert sr.expected_bar_date(ist(2026, 10, 15, 9, 0), lambda d: d in hol, 20) == date(2026, 10, 12)

    def test_naive_datetime_treated_as_ist(self):
        assert sr.expected_bar_date(datetime(2026, 10, 14, 16, 0), no_holidays, 20) == WED

    def test_buffer_env_default_20(self, monkeypatch):
        monkeypatch.setenv("STRUCTURE_BAR_CLOSE_BUFFER_MIN", "60")
        assert sr.expected_bar_date(ist(2026, 10, 14, 16, 0), no_holidays) == date(2026, 10, 13)
        monkeypatch.setenv("STRUCTURE_BAR_CLOSE_BUFFER_MIN", "junk")
        assert sr.expected_bar_date(ist(2026, 10, 14, 16, 0), no_holidays) == WED


# ------------------------------------------------------------------ asof_date / row_state / needs_refresh
class TestAsofAndState:
    @pytest.mark.parametrize("v,want", [
        ("2026-10-14", WED),
        ("2026-10-14T00:00:00", WED),
        ("2026-10-13T18:30:00Z", WED),         # UTC evening = next IST day
        ("2026-10-14T00:00:00+05:30", WED),
        (1791916200, WED),                     # 2026-10-13T18:30:00Z in seconds
        (1791916200000, WED),                  # ... in ms
        (None, None), ("", None), ("garbage", None), (True, None), ({}, None),
    ])
    def test_asof_date(self, v, want):
        assert sr.asof_date(v) == want

    def test_states(self):
        assert sr.row_state(None, WED) == "missing"
        assert sr.row_state({}, WED) == "missing"
        assert sr.row_state({"ms_quality": "unavailable"}, WED) == "missing"
        assert sr.row_state({"ms_quality": "ok"}, WED) == "stale"                       # no asof
        assert sr.row_state({"ms_quality": "ok", "ms_asof": "garbage"}, WED) == "stale"
        assert sr.row_state({"ms_quality": "ok", "ms_asof": "2026-10-13"}, WED) == "stale"
        assert sr.row_state({"ms_quality": "ok", "ms_asof": "2026-10-14"}, WED) == "fresh"
        assert sr.row_state({"ms_quality": "limited", "ms_asof": "2026-10-15"}, WED) == "fresh"

    def test_priorities_and_force(self):
        assert sr.needs_refresh({}, WED) == 0
        assert sr.needs_refresh({"ms_quality": "ok", "ms_asof": "2026-10-10"}, WED) == 1
        fresh = {"ms_quality": "ok", "ms_asof": "2026-10-14"}
        assert sr.needs_refresh(fresh, WED) is None
        assert sr.needs_refresh(fresh, WED, force=True) == 2

    def test_recent_attempt_skipped_unless_force(self):
        now = ist(2026, 10, 14, 16, 0)
        row = {"ms_quality": "ok", "ms_asof": "2026-10-10", sr.REFRESHED_AT_KEY: (now - timedelta(hours=2)).isoformat()}
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=6) is None
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=6, force=True) == 1
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=1) == 1       # attempt older than the window
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=0) == 1       # window disabled
        row[sr.REFRESHED_AT_KEY] = "garbage"
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=6) == 1       # unreadable stamp = retry

    def test_future_stamp_does_not_block(self):
        now = ist(2026, 10, 14, 16, 0)
        row = {"ms_quality": "ok", "ms_asof": "2026-10-10", sr.REFRESHED_AT_KEY: (now + timedelta(hours=3)).isoformat()}
        assert sr.needs_refresh(row, WED, now=now, recheck_hours=6) == 1


class TestSelect:
    def rows(self):
        return {
            "AAA": {"ms_quality": "ok", "ms_asof": "2026-10-14"},      # fresh
            "BBB": {"ms_quality": "ok", "ms_asof": "2026-10-09"},      # stale
            "CCC": {},                                                 # missing
            "DDD": {"ms_quality": "unavailable"},                      # missing
            "EEE.NS": {"ms_quality": "ok", "ms_asof": "2026-10-01"},   # stale (suffix normalised)
            "SYSTEM:x": {},
            "": {},
        }

    def test_order_and_tally(self):
        syms, t = sr.select_symbols(self.rows(), WED)
        assert syms == ["CCC", "DDD", "BBB", "EEE"]
        assert t == {"considered": 5, "missing": 2, "stale": 2, "fresh": 1, "selected": 4, "skipped": 1}

    def test_limit(self):
        syms, t = sr.select_symbols(self.rows(), WED, limit=2)
        assert syms == ["CCC", "DDD"] and t["selected"] == 2

    def test_force_includes_fresh_last(self):
        syms, _ = sr.select_symbols(self.rows(), WED, force=True)
        assert syms == ["CCC", "DDD", "BBB", "EEE", "AAA"]

    def test_garbage_rows_do_not_raise(self):
        syms, t = sr.select_symbols({"ZZZ": None, "YYY": "x", "XXX": 5}, WED)
        assert syms == ["XXX", "YYY", "ZZZ"] and t["missing"] == 3
        assert sr.select_symbols(None, WED) == ([], {"considered": 0, "missing": 0, "stale": 0, "fresh": 0, "selected": 0, "skipped": 0})


# ------------------------------------------------------------------ refresh_one
NOW = ist(2026, 10, 14, 16, 0)


class TestRefreshOne:
    def test_replaces_whole_group_and_drops_old_keys(self):
        old = {"symbol": "AAA", "price": 10, "ms_quality": "ok", "ms_asof": "2026-10-09", "ms_hash": "old", "ms_ob_top": 99.0}
        st = FakeStore({"AAA": old})
        assert sr.refresh_one("AAA", lambda s: block(), st, NOW) == sr.REFRESHED
        row = st.rows["AAA"]
        assert row["ms_hash"] == "h1" and row["ms_asof"] == "2026-10-14"
        assert "ms_ob_top" not in row                       # the old zone did not survive next to the new asof
        assert row["price"] == 10                           # non-structure fields untouched
        assert row[sr.REFRESHED_AT_KEY] == NOW.isoformat()
        assert set(st.calls[-1][2]) == set(sr.OWNED_KEYS)

    def test_same_hash_is_unchanged_and_only_stamps(self):
        st = FakeStore({"AAA": {"ms_quality": "ok", "ms_asof": "2026-10-14", "ms_hash": "h1"}})
        assert sr.refresh_one("AAA", lambda s: block(), st, NOW) == sr.UNCHANGED
        assert st.calls[-1][1] == {sr.REFRESHED_AT_KEY: NOW.isoformat()} and st.calls[-1][2] == ()

    def test_no_block_keeps_old_keys_and_stamps(self):
        st = FakeStore({"AAA": {"ms_quality": "ok", "ms_asof": "2026-10-09", "ms_hash": "old"}})
        assert sr.refresh_one("AAA", lambda s: {"symbol": "AAA"}, st, NOW) == sr.NO_BLOCK
        assert st.rows["AAA"]["ms_hash"] == "old" and st.rows["AAA"][sr.REFRESHED_AT_KEY] == NOW.isoformat()

    def test_unknown_schema_is_no_block(self):
        st = FakeStore({"AAA": {}})
        bad = {"market_structure": {"schema_version": 99, "hash": "x"}}
        assert sr.refresh_one("AAA", lambda s: bad, st, NOW) == sr.NO_BLOCK
        assert "ms_hash" not in st.rows["AAA"]

    def test_fetch_none_or_non_dict_or_raise_is_failed(self):
        st = FakeStore({"AAA": {"ms_hash": "old"}})
        assert sr.refresh_one("AAA", lambda s: None, st, NOW) == sr.FAILED
        assert sr.refresh_one("AAA", lambda s: [1], st, NOW) == sr.FAILED

        def boom(_s):
            raise RuntimeError("x")
        assert sr.refresh_one("AAA", boom, st, NOW) == sr.FAILED
        # group424: old structure keys are kept, but each failed attempt is now recorded (it used to leave no trace, so the symbol was
        # re-selected on every catch-up pass). Three failures at the same instant -> count 3.
        row = st.rows["AAA"]
        assert row["ms_hash"] == "old" and row[sr.FAIL_COUNT_KEY] == 3 and row[sr.FAILED_AT_KEY] == NOW.isoformat()
        assert sr.REFRESHED_AT_KEY not in row

    def test_symbol_not_in_feed_is_not_added(self):
        st = FakeStore({})
        assert sr.refresh_one("NEW", lambda s: block(), st, NOW) == sr.NOT_IN_FEED
        assert "NEW" not in st.rows

    def test_store_errors_never_raise(self):
        class Bad(FakeStore):
            def get_symbol(self, s):
                raise RuntimeError("db down")
        assert sr.refresh_one("AAA", lambda s: block(), Bad({"AAA": {}}), NOW) == sr.FAILED

        class BadWrite(FakeStore):
            def replace_fields(self, *a, **k):
                return False
        assert sr.refresh_one("AAA", lambda s: block(), BadWrite({"AAA": {"price": 1}}), NOW) == sr.FAILED

    def test_passthrough_off_means_no_block(self, monkeypatch):
        monkeypatch.setenv("STRUCTURE_PASSTHROUGH", "0")
        st = FakeStore({"AAA": {"ms_hash": "old"}})
        assert sr.refresh_one("AAA", lambda s: block(), st, NOW) == sr.NO_BLOCK
        assert st.rows["AAA"]["ms_hash"] == "old"


# ------------------------------------------------------------------ failure back-off (group 424)
class TestFailureBackoff:
    def failed_row(self, n=1, at=None, **extra):
        r = {"price": 5, sr.FAILED_AT_KEY: (at or NOW).isoformat(), sr.FAIL_COUNT_KEY: n}
        r.update(extra)
        return r

    def test_first_failure_recorded_then_counts_up(self):
        st = FakeStore({"AAA": {"price": 1}})
        assert sr.refresh_one("AAA", lambda s: None, st, NOW) == sr.FAILED
        assert st.rows["AAA"][sr.FAIL_COUNT_KEY] == 1
        assert sr.refresh_one("AAA", lambda s: None, st, NOW + timedelta(hours=2)) == sr.FAILED
        assert st.rows["AAA"][sr.FAIL_COUNT_KEY] == 2
        assert st.rows["AAA"][sr.FAILED_AT_KEY] == (NOW + timedelta(hours=2)).isoformat()

    def test_failure_never_creates_a_row(self):
        st = FakeStore({})
        assert sr.refresh_one("NEW", lambda s: None, st, NOW) == sr.FAILED
        assert "NEW" not in st.rows

    def test_backoff_schedule(self):
        assert [sr.fail_backoff_hours(n) for n in (0, 1, 2, 3, 4, 5, 6, 20)] == [0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 24.0, 24.0]

    def test_symbol_is_skipped_inside_backoff_and_returns_after(self):
        exp = WED
        row = self.failed_row(n=3)                       # 4 h wait
        assert sr.needs_refresh(row, exp, now=NOW + timedelta(hours=3, minutes=59)) is None
        assert sr.needs_refresh(row, exp, now=NOW + timedelta(hours=4, minutes=1)) == 0   # missing row -> priority 0

    def test_force_ignores_backoff(self):
        assert sr.needs_refresh(self.failed_row(n=5), WED, now=NOW + timedelta(minutes=1), force=True) is not None

    def test_newer_answered_attempt_lifts_backoff(self):
        row = self.failed_row(n=4, **{sr.REFRESHED_AT_KEY: (NOW + timedelta(minutes=5)).isoformat()})
        assert sr._live_fail_count(row) == 0
        assert not sr.in_fail_backoff(row, NOW + timedelta(minutes=6))

    def test_select_symbols_leaves_backed_off_symbol_out(self):
        rows = {"AAA": self.failed_row(n=2), "BBB": {"price": 1}}
        syms, tally = sr.select_symbols(rows, WED, now=NOW + timedelta(minutes=30))
        assert syms == ["BBB"] and tally["skipped"] == 1

    def test_success_clears_failure_stamp(self):
        st = FakeStore({"AAA": self.failed_row(n=3, ms_quality="ok", ms_asof="2026-10-09", ms_hash="old")})
        assert sr.refresh_one("AAA", lambda s: block(), st, NOW + timedelta(hours=5)) == sr.REFRESHED
        assert sr.FAILED_AT_KEY not in st.rows["AAA"] and sr.FAIL_COUNT_KEY not in st.rows["AAA"]

    def test_unchanged_clears_failure_stamp(self):
        st = FakeStore({"AAA": self.failed_row(n=2, ms_quality="ok", ms_asof="2026-10-14", ms_hash="h1")})
        assert sr.refresh_one("AAA", lambda s: block(), st, NOW + timedelta(hours=5)) == sr.UNCHANGED
        assert sr.FAILED_AT_KEY not in st.rows["AAA"] and sr.FAIL_COUNT_KEY not in st.rows["AAA"]

    def test_no_block_answer_lifts_backoff_via_timestamp(self):
        st = FakeStore({"AAA": self.failed_row(n=4)})
        assert sr.refresh_one("AAA", lambda s: {"symbol": "AAA"}, st, NOW + timedelta(hours=1)) == sr.NO_BLOCK
        assert sr._live_fail_count(st.rows["AAA"]) == 0

    def test_failure_is_logged_with_the_symbol(self, caplog):
        import logging
        st = FakeStore({"TONBO": {"price": 1}})
        with caplog.at_level(logging.WARNING, logger="structure-refresh"):
            sr.refresh_one("TONBO", lambda s: None, st, NOW)
        assert any("TONBO" in r.getMessage() for r in caplog.records)

    def test_unreadable_failure_fields_are_ignored(self):
        assert sr._live_fail_count({sr.FAILED_AT_KEY: "garbage", sr.FAIL_COUNT_KEY: 3}) == 0
        assert sr._live_fail_count({sr.FAILED_AT_KEY: NOW.isoformat(), sr.FAIL_COUNT_KEY: "x"}) == 1
        assert sr._live_fail_count(None) == 0


# ------------------------------------------------------------------ plan + run
class TestPlanAndRun:
    def feed(self):
        return {
            "AAA": {"ms_quality": "ok", "ms_asof": "2026-10-14", "ms_hash": "h1"},   # fresh -> skipped
            "BBB": {"ms_quality": "ok", "ms_asof": "2026-10-09", "ms_hash": "o"},    # stale
            "CCC": {"price": 5},                                                      # missing
        }

    def test_plan_uses_bulk_and_selects(self):
        st = FakeStore(self.feed())
        syms, tally, exp = sr.plan(st, now=ist(2026, 10, 14, 16, 0))
        assert syms == ["CCC", "BBB"] and exp == WED and tally["skipped"] == 1

    def test_plan_falls_back_to_single_reads(self):
        class NoBulk(FakeStore):
            def get_symbols_bulk(self, syms):
                raise RuntimeError("nope")
        syms, _, _ = sr.plan(NoBulk(self.feed()), now=ist(2026, 10, 14, 16, 0))
        assert syms == ["CCC", "BBB"]

    def test_plan_limit_env_and_arg(self, monkeypatch):
        monkeypatch.setenv("STRUCTURE_REFRESH_MAX_SYMBOLS", "1")
        assert sr.plan(FakeStore(self.feed()), now=ist(2026, 10, 14, 16, 0))[0] == ["CCC"]
        assert len(sr.plan(FakeStore(self.feed()), limit=0, now=ist(2026, 10, 14, 16, 0))[0]) == 2   # explicit 0 = no cap

    def test_plan_store_down(self):
        class Dead:
            def list_symbols(self):
                raise RuntimeError("x")
        assert sr.plan(Dead())[0] == []

    def test_run_counts_lifetime_and_status(self):
        st = FakeStore(self.feed())
        data = {"BBB": block(), "CCC": None}
        out = sr.run(["BBB", "CCC", "BBB", "ddd.ns"], st, lambda s: data.get(s), concurrency=2, tally={"selected": 3})
        assert out["status"] == "done" and out["total"] == 3 and out["processed"] == 3     # de-duplicated, normalised
        assert out["refreshed"] == 1 and out["failed"] == 2
        assert out["lifetime"]["runs"] == 1 and out["lifetime"]["symbols"] == 3 and out["lifetime"]["refreshed"] == 1
        assert out["selection"] == {"selected": 3} and out["finished_at"]
        assert st.rows["BBB"]["ms_hash"] == "h1"

    def test_run_lifetime_accumulates(self):
        st = FakeStore(self.feed())
        sr.run(["BBB"], st, lambda s: block())
        out = sr.run(["BBB"], st, lambda s: block())      # second pass: same hash
        assert out["unchanged"] == 1 and out["lifetime"]["runs"] == 2 and out["lifetime"]["refreshed"] == 1

    def test_run_empty(self):
        out = sr.run([], FakeStore(), lambda s: None)
        assert out["status"] == "done" and out["total"] == 0

    def test_run_stop(self):
        st = FakeStore({f"S{i}": {} for i in range(20)})

        def fetch(s):
            sr.request_stop()
            return block()
        out = sr.run(list(st.rows), st, fetch, concurrency=1)
        assert out["status"] == "stopped" and out["processed"] < 20

    def test_run_never_raises_on_executor_failure(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("pool")
        monkeypatch.setattr(sr, "ThreadPoolExecutor", boom)
        out = sr.run(["AAA"], FakeStore({"AAA": {}}), lambda s: block())
        assert out["status"] == "error"

    def test_stalled_detection(self, monkeypatch):
        sr._set_job(status="running", message="x")
        sr._JOB["_updated_epoch"] = 1.0
        assert sr.get_job()["status"] == "stalled" and sr.is_running() is False
        assert "_updated_epoch" not in sr.get_job()

    def test_enabled_flag(self, monkeypatch):
        assert sr.enabled() is True
        for v in ("0", "false", "off", "no"):
            monkeypatch.setenv("STRUCTURE_REFRESH_ENABLED", v)
            assert sr.enabled() is False
        assert sr.get_job()["enabled"] is False


# ------------------------------------------------------------------ fetcher
class TestFetcher:
    class Resp:
        def __init__(self, code, body=b"{}", js=None):
            self.status_code, self.content, self._js = code, body, js

        def json(self):
            return self._js

    class Client:
        def __init__(self, resps):
            self.resps, self.urls = list(resps), []

        def get(self, url, timeout=None):
            self.urls.append(url)
            r = self.resps.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    def test_ok_and_url(self, monkeypatch):
        monkeypatch.setenv("TECHNICAL_URL", "http://tech:8000/")
        c = self.Client([self.Resp(200, b"x", {"a": 1})])
        assert sr.make_fetcher(c)("AAA") == {"a": 1} and c.urls == ["http://tech:8000/structure/AAA"]

    def test_default_url_from_analysis_base(self, monkeypatch):
        monkeypatch.delenv("TECHNICAL_URL", raising=False)
        monkeypatch.setenv("ANALYSIS_INTELLIGENCE_URL", "http://ai/")
        assert sr._technical_url() == "http://ai/technical"

    def test_429_retries_once(self, monkeypatch):
        monkeypatch.setattr(sr.time, "sleep", lambda s: None)
        c = self.Client([self.Resp(429), self.Resp(200, b"x", {"ok": 1})])
        assert sr.make_fetcher(c)("AAA") == {"ok": 1} and len(c.urls) == 2

    @pytest.mark.parametrize("resp", [Resp(404), Resp(500), Resp(200, b""), Resp(200, b"x", [1]), RuntimeError("net")])
    def test_failures_return_none(self, resp):
        assert sr.make_fetcher(self.Client([resp]))("AAA") is None


# ------------------------------------------------------------------ real DataFeedStore.replace_fields
class TestReplaceFieldsReal:
    @pytest.fixture
    def store(self, monkeypatch):
        df = pytest.importorskip("data_feed")
        mem = {}
        s = df.DataFeedStore(lambda k: mem.get(k), lambda k, v, ttl=None: mem.__setitem__(k, v), None)
        monkeypatch.setattr(s, "_get", lambda k: mem.get(k), raising=False)
        monkeypatch.setattr(s, "_set", lambda k, v, ttl=None: mem.__setitem__(k, v), raising=False)
        df._LOCAL_SYMBOLS.clear()
        s._mem = mem
        yield s
        df._LOCAL_SYMBOLS.clear()

    def test_replace_drops_then_sets_and_writes_all_aliases(self, store):
        import data_feed as df
        base = {"symbol": "AAA", "price": 10, "ms_hash": "old", "ms_ob_top": 1.0, "pe_ratio": 20}
        df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "AAA"] = dict(base)
        assert store.replace_fields("aaa.ns", {"ms_hash": "new", "ms_x": None}, drop_keys=("ms_hash", "ms_ob_top")) is True
        row = store.get_symbol("AAA")
        assert row["ms_hash"] == "new" and "ms_ob_top" not in row and "ms_x" not in row
        assert row["pe_ratio"] == 20 and row["price"] == 10 and row["updated_at"]
        for k in (df.DATA_FEED_PREFIX, df.FEED_ALIAS_PREFIX, df.FEED_LEGACY_PREFIX):
            assert store._mem[k + "AAA"]["ms_hash"] == "new"

    def test_never_creates_a_row(self, store):
        assert store.replace_fields("NOPE", {"ms_hash": "x"}) is False
        assert store.get_symbol("NOPE") is None

    def test_bad_input(self, store):
        assert store.replace_fields("", {"a": 1}) is False
        assert store.replace_fields("AAA", None) is False

    def test_write_failure_returns_false(self, store, monkeypatch):
        import data_feed as df
        df._LOCAL_SYMBOLS[df.DATA_FEED_PREFIX + "AAA"] = {"symbol": "AAA", "price": 1}

        def boom(*a, **k):
            raise RuntimeError("neon")
        monkeypatch.setattr(store, "_set", boom)
        assert store.replace_fields("AAA", {"ms_hash": "x"}) is False


# ------------------------------------------------------------------ main.py wiring (source / AST)
MAIN_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
MAIN_TREE = ast.parse(MAIN_SRC)
import glob as _glob  # noqa: E402
_SUPPORT_SRC = "\n".join(open(f, encoding="utf-8").read() for f in sorted(_glob.glob(os.path.join(_SERVICE, "gateway_support", "bound_routes*.py"))))


def _routes():
    out = {}
    # E49: some handlers moved to gateway_routers/*.py; scan those files too
    import glob
    trees = [MAIN_TREE] + [ast.parse(open(f, encoding="utf-8").read()) for f in sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_routers", "*.py")))
                                      + sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_support", "bound_routes*.py")))]   # E87: batch L (gateway_support) holds the status / start routes now
    for n in (x for t in trees for x in ast.walk(t)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "post") and d.args:
                    out.setdefault((d.func.attr, d.args[0].value), n.name)
    return out


class TestMainWiring:
    def test_routes_registered_with_api_aliases(self):
        r = _routes()
        for m, p, fn in [
            ("post", "/data-feed/structure-refresh", "data_feed_structure_refresh"),
            ("post", "/api/data-feed/structure-refresh", "data_feed_structure_refresh"),
            ("get", "/data-feed/structure-refresh/status", "data_feed_structure_refresh_status"),
            ("get", "/api/data-feed/structure-refresh/status", "data_feed_structure_refresh_status"),
            ("post", "/data-feed/structure-refresh/stop", "data_feed_structure_refresh_stop"),
            ("post", "/api/data-feed/structure-refresh/stop", "data_feed_structure_refresh_stop"),
        ]:
            assert r.get((m, p)) == fn, (m, p)

    def test_get_on_action_name_is_405_not_catch_all(self):
        assert '"refill-additional", "structure-refresh",' in MAIN_SRC

    def test_status_route_comes_before_symbol_catch_all(self):
        assert MAIN_SRC.index("app.include_router(_bound_routes_l.data_feed_structure_refresh_status_router)") < MAIN_SRC.index("app.include_router(_bound_routes_l.data_feed_symbol_router)")   # E87

    def test_tick_action_and_jobs_snapshot(self):
        assert 'payload.get("action") == "structure-refresh"' in MAIN_SRC + _SUPPORT_SRC      # E87: the tick handler moved to gateway_support/bound_routes_d.py (E78)
        assert 'out["structure_refresh"]' in MAIN_SRC
        assert '"refill_additional", "structure_refresh", "surprise_premarket"' in MAIN_SRC

    def test_start_helper_is_locked_and_never_raises_shape(self):
        fn = next(n for n in ast.walk(MAIN_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_start_structure_refresh")
        src = ast.get_source_segment(MAIN_SRC, fn)
        assert "acquire(blocking=False)" in src and "finally" in src and "release()" in src
        inner = next(n for n in ast.walk(MAIN_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_start_structure_refresh_locked")
        isrc = ast.get_source_segment(MAIN_SRC, inner)
        for needle in ("_sr.enabled()", "already_running", "dry_run", "threading.Thread", "daemon=True", '"ok": False'):
            assert needle in isrc, needle

    def test_counters_are_fail_safe(self):
        fn = next(n for n in ast.walk(MAIN_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_structure_metric")
        assert "except Exception" in ast.get_source_segment(MAIN_SRC, fn)
        # E54: the sniper counter name is used in gateway_routers/sniper.py now; the prepare counter is still in main.py
        _sniper = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gateway_routers", "sniper.py"), encoding="utf-8").read()
        assert "stockky_sniper_structure_fetch_total" in _sniper
        assert "stockky_prepare_structure_total" in MAIN_SRC + _SUPPORT_SRC      # E87: moved to gateway_support/bound_routes_f.py (E80)

    def test_metric_helper_behaviour(self):
        ns = {"metrics": type("M", (), {"inc": staticmethod(lambda n, v, **l: ns.setdefault("seen", []).append((n, v, l)))})}
        fn = next(n for n in ast.walk(MAIN_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_structure_metric")
        exec(compile(ast.Module([fn], []), "m", "exec"), {"Any": object, **ns}, ns)
        ns["_structure_metric"]("x", 2, outcome="a")
        ns["_structure_metric"]("x", 0, outcome="b")            # zero is not recorded
        assert ns["seen"] == [("x", 2.0, {"outcome": "a"})]
        ns["metrics"] = None                                    # broken registry must not raise
        ns["_structure_metric"]("x", 1)

    def test_env_vars_documented(self):
        env = open(os.path.join(_SERVICE, "..", "..", ".env.example"), encoding="utf-8").read()
        for v in ("STRUCTURE_REFRESH_ENABLED", "STRUCTURE_REFRESH_CONCURRENCY", "STRUCTURE_REFRESH_MAX_SYMBOLS",
                  "STRUCTURE_REFRESH_RECHECK_H", "STRUCTURE_REFRESH_TIMEOUT_S", "STRUCTURE_REFRESH_STALE_AFTER_SEC"):
            assert v in env, v
