"""Round 553 (E578, P4): GET /market/global-cues - row cleaning, missing list, market-data errors, switch. No network: market-data is faked.
Works with or without fastapi / httpx installed (small stand-ins are put in place only when the real ones are missing)."""
import asyncio
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_SRC = os.path.join(_SERVICE, "gateway_routers", "global_cues.py")


def _load():
    saved = {}
    for name in ("fastapi", "httpx"):
        try:
            __import__(name)
        except ImportError:
            saved[name] = None
            m = types.ModuleType(name)
            if name == "fastapi":
                class APIRouter:
                    def get(self, *a, **k):
                        return lambda f: f
                m.APIRouter = APIRouter
            else:
                m.get = lambda *a, **k: None
            sys.modules[name] = m
    spec = importlib.util.spec_from_file_location("global_cues_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in saved:
        sys.modules.pop(name, None)
    return mod


gc = _load()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def _row(price=6000.0, pct=0.5, bar="2026-11-14", prev=5970.0):
    return {"price": price, "previous_close": prev, "day_change_pct": pct, "bar_date": bar, "source": "yahoo_global"}


def _call(monkeypatch, resp=None, exc=None):
    def fake_get(url, **kw):
        assert url.endswith("/global-strip"), url
        if exc:
            raise exc
        return resp
    monkeypatch.setattr(gc.httpx, "get", fake_get, raising=False)
    return asyncio.run(gc.market_global_cues())


def test_good_rows_pass_and_the_rest_are_listed_missing(monkeypatch):
    out = _call(monkeypatch, _Resp(200, {"rows": {"^GSPC": _row(), "GC=F": _row(2500.0, -1.2)}, "missing": []}))
    assert set(out["rows"]) == {"^GSPC", "GC=F"} and out["missing"] == ["^IXIC", "BZ=F", "DX-Y.NYB", "INR=X"]
    assert out["rows"]["^GSPC"]["bar_date"] == "2026-11-14" and out["enabled"] is True and "error" not in out
    assert len(out["today_ist"]) == 10 and out["fetched_at"].endswith("+05:30")


def test_unusable_rows_are_dropped_not_repaired(monkeypatch):
    rows = {"^GSPC": _row(price=0), "^IXIC": _row(pct=float("nan")), "BZ=F": _row(pct=40.0), "DX-Y.NYB": _row(bar=None),
            "INR=X": "junk", "GC=F": _row(price="x"), "RELIANCE.NS": _row()}
    out = _call(monkeypatch, _Resp(200, {"rows": rows}))
    assert out["rows"] == {} and out["missing"] == list(gc.SYMBOLS)          # zero, NaN, a 40 % move, no date, junk, non-number: all dashes; an unknown symbol never passes


def test_market_data_down_or_odd_answers_give_an_empty_set_with_the_reason(monkeypatch):
    out = _call(monkeypatch, exc=RuntimeError("down"))
    assert out["rows"] == {} and out["missing"] == list(gc.SYMBOLS) and out["error"] == "market-data unreachable"
    out = _call(monkeypatch, _Resp(503, {}))
    assert out["rows"] == {} and "503" in out["error"]
    for body in (None, [], {"rows": []}, {"rows": None}):
        out = _call(monkeypatch, _Resp(200, body))
        assert out["rows"] == {} and out["missing"] == list(gc.SYMBOLS)


def test_switch_off_asks_nobody(monkeypatch):
    monkeypatch.setenv("GLOBAL_STRIP_ENABLED", "0")
    def boom(*a, **k):
        raise AssertionError("market-data must not be called when the strip is off")
    monkeypatch.setattr(gc.httpx, "get", boom, raising=False)
    out = asyncio.run(gc.market_global_cues())
    assert out["enabled"] is False and out["rows"] == {} and out["missing"] == list(gc.SYMBOLS)


def test_route_is_read_only_public_and_wired_into_main():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    assert "global_cues_router" in src and "app.include_router(_global_cues_router)" in src
    mod_src = open(_SRC, encoding="utf-8").read()
    assert "@global_cues_router.get(\"/market/global-cues\")" in mod_src
    assert not any(w in mod_src for w in (".post(", ".put(", ".delete(", ".patch("))
    guard = open(os.path.join(_SERVICE, "admin_guard.py"), encoding="utf-8").read()
    assert "/market/global-cues" not in guard                                # a public read like /market/sparklines
