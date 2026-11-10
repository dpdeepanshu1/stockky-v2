"""fno/provider.py - where the engine gets its data: the market-data-service (which owns the Dhan credentials, the rate limit and the scrip master).

`Provider` is the interface the engine needs; `HttpProvider` implements it with stdlib urllib and short caches; tests use a fake. Nothing here places an order."""
from __future__ import annotations

import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import Chain

IST = timezone(timedelta(hours=5, minutes=30))
INDEX_SYMBOL = {"NIFTY": "^NSEI", "SENSEX": "^BSESN"}
VIX_SYMBOL = "^INDIAVIX"
try:                                                           # E322: the same tolerance the market-structure engine's prep applies (0.05 %)
    from stockky_ms.params import MSParams
    BAR_TOLERANCE_PCT = float(MSParams().bar_tolerance_pct)
except Exception:  # noqa: BLE001
    BAR_TOLERANCE_PCT = 0.05


def bar_ok(o: float, h: float, l: float, c: float) -> bool:
    """E322: the engine's own bar rule (stockky_ms.prep._valid): finite, positive, high >= low, open and close inside [low, high] within the tolerance. The F&O signals and strategies
    read the index candles as they come, so a corrupt print (a close of 140 under a high of 101, a NaN, a zero) used to reach the opening-range and trend decisions."""
    try:
        vals = (float(o), float(h), float(l), float(c))
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in vals):
        return False
    o, h, l, c = vals
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or h < l:
        return False
    tol = max(h, 1e-9) * BAR_TOLERANCE_PCT / 100.0
    return not (c > h + tol or c < l - tol or o > h + tol or o < l - tol)


class Provider:
    """Interface. Every method returns None / [] when data is unavailable - never raises into the engine."""
    def expiries(self, underlying: str) -> List[str]: raise NotImplementedError
    def chain(self, underlying: str, expiry: str) -> Optional[Chain]: raise NotImplementedError
    def daily(self, underlying: str) -> List[Dict[str, float]]: raise NotImplementedError
    def intraday(self, underlying: str) -> List[Dict[str, float]]: raise NotImplementedError
    def vix(self) -> Optional[float]: raise NotImplementedError
    def lookup(self, underlying: str, expiry: str, strike: float, opt_type: str) -> Any: raise NotImplementedError


def label_to_epoch(label: str) -> Optional[float]:
    """market-data candle label 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' (IST wall clock) -> epoch seconds."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(label)[:16] if " " in str(label) else str(label)[:10], fmt).replace(tzinfo=IST).timestamp()
        except ValueError:
            continue
    return None


def convert_candles(raw: Any) -> List[Dict[str, float]]:
    """market-data {"date","open","high","low","close","volume"} rows -> {"t","o","h","l","c","v"}; bad rows dropped, ascending, one candle per timestamp (the later row wins).
    E322: a row the engine's bar rule rejects (see bar_ok) is bad, and an unreadable volume is 0 (it used to raise out of intraday() / daily())."""
    by_t: Dict[float, Dict[str, float]] = {}
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        t = label_to_epoch(r.get("date"))
        try:
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            continue
        if t is None or not bar_ok(o, h, l, c):
            continue
        try:
            v = float(r.get("volume") or 0)
        except (TypeError, ValueError):
            v = 0.0
        by_t[t] = {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v if math.isfinite(v) and v >= 0 else 0.0}
    return [by_t[k] for k in sorted(by_t)]


class _Ttl:
    def __init__(self) -> None:
        self._d: Dict[Any, Any] = {}

    def get(self, key: Any, ttl: float) -> Any:
        hit = self._d.get(key)
        return hit[1] if hit and time.monotonic() - hit[0] <= ttl else None

    def put(self, key: Any, val: Any) -> Any:
        self._d[key] = (time.monotonic(), val)
        return val


class HttpProvider(Provider):
    def __init__(self, base_url: str, timeout_s: float = 12.0, opener: Any = None, daily_exclude_today: bool = True):
        self.base = base_url.rstrip("/")
        self.timeout = timeout_s
        self.daily_exclude_today = bool(daily_exclude_today)
        self._open = opener or urllib.request.urlopen
        self._cache = _Ttl()
        self.last_error: Optional[str] = None

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        try:
            with self._open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=self.timeout) as r:
                self.last_error = None
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:   # round 277: + a truncated response
            self.last_error = f"{path}: {type(e).__name__}"
            return None

    def expiries(self, underlying: str) -> List[str]:
        hit = self._cache.get(("exp", underlying), 300)
        if hit is not None:
            return hit
        body = self._get("/dhan/options/expiries", {"underlying": underlying})
        ex = sorted(body.get("expiries") or []) if isinstance(body, dict) else []
        return self._cache.put(("exp", underlying), ex) if ex else []

    def chain(self, underlying: str, expiry: str) -> Optional[Chain]:
        body = self._get("/dhan/options/chain", {"underlying": underlying, "expiry": expiry})
        if not isinstance(body, dict) or not body.get("strikes"):
            return None
        ch = Chain.from_json(body)
        ch.fetched_at = ch.fetched_at or time.time()
        return ch

    def daily(self, underlying: str) -> List[Dict[str, float]]:
        """Daily index candles. Round 287: by default TODAY's candle is left out (`daily_exclude_today`). market-data serves the session that is still running as the last daily candle, so
        the live direction logic saw a daily candle that repaints every minute (today's high / low / close so far), while every backtest (sim, rolling, replay) only knows the daily
        candles up to YESTERDAY. Live now reads the same picture the backtests tested. The raw list is cached; the cut is made on every call, so it follows the date."""
        hit = self._cache.get(("d", underlying), 600)
        if hit is None:
            body = self._get("/history/" + urllib.parse.quote(INDEX_SYMBOL[underlying], safe=""), {"period": "1y", "interval": "1d"})
            c = convert_candles(body.get("candles") if isinstance(body, dict) else None)
            if not c:
                return []
            hit = self._cache.put(("d", underlying), c)
        if not self.daily_exclude_today:
            return hit
        today = datetime.now(IST).date()
        return [x for x in hit if datetime.fromtimestamp(x["t"], IST).date() < today]

    def intraday(self, underlying: str) -> List[Dict[str, float]]:
        hit = self._cache.get(("i", underlying), 60)
        if hit is not None:
            return hit
        body = self._get("/history/" + urllib.parse.quote(INDEX_SYMBOL[underlying], safe=""), {"period": "5d", "interval": "5m", "force": "true"})
        c = convert_candles(body.get("candles") if isinstance(body, dict) else None)
        today = datetime.now(IST).date()
        c = [x for x in c if datetime.fromtimestamp(x["t"], IST).date() == today]       # today's session only
        return self._cache.put(("i", underlying), c) if c else []

    def vix(self) -> Optional[float]:
        hit = self._cache.get(("vix",), 30)
        if hit is not None:
            return hit
        body = self._get("/quote/" + urllib.parse.quote(VIX_SYMBOL, safe=""))
        for k in ("price", "last_price", "ltp", "close"):
            try:
                v = float((body or {}).get(k))
                if v > 0:
                    return self._cache.put(("vix",), v)
            except (TypeError, ValueError):
                continue
        return None

    def lookup(self, underlying: str, expiry: str, strike: float, opt_type: str) -> Any:
        key = ("con", underlying, expiry)
        rows = self._cache.get(key, 3600)
        if rows is None:
            body = self._get("/dhan/options/contracts", {"underlying": underlying, "expiry": expiry, "limit": 5000})
            rows = {(float(c["strike"]), c["opt_type"]): c for c in (body or {}).get("contracts", []) if isinstance(c, dict) and "strike" in c} if isinstance(body, dict) else {}
            self._cache.put(key, rows)
        c = rows.get((float(strike), opt_type))
        if not c:
            return None

        class _Ref:
            pass
        r = _Ref()
        r.security_id, r.lot_size, r.segment, r.tick_size = c.get("security_id"), c.get("lot_size"), c.get("segment", ""), c.get("tick_size", 0.05)
        return r
