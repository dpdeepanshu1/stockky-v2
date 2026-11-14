"""backtest/replay.py - replay RECORDED option-chain snapshots through the real fno.engine.Engine (the live code path: gates, signal, contract choice, sizing, fills, exits, costs).

The recorder (fno/recorder.py) saves a chain every minute while the engine runs with FNO_RECORD_SNAPSHOTS on. This module feeds those snapshots back, in time order, to a fresh in-memory
Engine with a replay Provider and an injected clock, then reads the closed trades. Daily candles for the direction logic come from the caller (a CSV or market-data); the intraday bars
are built from the recorded spot. Reliability: real premiums, real bids and asks; limited to the days that were recorded."""
from __future__ import annotations

import bisect
import copy
from datetime import datetime, time as dtime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence

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


class _SnapIndex:
    """Round 504 (E525): the snapshots of ONE underlying and day as small per-snapshot values (ts, spot, vix, expiry) plus a loader for the full snapshot. The provider asks 'what is the
    newest snapshot at time t' with bisect, so a replay tick no longer rebuilds a list of every snapshot so far (that was O(n^2)), and with a file loader a snapshot is parsed only when
    the engine asks for its chain, so the whole day is never in memory at once."""

    def __init__(self, metas: Sequence[tuple], loader: Callable[[int], Optional[Dict[str, Any]]]):
        self.ts = [m[0] for m in metas]
        self.spot = [m[1] for m in metas]
        self.vix = [m[2] for m in metas]
        self.exp = [m[3] for m in metas]
        self.load = loader
        self.by_exp: Dict[str, List[int]] = {}
        for i, e in enumerate(self.exp):
            if e is not None:
                self.by_exp.setdefault(e, []).append(i)
        self.vix_idx = [i for i, v in enumerate(self.vix) if v]
        self._cache: Dict[int, Optional[Dict[str, Any]]] = {}

    @classmethod
    def from_list(cls, snaps: Sequence[Dict[str, Any]]) -> "_SnapIndex":
        rows = sorted(snaps, key=lambda s: s["ts"])
        return cls([(s["ts"], (s.get("chain") or {}).get("spot"), s.get("vix"), (s.get("chain") or {}).get("expiry")) for s in rows], lambda i: rows[i])

    @classmethod
    def from_file(cls, root: str, day: str, und: str) -> "_SnapIndex":
        path, rows = recorder.index_day(root, day, und)
        return cls([(m[0], m[2], m[3], m[4]) for m in rows], lambda i: recorder.read_at(path, rows[i][1]))

    def count_upto(self, t: float) -> int:
        return bisect.bisect_right(self.ts, t)

    def snap(self, i: int) -> Optional[Dict[str, Any]]:
        if i not in self._cache:
            if len(self._cache) >= 4:
                self._cache.clear()
            self._cache[i] = self.load(i)
        return self._cache[i]

    def __len__(self) -> int:
        return len(self.ts)


class ReplayProvider(Provider):
    def __init__(self, snaps: Dict[str, Any], daily: Dict[str, Sequence[dict]], vix_default: float = 15.0):
        """snaps: {underlying: list of snapshot dicts} (tests) or {underlying: _SnapIndex} (replay.run, memory-light)."""
        self.ix: Dict[str, _SnapIndex] = {u: (v if isinstance(v, _SnapIndex) else _SnapIndex.from_list(v)) for u, v in snaps.items()}
        self.daily_by, self.vix_default = daily, vix_default
        self.ts = 0.0
        self._bars: Dict[str, Any] = {}

    def set_time(self, ts: float) -> None:
        self.ts = ts

    def expiries(self, und: str) -> List[str]:
        ix = self.ix.get(und)
        return sorted(e for e, idxs in ix.by_exp.items() if ix.ts[idxs[0]] <= self.ts) if ix else []

    def chain(self, und: str, expiry: str) -> Optional[Chain]:
        ix = self.ix.get(und)
        idxs = ix.by_exp.get(expiry) if ix else None
        if not idxs:
            return None
        pos = bisect.bisect_left(idxs, ix.count_upto(self.ts)) - 1       # the newest snapshot of this expiry at or before the replay time
        if pos < 0:
            return None
        s = ix.snap(idxs[pos])
        if s is None:
            return None
        c = Chain.from_json(s["chain"])
        c.fetched_at = s["ts"]
        return c

    def daily(self, und: str):
        """Daily candles of the days BEFORE the replayed day only (same rule as backtest/sim.py). A daily candle is stamped at the start of its day, so the old cut-off
        `t < now - 6h` let TODAY's full-day candle in from 09:15 on: the strategies saw the day's final high / low / close before it happened (look-ahead, flattering every result)."""
        today = datetime.fromtimestamp(self.ts, IST).date()
        return [c for c in self.daily_by.get(und, []) if datetime.fromtimestamp(c["t"], IST).date() < today]

    def intraday(self, und: str):
        ix = self.ix.get(und)
        if not ix:
            return []
        n = ix.count_upto(self.ts)
        done, bars = self._bars.get(und, (0, {}))
        if n < done:                                                   # time went backwards (a new day, a new run): start again
            done, bars = 0, {}
        for i in range(done, n):
            sp = ix.spot[i]
            if not sp:
                continue
            k = int(ix.ts[i] // 300) * 300
            b = bars.setdefault(k, {"t": float(k), "o": sp, "h": sp, "l": sp, "c": sp, "v": 0.0})
            b["h"], b["l"], b["c"] = max(b["h"], sp), min(b["l"], sp), sp
        self._bars[und] = (n, bars)
        return [dict(bars[k]) for k in sorted(bars)]

    def vix(self) -> Optional[float]:
        """The most RECENT recorded VIX (by time, across both underlyings; it used to be whichever underlying was iterated last)."""
        best_ts, last = -1.0, None
        for ix in self.ix.values():
            j = self._vix_pos(ix)
            if j:
                i = ix.vix_idx[j - 1]
                if ix.ts[i] >= best_ts:
                    best_ts, last = ix.ts[i], ix.vix[i]
        return last or self.vix_default

    def _vix_pos(self, ix: "_SnapIndex") -> int:
        lo, hi = 0, len(ix.vix_idx)
        while lo < hi:                                                 # bisect over the vix rows without building a list per call
            mid = (lo + hi) // 2
            if ix.ts[ix.vix_idx[mid]] <= self.ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def lookup(self, und, expiry, strike, opt_type):
        return None


def run(root: str, days: Sequence[str], daily_by_und: Dict[str, Sequence[dict]], overrides: Optional[Dict[str, Any]] = None, underlyings: Sequence[str] = ("NIFTY", "SENSEX"),
        holidays: Sequence[str] = (), vix_default: float = 15.0) -> Dict[str, Any]:
    cfg = make_cfg({"FNO_UNDERLYINGS": "BOTH" if len(underlyings) > 1 else underlyings[0], **(overrides or {})})
    p = Params.from_config(cfg)
    all_trades = []
    ran: List[str] = []
    warnings: List[str] = []
    suspect: set = set()                                               # round 507: days whose recording starts late or has a hole: their opening range / bars are not real
    for day in days:
        snaps = {u: _SnapIndex.from_file(root, day, u) for u in underlyings}      # round 504: indexes, not every parsed snapshot of the day
        if not any(len(ix) for ix in snaps.values()):
            continue
        ran.append(day)
        prov = ReplayProvider(snaps, {u: daily_by_und.get(u, []) for u in underlyings}, vix_default)
        db = st.Store(":memory:")
        e = eng.Engine(db, prov, cfg, holidays=tuple(holidays))
        stamps = sorted({t for ix in snaps.values() for t in ix.ts})
        # The intraday bars are BUILT from these snapshots. The opening-range strategies read "the first 15 / 30 minutes" from the first bar, so a recording that starts late, or has a hole,
        # silently changes what they see. Say so instead of hiding it.
        first = datetime.fromtimestamp(stamps[0], IST)
        if first.time() > dtime(9, 20):
            suspect.add(day)
            warnings.append(f"{day}: recording starts {first:%H:%M}, not at the open: the opening range the strategies use is wrong that day")
        gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a > 300.0]
        if gaps:
            suspect.add(day)
            warnings.append(f"{day}: {len(gaps)} gap(s) in the recording, longest {max(gaps) / 60:.0f} min: bars across a gap are not real bars")
        for ts in stamps:
            prov.set_time(ts)
            e.tick(datetime.fromtimestamp(ts, IST))
        if db.open_positions():                                         # whatever the recording did not cover is closed at its last known price
            prov.set_time(stamps[-1])
            e.squareoff_all(reason="END_OF_RECORDING", now=datetime.fromtimestamp(stamps[-1], IST))
        for t in db.positions(limit=5000):
            all_trades.append((t, day))
        db.close()
    trades_only = [t for t, _ in all_trades]
    m = metrics.summarize(trades_only, p.capital)
    clean = [t for t, d in all_trades if d not in suspect]
    clean_days = [d for d in ran if d not in suspect]
    m_clean = metrics.summarize(clean, p.capital) if (suspect and clean) else None
    return {"kind": "REPLAY", "underlying": "+".join(underlyings), "from": ran[0] if ran else None, "to": ran[-1] if ran else None, "days": len(ran), "capital": p.capital,
            "params": p.to_json(), "warnings": warnings, "metrics": m, "verdict": metrics.verdict(m), "trades": [dict(t.to_json(), suspect_day=(d in suspect)) for t, d in all_trades],
            "suspect_days": sorted(suspect), "clean_days": len(clean_days), "clean_overall": (m_clean or {}).get("overall") if m_clean else None,
            "notes": ["real recorded premiums and quotes through the live engine code",
                      "snapshots are about a minute apart: exits are decided on those minute marks, a real stop could have been hit sooner",
                      "positions still open when a day's recording ends are closed at END_OF_RECORDING",
                      "intraday bars are built from the snapshots (spot about once a minute), so a bar's high / low is thinner than a real 5-minute bar: range-based strategies see fewer expansion bars than live"
                      + (f"; {len(warnings)} day warning(s), see `warnings`" if warnings else "")] if ran else ["no recorded snapshots found for the requested days"]}
