"""backtest/replay.py - replay RECORDED option-chain snapshots through the real fno.engine.Engine (the live code path: gates, signal, contract choice, sizing, fills, exits, costs).

The recorder (fno/recorder.py) saves a chain every minute while the engine runs with FNO_RECORD_SNAPSHOTS on. This module feeds those snapshots back, in time order, to a fresh in-memory
Engine with a replay Provider and an injected clock, then reads the closed trades. Daily candles for the direction logic come from the caller (a CSV or market-data); the intraday bars
are built from the recorded spot. Reliability: real premiums, real bids and asks; limited to the days that were recorded."""
from __future__ import annotations

import copy
from datetime import datetime, time as dtime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence

import config as live_config
from fno import engine as eng, recorder, store as st
from fno.models import Chain
from fno.params import Params
from fno.provider import IST, Provider
from . import metrics


def make_cfg(overrides: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
    ns = SimpleNamespace(**{k: getattr(live_config, k) for k in dir(live_config) if k.isupper()})
    ns.FNO_ENGINE_ENABLED, ns.FNO_MODE = True, "DEMO"
    for k, v in (overrides or {}).items():
        setattr(ns, k, v)
    return ns


class ReplayProvider(Provider):
    def __init__(self, snaps: Dict[str, List[Dict[str, Any]]], daily: Dict[str, Sequence[dict]], vix_default: float = 15.0):
        self.snaps, self.daily_by, self.vix_default = snaps, daily, vix_default
        self.ts = 0.0

    def set_time(self, ts: float) -> None:
        self.ts = ts

    def _upto(self, und: str) -> List[Dict[str, Any]]:
        return [s for s in self.snaps.get(und, []) if s["ts"] <= self.ts]

    def expiries(self, und: str) -> List[str]:
        return sorted({s["chain"]["expiry"] for s in self._upto(und)})

    def chain(self, und: str, expiry: str) -> Optional[Chain]:
        for s in reversed(self._upto(und)):
            if s["chain"]["expiry"] == expiry:
                c = Chain.from_json(s["chain"])
                c.fetched_at = s["ts"]
                return c
        return None

    def daily(self, und: str):
        """Daily candles of the days BEFORE the replayed day only (same rule as backtest/sim.py). A daily candle is stamped at the start of its day, so the old cut-off
        `t < now - 6h` let TODAY's full-day candle in from 09:15 on: the strategies saw the day's final high / low / close before it happened (look-ahead, flattering every result)."""
        today = datetime.fromtimestamp(self.ts, IST).date()
        return [c for c in self.daily_by.get(und, []) if datetime.fromtimestamp(c["t"], IST).date() < today]

    def intraday(self, und: str):
        bars: Dict[int, Dict[str, float]] = {}
        for s in self._upto(und):
            sp = s["chain"].get("spot")
            if not sp:
                continue
            k = int(s["ts"] // 300) * 300
            b = bars.setdefault(k, {"t": float(k), "o": sp, "h": sp, "l": sp, "c": sp, "v": 0.0})
            b["h"], b["l"], b["c"] = max(b["h"], sp), min(b["l"], sp), sp
        return [bars[k] for k in sorted(bars)]

    def vix(self) -> Optional[float]:
        """The most RECENT recorded VIX (by time, across both underlyings; it used to be whichever underlying was iterated last)."""
        best_ts, last = -1.0, None
        for und in self.snaps:
            for s in self._upto(und):
                if s.get("vix") and s["ts"] >= best_ts:
                    best_ts, last = s["ts"], s["vix"]
        return last or self.vix_default

    def lookup(self, und, expiry, strike, opt_type):
        return None


def run(root: str, days: Sequence[str], daily_by_und: Dict[str, Sequence[dict]], overrides: Optional[Dict[str, Any]] = None, underlyings: Sequence[str] = ("NIFTY", "SENSEX"),
        holidays: Sequence[str] = (), vix_default: float = 15.0) -> Dict[str, Any]:
    cfg = make_cfg({"FNO_UNDERLYINGS": "BOTH" if len(underlyings) > 1 else underlyings[0], **(overrides or {})})
    p = Params.from_config(cfg)
    all_trades = []
    ran: List[str] = []
    warnings: List[str] = []
    for day in days:
        snaps = {u: list(recorder.read_day(root, day, u)) for u in underlyings}
        if not any(snaps.values()):
            continue
        ran.append(day)
        prov = ReplayProvider(snaps, {u: daily_by_und.get(u, []) for u in underlyings}, vix_default)
        db = st.Store(":memory:")
        e = eng.Engine(db, prov, cfg, holidays=tuple(holidays))
        stamps = sorted({s["ts"] for ss in snaps.values() for s in ss})
        # The intraday bars are BUILT from these snapshots. The opening-range strategies read "the first 15 / 30 minutes" from the first bar, so a recording that starts late, or has a hole,
        # silently changes what they see. Say so instead of hiding it.
        first = datetime.fromtimestamp(stamps[0], IST)
        if first.time() > dtime(9, 20):
            warnings.append(f"{day}: recording starts {first:%H:%M}, not at the open: the opening range the strategies use is wrong that day")
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a > 300.0]
        if gaps:
            warnings.append(f"{day}: {len(gaps)} gap(s) in the recording, longest {max(gaps) / 60:.0f} min: bars across a gap are not real bars")
        for ts in stamps:
            prov.set_time(ts)
            e.tick(datetime.fromtimestamp(ts, IST))
        if db.open_positions():                                         # whatever the recording did not cover is closed at its last known price
            prov.set_time(stamps[-1])
            e.squareoff_all(reason="END_OF_RECORDING", now=datetime.fromtimestamp(stamps[-1], IST))
        all_trades += db.positions(limit=5000)
        db.close()
    m = metrics.summarize(all_trades, p.capital)
    return {"kind": "REPLAY", "underlying": "+".join(underlyings), "from": ran[0] if ran else None, "to": ran[-1] if ran else None, "days": len(ran), "capital": p.capital,
            "params": p.to_json(), "warnings": warnings, "metrics": m, "verdict": metrics.verdict(m), "trades": [t.to_json() for t in all_trades],
            "notes": ["real recorded premiums and quotes through the live engine code",
                      "snapshots are about a minute apart: exits are decided on those minute marks, a real stop could have been hit sooner",
                      "positions still open when a day's recording ends are closed at END_OF_RECORDING",
                      "intraday bars are built from the snapshots (spot about once a minute), so a bar's high / low is thinner than a real 5-minute bar: range-based strategies see fewer expansion bars than live"
                      + (f"; {len(warnings)} day warning(s), see `warnings`" if warnings else "")] if ran else ["no recorded snapshots found for the requested days"]}
