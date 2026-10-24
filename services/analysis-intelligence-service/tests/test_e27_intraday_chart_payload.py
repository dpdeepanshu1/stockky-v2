"""E27: the technical service's intraday chart context (1m / 5m / 15m / 30m / 1h): fetch plan, closed vs forming bar, fail-open answers, cache TTL.
The helper block of technical/main.py is exec'd without fastapi / pandas; market-data is a fake."""
from __future__ import annotations

import datetime as _dt
import logging
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
SRC = open(os.path.join(HERE, "technical", "main.py"), encoding="utf-8").read()

from stockky_ms import timeframes as TF            # noqa: E402
from stockky_ms.chart_context import chart_context  # noqa: E402

IST = 19800


class _Err(Exception):
    pass


class _HTTPX:
    HTTPError = _Err


class _Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._b, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("no json")
        return self._b


def _load(open_=True, md=None):
    a = SRC.index("# E27: intraday candles.")
    b = SRC.index("def _structure_enabled")
    import time
    calls = []

    def md_get(url, **kw):
        calls.append((url, kw))
        r = (md or {}).get("resp", _Resp(200, {"candles": []}))
        if isinstance(r, Exception):
            raise r
        return r

    ns = {"time": time, "httpx": _HTTPX, "logger": logging.getLogger("t"), "_md_get": md_get, "_exc_detail": lambda e: str(e), "MARKET_DATA_URL": "http://md:8001",
          "is_market_open": lambda: open_, "_ms_chart_context": chart_context, "_ms_tf": TF, "_calls": calls}
    exec(compile(SRC[a:b], "e27", "exec"), ns)
    return ns


def _ts(day, hhmm):
    d = _dt.datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=_dt.timezone.utc)
    return d.timestamp() - IST


def _rows(day, n, step_min=1, px=100.0):
    t0 = _ts(day, "09:15")
    out = []
    for k in range(n):
        ts = t0 + k * step_min * 60
        lab = _dt.datetime.fromtimestamp(ts + IST, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
        o = px + 0.02 * k + (0.3 if k % 3 == 0 else -0.2)
        out.append({"date": lab, "open": o, "high": o + 0.4, "low": o - 0.4, "close": o + 0.1, "volume": 100 + k})
    return out


def _days(n):
    ds, d = [], _dt.date(2026, 10, 22)
    while len(ds) < n:
        if d.weekday() < 5:
            ds.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return sorted(ds)


def test_payload_for_every_intraday_timeframe():
    ns = _load()
    now = _ts("2026-10-22", "10:07") + 20
    for tf, step, per_day in (("5m", 5, 75), ("15m", 15, 25), ("1h", 60, 7)):
        rows = [r for d in _days(12) for r in _rows(d, per_day if d != "2026-10-22" else 8, step)]
        out = ns["_intraday_chart_payload"]("INFY", tf, 120, "bullish", rows=rows, now=now)
        assert out["ok"] and out["tf"] == tf and out["symbol"] == "INFY" and out["unit"] == "candle", tf
        assert out["regime"]["structure"] == "bullish" and any(b["label"] == "Daily structure: up" for b in out["badges"])
        assert all(len(b[0]) == 16 for b in out["bars"]) and len(out["bars"]) <= 120


def test_the_forming_bar_is_live_and_never_a_closed_bar_and_the_30m_is_built_from_15m_rows():
    ns = _load()
    rows = [r for d in _days(12) for r in _rows(d, 25 if d != "2026-10-22" else 3, 15)]
    out = ns["_intraday_chart_payload"]("INFY", "30m", 200, None, rows=rows, now=_ts("2026-10-22", "10:00") - 60)
    assert out["ok"] and out["live"] and out["live"][0] == "2026-10-22 09:45"
    assert out["bars"][-1][0] == "2026-10-22 09:15" and all(b[0] != out["live"][0] for b in out["bars"])


def test_lag_is_reported_only_while_the_market_is_open():
    rows = [r for d in _days(12) for r in _rows(d, 75 if d != "2026-10-22" else 20, 5)]
    now = _ts("2026-10-22", "11:30")
    assert _load(open_=True)["_intraday_chart_payload"]("INFY", "5m", 100, None, rows=rows, now=now)["data_lag_s"] > 0
    assert _load(open_=False)["_intraday_chart_payload"]("INFY", "5m", 100, None, rows=rows, now=now)["data_lag_s"] is None


def test_every_miss_is_a_200_style_answer_with_a_reason_not_an_exception():
    ns = _load()
    f = ns["_intraday_chart_payload"]
    for tf, rows, why in (("5m", [], "thin"), ("5m", _rows("2026-10-22", 3, 5), "thin"), ("1d", _rows("2026-10-22", 50, 5), "bad_tf"), ("7m", _rows("2026-10-22", 50, 5), "bad_tf")):
        out = f("INFY", tf, 100, None, rows=rows, now=_ts("2026-10-22", "15:40"))
        assert out["ok"] is False and out["reason"] == why and out["bars"] == [] and out["schema"] == 1 and out["live"] is None, (tf, why)
    ns2 = _load()
    ns2["_ms_chart_context"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    assert ns2["_intraday_chart_payload"]("INFY", "5m", 100, None, rows=[r for d in _days(6) for r in _rows(d, 75, 5)])["reason"] == "error"
    ns3 = _load()
    ns3["_ms_chart_context"] = None
    assert ns3["_intraday_chart_payload"]("INFY", "5m", 100, None, rows=[])["reason"] == "engine_off"


def test_fetch_asks_market_data_for_the_right_interval_period_and_forces_fresh():
    ns = _load(md={"resp": _Resp(200, {"candles": _rows("2026-10-22", 5, 5), "source": "dhan"})})
    rows, src = ns["_fetch_intraday_rows"]("INFY", "5m", "1mo")
    assert len(rows) == 5 and src == "dhan"
    url, kw = ns["_calls"][0]
    assert url == "http://md:8001/history/INFY" and kw["params"] == {"period": "1mo", "interval": "5m", "force": "true"} and kw["timeout"] <= 25
    assert ns["_fetch_intraday_rows"]("INFY", "5m", "1mo", force=False)[0] is not None and ns["_calls"][1][1]["params"]["force"] == "false"


def test_the_payload_builder_requests_the_timeframes_own_interval_and_period():
    ns = _load(md={"resp": _Resp(200, {"candles": []})})
    for tf, iv, per in (("1m", "1m", "5d"), ("5m", "5m", "1mo"), ("15m", "15m", "1mo"), ("30m", "15m", "1mo"), ("1h", "1h", "3mo")):
        ns["_calls"].clear()
        out = ns["_intraday_chart_payload"]("INFY", tf, 100, None)
        assert out["ok"] is False and out["reason"] == "thin"
        assert ns["_calls"][0][1]["params"]["interval"] == iv and ns["_calls"][0][1]["params"]["period"] == per, tf


def test_fetch_failures_have_a_reason():
    for resp, why in ((_Resp(404, {}), "no_data"), (_Resp(503, {}), "http_503"), (_Resp(200, None, bad_json=True), "bad_json"), (_Resp(200, {"candles": []}), "thin"),
                      (_Resp(200, ["x"]), "thin"), (_Err("down"), "transient")):
        rows, reason = _load(md={"resp": resp})["_fetch_intraday_rows"]("INFY", "5m", "1mo")
        assert rows is None and reason == why, why


def test_cache_ttl_is_short_while_open_and_long_when_closed():
    o, c = _load(open_=True), _load(open_=False)
    assert [o["_intraday_ctx_ttl"](t) for t in ("1m", "5m", "15m", "30m", "1h")] == [20, 45, 90, 90, 180]
    assert all(c["_intraday_ctx_ttl"](t) == 600 for t in ("1m", "1h"))


def test_endpoint_wiring():
    a = SRC.index('@app.get("/chart-context/{symbol}")')
    body = SRC[a:SRC.index('if __name__ == "__main__":')]
    assert 'tf: str = "1d"' in body and "tail: int = None" in body
    assert 'status_code=400' in body and "tf must be one of 1m, 5m, 15m, 30m, 1h, 1d" in body
    assert 'key = f"tech_chart_ctx:{sym}:{tail}" if tf == "1d" else f"tech_chart_ctx:{sym}:{tf}:{tail}"' in body
    assert body.index("if spec[\"intraday\"]:") < body.index("df = _fetch_history(sym, force=force)")
    assert "raise HTTPException(status_code=503, detail=f\"{tf}" not in body


def test_the_daily_fibonacci_leg_rides_along_on_intraday_charts_and_is_none_without_it():
    ns = _load()
    now = _ts("2026-10-22", "10:07") + 20
    rows = [r for d in _days(12) for r in _rows(d, 25 if d != "2026-10-22" else 3, 15)]
    fib = {"swing_low": 100.0, "swing_high": 200.0, "direction": "bullish", "retracement": 0.55, "in_golden_zone": True, "leg_invalidated": False,
           "ext_1272": None, "ext_1618": None}
    out = ns["_intraday_chart_payload"]("INFY", "15m", 100, "bullish", rows=rows, now=now, daily_fib=fib)
    assert out["ok"] and out["fib"]["source"] == "daily" and out["fib"]["golden"] == {"low": 138.2, "high": 150.0}
    assert ns["_intraday_chart_payload"]("INFY", "15m", 100, "bullish", rows=rows, now=now)["fib"] is None
    assert ns["_intraday_unavailable"]("INFY", "5m", "thin")["fib"] is None
