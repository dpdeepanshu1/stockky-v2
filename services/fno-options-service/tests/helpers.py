"""Synthetic data for the F&O tests: a believable Nifty chain and index candles. No network, no files."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import List

from fno import bs
from fno.models import Chain


def make_chain(spot: float = 24500.0, expiry: str = "2026-11-17", underlying: str = "NIFTY", step: float = 50.0, n_each: int = 8, iv: float = 0.14,
               now: datetime | None = None, lot: int = 65, pe_oi_bias: float = 1.0, fetched_at: float = 1_000_000.0) -> Chain:
    """Strikes around spot priced with Black-Scholes at `iv`; OI peaks away from the money; a tight bid/ask around the theoretical price."""
    now = now or datetime(2026, 11, 10, 10, 30)
    from datetime import date
    t = bs.year_fraction(now, date.fromisoformat(expiry))
    atm = round(spot / step) * step
    rows = []
    for i in range(-n_each, n_each + 1):
        k = atm + i * step
        row = {"strike": k}
        for ot in ("CE", "PE"):
            px = max(0.05, bs.price(spot, k, t, 0.065, iv, ot))
            g = bs.greeks(spot, k, t, 0.065, iv, ot)
            half = max(0.05, px * 0.004)
            oi = 200000 * math.exp(-abs(i) / 3.0) * (pe_oi_bias if ot == "PE" else 1.0)
            row[ot.lower()] = {"security_id": 1000 + (i + 20) * 2 + (0 if ot == "CE" else 1), "ltp": round(px, 2), "iv": iv * 100, "delta": g["delta"],
                               "gamma": g["gamma"], "theta": g["theta"], "vega": g["vega"], "oi": oi, "prev_oi": oi * 0.97, "volume": 50000 * math.exp(-abs(i) / 3.0),
                               "prev_volume": 40000, "prev_close": round(px * 0.98, 2), "bid": round(px - half, 2), "bid_qty": 650, "ask": round(px + half, 2),
                               "ask_qty": 650}
        rows.append(row)
    return Chain.from_json({"underlying": underlying, "expiry": expiry, "spot": spot, "strikes": rows, "fetched_at": fetched_at})


def make_daily(n: int = 120, start: float = 22000.0, drift: float = 0.0015, wiggle: float = 0.006, t0: int = 1_700_000_000) -> List[dict]:
    """Daily candles with a steady drift (up if drift > 0) and a deterministic wiggle."""
    out, px = [], start
    for i in range(n):
        w = math.sin(i * 0.9) * wiggle
        o = px
        c = px * (1 + drift + w * 0.5)
        h = max(o, c) * (1 + 0.003 + abs(w) * 0.3)
        l = min(o, c) * (1 - 0.003 - abs(w) * 0.3)
        out.append({"t": t0 + i * 86400, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2), "c": round(c, 2), "v": 1e6})
        px = c
    return out


def make_intraday(n: int = 40, start: float = 24400.0, step: float = 6.0, t0: int = 1_700_000_000) -> List[dict]:
    """5-minute candles trending by `step` points per bar (negative step = falling)."""
    out, px = [], start
    for i in range(n):
        o = px
        c = px + step
        out.append({"t": t0 + i * 300, "o": o, "h": max(o, c) + 2, "l": min(o, c) - 2, "c": c, "v": 1000 + i})
        px = c
    return out
