"""Offline harness for technical/main.py::analyze (group368, item 7): freezes the classic /analyze field set.

fastapi / httpx are stubbed ONLY when they are not installed. History, cache and quote lookups are patched so every call is a pure function
of a seeded synthetic daily frame. _STRUCTURE_STALE_DAYS is raised so old synthetic frames never flip the structure block to "stale".
Exposes load_main, run, cases, frame, legacy_fields, NEW_KEYS, OVERRIDES.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import random
import sys
import types

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PATH = os.path.join(HERE, "technical", "main.py")
if HERE not in sys.path:
    sys.path.insert(0, HERE)

NEW_KEYS = ("market_structure", "structure_summary", "structure_truncated")

# non-default threshold / weight overrides (each one must bypass the plain per-symbol cache)
OVERRIDES = [
    {"rsi_oversold": 35.0}, {"rsi_overbought": 65.0}, {"extended_1m_pct": 0.12}, {"extended_short_pct": 0.03},
    {"trend_weight": 1.3}, {"meanrev_weight": 0.7}, {"trend_weight": 0.8, "meanrev_weight": 1.2},
    {"rsi_oversold": 40.0, "rsi_overbought": 60.0, "extended_1m_pct": 0.1, "extended_short_pct": 0.02},
]

_END = pd.Timestamp("2026-09-30")


def _stub_modules():
    if importlib.util.find_spec("fastapi") is None:
        fa = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code=500, detail=None):
                super().__init__(detail)
                self.status_code, self.detail = status_code, detail

        class FastAPI:
            def __init__(self, *a, **k):
                pass

            def add_middleware(self, *a, **k):
                pass

            def _dec(self, *a, **k):
                return lambda f: f

            get = post = put = delete = on_event = middleware = exception_handler = _dec

        fa.FastAPI, fa.HTTPException = FastAPI, HTTPException
        for n in ("Request", "Response", "Query", "Depends", "BackgroundTasks", "Header"):
            setattr(fa, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
        mw = types.ModuleType("fastapi.middleware")
        cors = types.ModuleType("fastapi.middleware.cors")
        cors.CORSMiddleware = type("CORSMiddleware", (), {})
        sys.modules.update({"fastapi": fa, "fastapi.middleware": mw, "fastapi.middleware.cors": cors})
    if importlib.util.find_spec("httpx") is None:
        hx = types.ModuleType("httpx")

        class _Boom(Exception):
            pass

        def _no_net(*a, **k):
            raise _Boom("network disabled in the offline harness")

        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            get = post = _no_net

        hx.get = hx.post = _no_net
        hx.Client = Client
        hx.AsyncClient = Client
        for n in ("HTTPError", "TimeoutException", "ConnectError", "ReadTimeout", "HTTPStatusError", "RequestError"):
            setattr(hx, n, type(n, (Exception,), {}))
        hx.Timeout = lambda *a, **k: None
        sys.modules["httpx"] = hx


_MAIN = None


def load_main():
    """Import technical/main.py once, with the offline patches applied."""
    global _MAIN
    if _MAIN is not None:
        return _MAIN
    _stub_modules()
    spec = importlib.util.spec_from_file_location("_technical_main_offline", MAIN_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m._STRUCTURE_STALE_DAYS = 36500
    m._cache_get = lambda key: None
    m._cache_set = lambda key, value, ttl=None: None
    m._fetch_quote_price = lambda symbol: None
    if hasattr(m, "_wake_pings_enabled"):
        m._wake_pings_enabled = lambda: False
    _MAIN = m
    return m


def frame(kind: str, seed: int, n: int = 260) -> pd.DataFrame:
    """Seeded synthetic daily OHLCV frame (Open/High/Low/Close/Volume, daily DatetimeIndex ending 2026-09-30)."""
    r = random.Random(seed * 1009 + sum(map(ord, kind)))
    drift, vol = {"walk": (0.0, 1.1), "up": (0.25, 0.9), "down": (-0.25, 0.9), "range": (0.0, 0.6), "spike": (0.05, 1.6)}[kind]
    px, rows = 100.0, []
    for i in range(n):
        o = px
        pull = -0.08 * (px - 100.0) if kind == "range" else 0.0
        c = max(5.0, o + drift + pull + r.gauss(0, vol) + ((r.choice((-1, 1)) * r.uniform(3, 6)) if kind == "spike" and r.random() < 0.05 else 0.0))
        h = max(o, c) + abs(r.gauss(0, vol / 3))
        lo = min(o, c) - abs(r.gauss(0, vol / 3))
        v = 100000.0 * (1 + r.random()) * (2.5 if i == n - 1 and seed % 3 == 0 else 1.0)
        rows.append((round(o, 4), round(h, 4), round(lo, 4), round(c, 4), round(v, 2)))
        px = c
    idx = pd.date_range(end=_END, periods=n, freq="D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def cases() -> list[dict]:
    """35 cases: 25 plain daily frames + 8 override variants + 2 thin-history paths."""
    out = []
    for kind, seeds in (("walk", range(8)), ("up", range(5)), ("down", range(5)), ("range", range(4)), ("spike", range(3))):
        for s in seeds:
            out.append({"name": f"{kind}{s}", "kind": kind, "seed": s, "n": 260, "params": {}})
    for i, ov in enumerate(OVERRIDES):
        out.append({"name": f"override{i}", "kind": "walk", "seed": 20 + i, "n": 260, "params": dict(ov)})
    out.append({"name": "thin_with_quote", "kind": None, "seed": 0, "n": 3, "params": {}, "quote": 123.45})
    out.append({"name": "thin_no_quote", "kind": None, "seed": 0, "n": 3, "params": {}, "quote": None})
    return out


def run(case: dict, structure: bool = True, lite: bool = False, main=None) -> dict:
    """Call analyze() for one case, offline. Returns a JSON-normalised copy of the result."""
    m = main or load_main()
    df = None if case.get("kind") is None else frame(case["kind"], case["seed"], case["n"])
    if case.get("kind") is None:
        df = frame("walk", 0, case["n"])
    m._fetch_history = lambda symbol, force=False: df
    m._fetch_quote_price = lambda symbol: case.get("quote")
    prev = os.environ.get("STRUCTURE_ENABLED")
    os.environ["STRUCTURE_ENABLED"] = "1" if structure else "0"
    try:
        res = m.analyze(f"TEST{case['name'].upper()}", force=True, lite=lite, **case.get("params", {}))
    finally:
        if prev is None:
            os.environ.pop("STRUCTURE_ENABLED", None)
        else:
            os.environ["STRUCTURE_ENABLED"] = prev
    return json.loads(json.dumps(copy.deepcopy(res), default=str))


def legacy_fields(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in NEW_KEYS}
