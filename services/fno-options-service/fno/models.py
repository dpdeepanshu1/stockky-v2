"""fno/models.py - the data shapes: one option side, one strike, one chain, one contract. Pure; tolerant of missing fields (None, never 0)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _n(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


@dataclass
class Side:
    security_id: Optional[int] = None
    ltp: Optional[float] = None
    iv: Optional[float] = None          # percent, as Dhan gives it (13.2 = 13.2 %)
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    oi: Optional[float] = None
    prev_oi: Optional[float] = None
    volume: Optional[float] = None
    prev_volume: Optional[float] = None
    prev_close: Optional[float] = None
    bid: Optional[float] = None
    bid_qty: Optional[float] = None
    ask: Optional[float] = None
    ask_qty: Optional[float] = None

    @classmethod
    def from_json(cls, d: Any) -> Optional["Side"]:
        if not isinstance(d, dict):
            return None
        sid = _n(d.get("security_id"))
        return cls(security_id=int(sid) if sid is not None else None, ltp=_n(d.get("ltp")), iv=_n(d.get("iv")), delta=_n(d.get("delta")),
                   gamma=_n(d.get("gamma")), theta=_n(d.get("theta")), vega=_n(d.get("vega")), oi=_n(d.get("oi")), prev_oi=_n(d.get("prev_oi")),
                   volume=_n(d.get("volume")), prev_volume=_n(d.get("prev_volume")), prev_close=_n(d.get("prev_close")), bid=_n(d.get("bid")),
                   bid_qty=_n(d.get("bid_qty")), ask=_n(d.get("ask")), ask_qty=_n(d.get("ask_qty")))

    @property
    def mid(self) -> Optional[float]:
        if self.bid and self.ask and self.bid > 0 and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread_pct(self) -> Optional[float]:
        """(ask - bid) / mid in percent; None when there is no two-sided quote (never read as 0 = "free")."""
        if self.bid and self.ask and self.bid > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2.0) * 100.0
        return None

    def to_json(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in ("security_id", "ltp", "iv", "delta", "gamma", "theta", "vega", "oi", "prev_oi", "volume", "prev_volume",
                                              "prev_close", "bid", "bid_qty", "ask", "ask_qty")}


@dataclass
class StrikeRow:
    strike: float
    ce: Optional[Side] = None
    pe: Optional[Side] = None

    def side(self, opt_type: str) -> Optional[Side]:
        return self.ce if opt_type == "CE" else self.pe


@dataclass
class Chain:
    underlying: str
    expiry: str                       # YYYY-MM-DD
    spot: Optional[float]
    rows: List[StrikeRow] = field(default_factory=list)
    fetched_at: float = 0.0           # epoch seconds

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Chain":
        rows: List[StrikeRow] = []
        for r in (d or {}).get("strikes") or []:
            k = _n(r.get("strike")) if isinstance(r, dict) else None
            if k is None:
                continue
            rows.append(StrikeRow(strike=k, ce=Side.from_json(r.get("ce")), pe=Side.from_json(r.get("pe"))))
        rows.sort(key=lambda x: x.strike)
        return cls(underlying=str((d or {}).get("underlying") or "").upper(), expiry=str((d or {}).get("expiry") or ""), spot=_n((d or {}).get("spot")),
                   rows=rows, fetched_at=_n((d or {}).get("fetched_at")) or 0.0)

    def to_json(self) -> Dict[str, Any]:
        return {"underlying": self.underlying, "expiry": self.expiry, "spot": self.spot, "fetched_at": self.fetched_at,
                "strikes": [{"strike": r.strike, "ce": r.ce.to_json() if r.ce else None, "pe": r.pe.to_json() if r.pe else None} for r in self.rows]}

    def age_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at) if self.fetched_at else float("inf")

    def step(self) -> Optional[float]:
        """Smallest strike spacing (50 for Nifty, 100 for Sensex in practice; read from the chain, never assumed)."""
        ks = [r.strike for r in self.rows]
        diffs = [b - a for a, b in zip(ks, ks[1:]) if b - a > 0]
        return min(diffs) if diffs else None

    def atm_index(self) -> Optional[int]:
        if not self.rows or self.spot is None:
            return None
        return min(range(len(self.rows)), key=lambda i: abs(self.rows[i].strike - self.spot))

    def atm_strike(self) -> Optional[float]:
        i = self.atm_index()
        return self.rows[i].strike if i is not None else None

    def row(self, strike: float) -> Optional[StrikeRow]:
        for r in self.rows:
            if abs(r.strike - strike) < 1e-6:
                return r
        return None


@dataclass
class ContractRef:
    """Everything needed to trade one option: what the scrip master says about it."""
    underlying: str
    expiry: str
    strike: float
    opt_type: str                     # CE | PE
    lot_size: int
    security_id: Optional[int] = None
    segment: str = ""                 # NSE_FNO | BSE_FNO
    tick_size: float = 0.05

    @property
    def exchange(self) -> str:
        return "BSE" if self.underlying == "SENSEX" else "NSE"

    def label(self) -> str:
        s = int(self.strike) if float(self.strike).is_integer() else self.strike
        return f"{self.underlying} {self.expiry} {s} {self.opt_type}"
