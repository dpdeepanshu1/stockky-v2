"""
tests/test_rate_limit_report.py — coverage for rate_limit_report.py
No Neon, no Redis. kv_cache stubbed in sys.modules.
"""
from __future__ import annotations
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import rate_limit_report as rlr


def _install_kv(monkeypatch):
    store = {}
    fake = types.ModuleType("kv_cache")
    fake.kv_get = lambda k: store.get(k)
    fake.kv_set = lambda k, v, ttl=None: store.update({k: v})
    monkeypatch.setitem(sys.modules, "kv_cache", fake)
    return store


# ── record_rate_limit_hit ────────────────────────────────────────────────────

class TestRecordRateLimitHit:
    def test_writes_event_to_neon(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit("analysis", 429, "/news", "quota hit", "RELIANCE")
        events = store.get(rlr.NEON_EVENTS_KEY)
        assert events is not None
        assert events[0]["source"] == "analysis"
        assert events[0]["status"] == 429
        assert events[0]["symbol"] == "RELIANCE"
        assert events[0]["origin"] == "analysis-intelligence-service"

    def test_writes_stats_to_neon(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit("gemini", 429)
        stats = store.get(rlr.NEON_STATS_KEY)
        assert "by_source_1h" in stats
        assert "gemini" in stats["by_source_1h"]

    def test_truncates_events_at_500(self, monkeypatch):
        store = _install_kv(monkeypatch)
        existing = [{"ts": time.time(), "source": "x", "status": 429,
                     "path": "", "detail": "", "symbol": "", "origin": "test"}
                    for _ in range(600)]
        store[rlr.NEON_EVENTS_KEY] = existing
        rlr.record_rate_limit_hit("analysis")
        assert len(store[rlr.NEON_EVENTS_KEY]) <= 500

    def test_merges_prior_stats_for_expired_sources(self, monkeypatch):
        store = _install_kv(monkeypatch)
        # Pre-seed stats with a source not in current window
        store[rlr.NEON_STATS_KEY] = {"by_source_1h": {"old_source": 5}}
        rlr.record_rate_limit_hit("gemini", 429)
        stats = store[rlr.NEON_STATS_KEY]
        assert "old_source" in stats["by_source_1h"]
        assert "gemini" in stats["by_source_1h"]

    def test_events_from_old_dict_format_handled(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_EVENTS_KEY] = {"events": [
            {"ts": time.time(), "source": "prior", "status": 429,
             "path": "", "detail": "", "symbol": "", "origin": "test"}
        ]}
        rlr.record_rate_limit_hit("groq")
        events = store[rlr.NEON_EVENTS_KEY]
        assert any(e["source"] == "prior" for e in events)

    def test_gateway_post_when_api_gateway_url_set(self, monkeypatch):
        _install_kv(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_URL", "http://fake-gw:8000")
        called = []
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda url, **kw: called.append(url) or type("R", (), {})())
        rlr.record_rate_limit_hit("analysis")
        assert any("rate-limits" in u for u in called)

    def test_no_gateway_post_when_env_absent(self, monkeypatch):
        _install_kv(monkeypatch)
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
        called = []
        import requests
        monkeypatch.setattr(requests, "post",
                            lambda *a, **kw: called.append(True))
        rlr.record_rate_limit_hit("analysis")
        assert not called

    def test_swallows_kv_failure(self, monkeypatch):
        fake = types.ModuleType("kv_cache")
        fake.kv_get = lambda k: (_ for _ in ()).throw(RuntimeError("neon down"))
        fake.kv_set = lambda k, v, ttl=None: None
        monkeypatch.setitem(sys.modules, "kv_cache", fake)
        rlr.record_rate_limit_hit("analysis")   # must not raise

    def test_provider_truncated_to_40_chars(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit("a" * 100)
        events = store[rlr.NEON_EVENTS_KEY]
        assert len(events[0]["source"]) <= 40

    def test_detail_truncated_to_200_chars(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit(detail="d" * 500)
        events = store[rlr.NEON_EVENTS_KEY]
        assert len(events[0]["detail"]) <= 200


# ── report_if_rate_limited ───────────────────────────────────────────────────

class TestReportIfRateLimited:
    def test_returns_true_for_429_status(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(Exception("error"), status=429) is True

    def test_returns_true_for_503_status(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(Exception("svc unavail"), status=503) is True

    def test_returns_true_for_rate_limit_in_message(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(Exception("429 Too Many Requests")) is True

    def test_returns_true_for_quota_message(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(Exception("quota exceeded")) is True

    def test_returns_false_for_generic_error(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(Exception("connection refused")) is False

    def test_extracts_status_code_from_response_attr(self, monkeypatch):
        _install_kv(monkeypatch)
        class _FakeResp:
            status_code = 429
        class _FakeErr(Exception):
            response = _FakeResp()
        assert rlr.report_if_rate_limited(_FakeErr("err")) is True

    def test_returns_false_for_none(self, monkeypatch):
        _install_kv(monkeypatch)
        assert rlr.report_if_rate_limited(None) is False

    def test_calls_record_when_rate_limited(self, monkeypatch):
        _install_kv(monkeypatch)
        called = []
        monkeypatch.setattr(rlr, "record_rate_limit_hit", lambda **kw: called.append(kw))
        rlr.report_if_rate_limited(Exception("throttled"), provider="gemini",
                                    path="/news", symbol="TCS")
        assert called[0]["provider"] == "gemini"
        assert called[0]["symbol"] == "TCS"


# ══ additional coverage: kv fallback paths, stats edge cases, gateway, status extraction ══

_FUND_DIR = os.path.join(os.path.dirname(os.path.abspath(rlr.__file__)), "fundamental")


def _flaky_kv(monkeypatch, fail_times=1):
    """kv_cache stub whose first `fail_times` kv_get / kv_set calls raise."""
    store = {}
    calls = {"get": 0, "set": 0}
    fake = types.ModuleType("kv_cache")

    def kv_get(k):
        calls["get"] += 1
        if calls["get"] <= fail_times:
            raise RuntimeError("kv_get failed")
        return store.get(k)

    def kv_set(k, v, ttl=None):
        calls["set"] += 1
        if calls["set"] <= fail_times:
            raise RuntimeError("kv_set failed")
        store[k] = (v, ttl)

    fake.kv_get = kv_get
    fake.kv_set = kv_set
    monkeypatch.setitem(sys.modules, "kv_cache", fake)
    return store, calls


class TestKvGetFallback:
    def test_first_attempt_success_does_not_touch_sys_path(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store["k"] = "v"
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != _FUND_DIR])
        assert rlr._kv_get("k") == "v"
        assert _FUND_DIR not in sys.path

    def test_retries_after_adding_fundamental_dir_to_sys_path(self, monkeypatch):
        store, calls = _flaky_kv(monkeypatch)
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != _FUND_DIR])
        store["k"] = "v"
        assert rlr._kv_get("k") == "v"
        assert calls["get"] == 2
        assert sys.path[0] == _FUND_DIR

    def test_fundamental_dir_not_duplicated_on_sys_path(self, monkeypatch):
        _flaky_kv(monkeypatch)
        monkeypatch.setattr(sys, "path", [_FUND_DIR] + [p for p in sys.path if p != _FUND_DIR])
        rlr._kv_get("k")
        assert sys.path.count(_FUND_DIR) == 1

    def test_returns_none_and_logs_when_both_attempts_fail(self, monkeypatch, caplog):
        _, calls = _flaky_kv(monkeypatch, fail_times=99)
        with caplog.at_level("DEBUG", logger="rate-limit-report"):
            assert rlr._kv_get("k") is None
        assert calls["get"] == 2
        assert any("rate_limit_report kv_get" in r.getMessage() for r in caplog.records)

    def test_returns_none_when_kv_cache_cannot_be_imported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)   # `import kv_cache` -> ImportError
        assert rlr._kv_get("k") is None


class TestKvSetFallback:
    def test_first_attempt_passes_value_and_default_ttl(self, monkeypatch):
        store, calls = _flaky_kv(monkeypatch, fail_times=0)
        rlr._kv_set("k", {"a": 1})
        assert calls["set"] == 1
        assert store["k"] == ({"a": 1}, rlr.NEON_TTL)

    def test_custom_ttl_is_forwarded(self, monkeypatch):
        store, _ = _flaky_kv(monkeypatch, fail_times=0)
        rlr._kv_set("k", "v", ttl=42)
        assert store["k"] == ("v", 42)

    def test_retries_after_adding_fundamental_dir_to_sys_path(self, monkeypatch):
        store, calls = _flaky_kv(monkeypatch)
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != _FUND_DIR])
        rlr._kv_set("k", "v", ttl=7)
        assert calls["set"] == 2
        assert store["k"] == ("v", 7)
        assert sys.path[0] == _FUND_DIR

    def test_fundamental_dir_not_duplicated_on_sys_path(self, monkeypatch):
        _flaky_kv(monkeypatch)
        monkeypatch.setattr(sys, "path", [_FUND_DIR] + [p for p in sys.path if p != _FUND_DIR])
        rlr._kv_set("k", "v")
        assert sys.path.count(_FUND_DIR) == 1

    def test_never_raises_and_logs_when_both_attempts_fail(self, monkeypatch, caplog):
        _, calls = _flaky_kv(monkeypatch, fail_times=99)
        with caplog.at_level("DEBUG", logger="rate-limit-report"):
            rlr._kv_set("k", "v")          # must not raise
        assert calls["set"] == 2
        assert any("rate_limit_report kv_set" in r.getMessage() for r in caplog.records)

    def test_never_raises_when_kv_cache_cannot_be_imported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "kv_cache", None)
        rlr._kv_set("k", "v")


class TestRecordEdgeCases:
    def test_none_provider_defaults_to_analysis_and_is_lowercased(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit(provider=None)
        rlr.record_rate_limit_hit(provider="GeMiNi")
        events = store[rlr.NEON_EVENTS_KEY]
        assert events[0]["source"] == "gemini"
        assert events[1]["source"] == "analysis"

    def test_path_symbol_truncated_and_status_coerced_to_int(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit(status="503", path="p" * 300, symbol="S" * 90)
        ev = store[rlr.NEON_EVENTS_KEY][0]
        assert ev["status"] == 503 and isinstance(ev["status"], int)
        assert len(ev["path"]) == 120
        assert len(ev["symbol"]) == 32

    def test_new_event_is_inserted_first(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit("first")
        rlr.record_rate_limit_hit("second")
        assert [e["source"] for e in store[rlr.NEON_EVENTS_KEY]] == ["second", "first"]

    def test_unrecognised_stored_events_shape_is_replaced_by_fresh_list(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_EVENTS_KEY] = "garbage"
        rlr.record_rate_limit_hit("groq")
        assert [e["source"] for e in store[rlr.NEON_EVENTS_KEY]] == ["groq"]

    def test_dict_shape_without_list_events_is_replaced(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_EVENTS_KEY] = {"events": "not-a-list"}
        rlr.record_rate_limit_hit("groq")
        assert len(store[rlr.NEON_EVENTS_KEY]) == 1

    def test_non_dict_old_and_timestampless_events_are_kept_but_not_counted(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_EVENTS_KEY] = [
            "junk",
            {"ts": time.time() - 7200, "source": "stale"},
            {"source": "nots"},
            {"ts": time.time()},                      # no source -> "unknown"
        ]
        rlr.record_rate_limit_hit("gemini")
        counts = store[rlr.NEON_STATS_KEY]["by_source_1h"]
        assert counts == {"gemini": 1, "unknown": 1}
        assert len(store[rlr.NEON_EVENTS_KEY]) == 5   # nothing dropped from the event log

    def test_stats_shape(self, monkeypatch):
        store = _install_kv(monkeypatch)
        rlr.record_rate_limit_hit("gemini", 429, "/n", "d", "TCS")
        rlr.record_rate_limit_hit("gemini", 429)
        s = store[rlr.NEON_STATS_KEY]
        assert s["window_sec"] == 3600
        assert s["by_source_1h"] == {"gemini": 2}
        assert s["events_1h"] == 2
        assert s["last_hit"]["source"] == "gemini" and s["last_hit"]["symbol"] == ""
        assert s["limits"]["analysis"] == {"limit": 300}
        assert set(s["limits"]) == {"market_data", "analysis", "indianapi", "gemini", "groq", "nse"}
        assert s["updated_at"] == pytest.approx(time.time(), abs=5)

    def test_prior_counts_are_coerced_and_bad_values_skipped(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_STATS_KEY] = {"by_source_1h": {"a": "7", "b": "abc", "c": None, "d": 3}}
        rlr.record_rate_limit_hit("gemini")
        counts = store[rlr.NEON_STATS_KEY]["by_source_1h"]
        assert counts["a"] == 7 and counts["d"] == 3
        assert "b" not in counts and "c" not in counts

    def test_prior_count_does_not_override_fresh_window_count(self, monkeypatch):
        store = _install_kv(monkeypatch)
        store[rlr.NEON_STATS_KEY] = {"by_source_1h": {"gemini": 99}}
        rlr.record_rate_limit_hit("gemini")
        assert store[rlr.NEON_STATS_KEY]["by_source_1h"]["gemini"] == 1

    @pytest.mark.parametrize("prior", ["junk", {"by_source_1h": "junk"}, {}, None])
    def test_malformed_prior_stats_are_ignored(self, monkeypatch, prior):
        store = _install_kv(monkeypatch)
        if prior is not None:
            store[rlr.NEON_STATS_KEY] = prior
        rlr.record_rate_limit_hit("gemini")
        assert store[rlr.NEON_STATS_KEY]["by_source_1h"] == {"gemini": 1}

    def test_internal_failure_is_logged_and_gateway_still_notified(self, monkeypatch, caplog):
        def boom(_key):
            raise RuntimeError("neon down")
        monkeypatch.setattr(rlr, "_kv_get", boom)
        monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
        import requests
        called = []
        monkeypatch.setattr(requests, "post", lambda url, **kw: called.append(url))
        with caplog.at_level("WARNING", logger="rate-limit-report"):
            rlr.record_rate_limit_hit("analysis")       # must not raise
        assert any("Failed to record analysis rate limit stat" in r.getMessage() for r in caplog.records)
        assert called == ["http://gw:1/ops/rate-limits/event"]


class TestGatewayPost:
    def test_payload_url_and_timeout(self, monkeypatch):
        _install_kv(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1/")      # trailing slash stripped
        import requests
        seen = {}

        def fake_post(url, **kw):
            seen["url"], seen["kw"] = url, kw

        monkeypatch.setattr(requests, "post", fake_post)
        rlr.record_rate_limit_hit("gemini", 503, "p" * 300, "d" * 500, "TCS")
        assert seen["url"] == "http://gw:1/ops/rate-limits/event"
        assert seen["kw"]["timeout"] == 2
        body = seen["kw"]["json"]
        assert body["source"] == "gemini" and body["status"] == 503 and body["symbol"] == "TCS"
        assert len(body["detail"]) == 200
        assert len(body["path"]) == 300        # pinned: the gateway payload is not truncated

    def test_gateway_failure_is_swallowed(self, monkeypatch):
        _install_kv(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")
        import requests

        def boom(*a, **kw):
            raise requests.exceptions.ConnectionError("down")

        monkeypatch.setattr(requests, "post", boom)
        rlr.record_rate_limit_hit("analysis")    # must not raise

    def test_whitespace_only_gateway_url_still_posts(self, monkeypatch):
        _install_kv(monkeypatch)
        monkeypatch.setenv("API_GATEWAY_URL", "   ")
        import requests
        called = []
        monkeypatch.setattr(requests, "post", lambda *a, **kw: called.append(a))
        rlr.record_rate_limit_hit("analysis")
        # Pinned: "   ".rstrip("/") is still truthy, so a post IS attempted.
        assert len(called) == 1


class TestReportEdgeCases:
    @pytest.fixture
    def recorded(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rlr, "record_rate_limit_hit", lambda **kw: calls.append(kw))
        return calls

    @pytest.mark.parametrize("msg", ["Rate-Limited by upstream", "THROTTLED", "Too Many Requests", "quota", "rate limit hit"])
    def test_message_keywords_default_status_to_429(self, recorded, msg):
        assert rlr.report_if_rate_limited(Exception(msg)) is True
        assert recorded[0]["status"] == 429

    def test_explicit_status_wins_over_message_default(self, recorded):
        assert rlr.report_if_rate_limited("quota exceeded", status=500) is True
        assert recorded[0]["status"] == 500

    def test_non_rate_limit_status_with_clean_message_is_false(self, recorded):
        assert rlr.report_if_rate_limited(Exception("boom"), status=500) is False
        assert recorded == []

    def test_response_status_503_is_recorded_with_503(self, recorded):
        class Err(Exception):
            response = type("R", (), {"status_code": 503})()
        assert rlr.report_if_rate_limited(Err("upstream"), provider="indianapi", path="/x", symbol="INFY") is True
        assert recorded[0] == {"provider": "indianapi", "status": 503, "path": "/x",
                               "detail": "upstream", "symbol": "INFY"}

    def test_response_with_falsy_status_code_is_ignored(self, recorded):
        class Err(Exception):
            response = type("R", (), {"status_code": 0})()
        assert rlr.report_if_rate_limited(Err("plain failure")) is False

    def test_response_with_non_numeric_status_code_is_ignored(self, recorded):
        class Err(Exception):
            response = type("R", (), {"status_code": "abc"})()
        assert rlr.report_if_rate_limited(Err("plain failure")) is False

    def test_response_whose_status_code_raises_is_ignored(self, recorded):
        class Resp:
            @property
            def status_code(self):
                raise RuntimeError("no status")

        class Err(Exception):
            response = Resp()
        assert rlr.report_if_rate_limited(Err("plain failure")) is False

    def test_response_none_is_ignored(self, recorded):
        class Err(Exception):
            response = None
        assert rlr.report_if_rate_limited(Err("plain failure")) is False

    def test_detail_is_truncated_to_200_chars(self, recorded):
        rlr.report_if_rate_limited("429 " + "x" * 500)
        assert len(recorded[0]["detail"]) == 200

    def test_string_error_is_accepted(self, recorded):
        assert rlr.report_if_rate_limited("HTTP 429") is True

    def test_empty_string_and_zero_are_false(self, recorded):
        assert rlr.report_if_rate_limited("") is False
        assert rlr.report_if_rate_limited(0) is False
