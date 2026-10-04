"""2026-10-04: Render-era wake pings are off on the always-on VM; closed-market movers survive a restart."""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kv_cache  # noqa: E402
import main as m  # noqa: E402


def test_movers_last_known_key_is_durable():
    assert any("stockky:market_movers_last_known".startswith(p) for p in kv_cache._DURABLE_PREFIXES)
    assert m.MARKET_MOVERS_LAST_KNOWN == "stockky:market_movers_last_known"


def test_wake_rule(monkeypatch):
    monkeypatch.delenv("WAKE_PINGS", raising=False)
    monkeypatch.delenv("ORACLE_DSN", raising=False)
    assert m._wake_pings_enabled() is True              # Render/Neon: unchanged
    monkeypatch.setenv("ORACLE_DSN", "stockkydb_tp")
    assert m._wake_pings_enabled() is False             # Oracle VM: off
    monkeypatch.setenv("WAKE_PINGS", "1")
    assert m._wake_pings_enabled() is True
    monkeypatch.setenv("WAKE_PINGS", "0")
    monkeypatch.delenv("ORACLE_DSN")
    assert m._wake_pings_enabled() is False


class _Boom:
    async def get(self, *a, **k):
        raise AssertionError("no network call expected")


def test_warm_upstream_services_makes_no_calls_when_disabled(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "0")
    asyncio.run(m._warm_upstream_services(_Boom()))


def test_wake_required_services_skips_and_keeps_shape(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "0")
    out = asyncio.run(m._wake_required_services(_Boom()))
    assert set(out) == set(m.SYSTEM_SERVICES)
    assert all(v["ok"] is True and v["skipped"] is True for v in out.values())


# ── group101 item 7: the remaining gateway warm sites ───────────────────────

class _Resp:
    status_code = 404

    def json(self):
        return {}


class _Recorder:
    def __init__(self):
        self.calls = []

    async def get(self, url, *a, **k):
        self.calls.append((url, k.get("params")))
        return _Resp()


def test_ops_keepalive_deep_skips_when_pings_off(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "0")
    rec = _Recorder()
    monkeypatch.setattr(m, "_get_http_client", lambda: rec)
    out = asyncio.run(m.ops_keepalive(deep=True))
    assert out["ok"] is True and out["services"] == {} and out.get("skipped") is True
    assert rec.calls == []


def test_ops_keepalive_deep_still_pings_when_on(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "1")
    rec = _Recorder()
    monkeypatch.setattr(m, "_get_http_client", lambda: rec)
    monkeypatch.setattr(m, "SYSTEM_SERVICES", {"x": {"url": "http://x"}})
    out = asyncio.run(m.ops_keepalive(deep=True))
    assert "skipped" not in out and out["services"] == {"x": False}
    assert rec.calls == [("http://x/health", {"warm": "true"})]


def test_market_history_sends_no_warm_ping_when_off(monkeypatch):
    monkeypatch.setenv("WAKE_PINGS", "0")
    rec = _Recorder()
    monkeypatch.setattr(m, "_get_http_client", lambda: rec)
    try:
        asyncio.run(m.market_history("TCS", "1mo"))
    except Exception:
        pass  # downstream fallbacks are not under test; only the ping is
    assert not any(p == {"warm": "true"} for _, p in rec.calls)
    assert any("/history/TCS" in u for u, _ in rec.calls)
