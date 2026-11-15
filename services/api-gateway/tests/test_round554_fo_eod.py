"""Round 554 (E579, P5): GET /market/fo-eod - row checking, build-up label recomputed, dates, market-data errors, switch. No network: market-data is faked.
Works with or without fastapi / httpx installed (small stand-ins are put in place only when the real ones are missing)."""
import asyncio
import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_SRC = os.path.join(_SERVICE, "gateway_routers", "fo_eod.py")


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
    spec = importlib.util.spec_from_file_location("fo_eod_gw_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in saved:
        sys.modules.pop(name, None)
    return mod


fe = _load()


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def _row(sym="AAA", **kw):
    r = {"symbol": sym, "near_expiry": "2026-11-24", "days_to_expiry": 11, "fut_close": 101.0, "price_chg_pct": 2.02, "oi": 1300.0, "oi_chg": 100.0,
         "oi_chg_pct": 8.33, "buildup": "long_buildup", "spot": 100.0, "basis_pct": 1.0, "annualised_pct": 33.2, "roll_pct": 23.1, "rolling": False}
    r.update(kw)
    return r


def _pack(rows, td="2026-11-13", ex="2026-11-13", **kw):
    b = {"enabled": True, "trade_date": td, "expected_date": ex, "stale": False, "rows": rows, "counts": {}, "fetched_at": "2026-11-15T13:30:00+05:30"}
    b.update(kw)
    return b


def _call(monkeypatch, resp=None, exc=None):
    def fake_get(url, **kw):
        assert url.endswith("/fo-eod"), url
        if exc:
            raise exc
        return resp
    monkeypatch.setattr(fe.httpx, "get", fake_get, raising=False)
    return asyncio.run(fe.market_fo_eod())


def test_good_rows_pass_with_their_trade_date_and_counts(monkeypatch):
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA"), _row("BBB", price_chg_pct=-1.0, oi_chg_pct=-3.0, buildup="long_unwinding")])))
    assert out["trade_date"] == "2026-11-13" and out["stale"] is False and out["enabled"] is True and "error" not in out
    assert [r["symbol"] for r in out["rows"]] == ["AAA", "BBB"]
    assert out["counts"]["long_buildup"] == 1 and out["counts"]["long_unwinding"] == 1 and out["counts"]["stocks"] == 2
    assert out["now_ist"].endswith("+05:30") and len(out["today_ist"]) == 10


def test_the_label_is_recomputed_from_the_numbers_it_sits_beside(monkeypatch):
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA", buildup="short_buildup"), _row("BBB", price_chg_pct=0.0), _row("CCC", oi_chg_pct=None)])))
    by = {r["symbol"]: r for r in out["rows"]}
    assert by["AAA"]["buildup"] == "long_buildup"            # a wrong label upstream cannot survive: price up and OI up
    assert by["BBB"]["buildup"] is None and by["CCC"]["buildup"] is None


def test_bad_numbers_become_none_never_zero(monkeypatch):
    bad = _row("AAA", price_chg_pct=float("nan"), oi="x", oi_chg_pct=float("inf"), fut_close=0, spot=-5, basis_pct=1.0, roll_pct=250)
    out = _call(monkeypatch, _Resp(200, _pack([bad])))
    r = out["rows"][0]
    assert r["price_chg_pct"] is None and r["oi"] is None and r["oi_chg_pct"] is None and r["fut_close"] is None and r["roll_pct"] is None
    assert r["spot"] is None and r["basis_pct"] is None and r["annualised_pct"] is None and r["buildup"] is None     # no usable spot -> no basis
    assert out["counts"]["no_basis"] == 1


def test_implausible_moves_and_basis_are_dropped_to_none(monkeypatch):
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA", price_chg_pct=75.0), _row("BBB", basis_pct=40.0, annualised_pct=999.0)])))
    by = {r["symbol"]: r for r in out["rows"]}
    assert by["AAA"]["price_chg_pct"] is None and by["AAA"]["buildup"] is None
    assert by["BBB"]["basis_pct"] is None and by["BBB"]["annualised_pct"] is None


def test_rows_without_a_real_symbol_are_dropped_and_unknown_dates_give_no_rows(monkeypatch):
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA"), _row("bad symbol!"), "junk", {"oi": 1}, _row("")])))
    assert [r["symbol"] for r in out["rows"]] == ["AAA"]
    for td in (None, "", "13 Nov", "2026-13"):
        out = _call(monkeypatch, _Resp(200, _pack([_row("AAA")], td=td)))
        assert out["rows"] == [] and out["trade_date"] is None          # no number without a time


def test_stale_pack_keeps_its_own_date_and_carries_the_error(monkeypatch):
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA")], td="2026-11-12", ex="2026-11-13", error="no newer file yet")))
    assert out["stale"] is True and out["trade_date"] == "2026-11-12" and out["expected_date"] == "2026-11-13" and out["error"] == "no newer file yet"
    out = _call(monkeypatch, _Resp(200, _pack([_row("AAA")], error="old error text")))
    assert "error" not in out                                           # a current pack does not show a stale error


def test_market_data_down_or_odd_answers_give_an_empty_set_with_the_reason(monkeypatch):
    out = _call(monkeypatch, exc=RuntimeError("down"))
    assert out["rows"] == [] and out["trade_date"] is None and out["error"] == "market-data unreachable"
    out = _call(monkeypatch, _Resp(503, {}))
    assert out["rows"] == [] and "503" in out["error"]
    for body in (None, [], {"rows": None}, {"trade_date": "2026-11-13", "rows": None}, {"trade_date": "2026-11-13", "rows": "x"}):
        out = _call(monkeypatch, _Resp(200, body))
        assert out["rows"] == []


def test_row_cap(monkeypatch):
    rows = [_row(f"S{i}") for i in range(fe.MAX_ROWS + 50)]
    out = _call(monkeypatch, _Resp(200, _pack(rows)))
    assert len(out["rows"]) == fe.MAX_ROWS


def test_switch_off_asks_nobody(monkeypatch):
    monkeypatch.setenv("FO_EOD_ENABLED", "0")
    def boom(*a, **k):
        raise AssertionError("market-data must not be called when the pack is off")
    monkeypatch.setattr(fe.httpx, "get", boom, raising=False)
    out = asyncio.run(fe.market_fo_eod())
    assert out["enabled"] is False and out["rows"] == [] and out["trade_date"] is None


def test_route_is_read_only_public_and_wired_into_main():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    assert "fo_eod_router" in src and "app.include_router(_fo_eod_router)" in src
    mod_src = open(_SRC, encoding="utf-8").read()
    assert '@fo_eod_router.get("/market/fo-eod")' in mod_src
    assert not any(w in mod_src for w in (".post(", ".put(", ".delete(", ".patch("))
    guard = open(os.path.join(_SERVICE, "admin_guard.py"), encoding="utf-8").read()
    assert "/market/fo-eod" not in guard
