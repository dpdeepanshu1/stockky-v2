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
