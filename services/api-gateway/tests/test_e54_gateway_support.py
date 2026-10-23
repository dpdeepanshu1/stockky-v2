"""E54: gateway_support hooks - router modules reach main-owned helpers through call-time trampolines (no import of main, patches on main still work)."""
import asyncio

import pytest

import gateway_support


def test_bind_rejects_unknown_hooks():
    with pytest.raises(KeyError):
        gateway_support.bind(not_a_hook=1)


def test_unbound_defaults_fail_loudly_or_do_nothing():
    # no importlib.reload here: the hooks live in the gateway_support package next to MainRef, and a reload would wipe main's binding for later tests
    with pytest.raises(RuntimeError):
        asyncio.run(gateway_support._unbound(None, "x", "http://x"))
    assert gateway_support._unbound_metric("m", 1) is None


def test_patching_main_cb_get_reaches_the_moved_route_helper(monkeypatch):
    import main
    from gateway_routers import sniper
    seen = {}

    async def fake(client, name, url, timeout=5.0, **kw):
        seen.update(name=name, url=url)
        return "fake-response"

    monkeypatch.setattr(main, "_cb_get", fake)
    out = asyncio.run(sniper._cb_get(None, "technical", "http://t/structure/X", timeout=1.0))
    assert out == "fake-response" and seen == {"name": "technical", "url": "http://t/structure/X"}


def test_patching_main_structure_metric_reaches_the_moved_helper(monkeypatch):
    import main
    from gateway_routers import sniper
    calls = []
    monkeypatch.setattr(main, "_structure_metric", lambda *a, **k: calls.append((a, k)))
    sniper._structure_metric("m", 2, outcome="ok")
    assert calls == [(("m", 2), {"outcome": "ok"})]


def test_technical_url_is_main_s_value():
    import main
    from gateway_routers import sniper
    assert sniper.TECHNICAL_URL == main.TECHNICAL_URL and sniper.TECHNICAL_URL


def test_main_binds_before_importing_the_sniper_module():
    src = open("main.py", encoding="utf-8").read()
    assert src.index("gateway_support.bind(") < src.index("from gateway_routers.sniper import")
