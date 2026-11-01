"""screening/premarket_plan.py - E272: the 08:30 to 09:20 pre-market plan (plan, 'The morning timeline').

One picture of the morning, built before any order is possible:
  * overseas cues: market-data GET /global-cues (ES=F, NQ=F, ^GSPC, ^IXIC, ^FTSE, ^GDAXI, ^N225, ^HSI; no GIFT Nifty). Rows carry no age, so freshness is tested on bar_date:
    a row is used only when its bar_date is the last trading day before today (or today). The average of the usable rows and the US-futures reading give a `risk_off` flag
    (same -0.9 % average / -1.2 % US-futures limits the swing carry check uses). It only scales size and picks the story (gap-down risk, S4 and the gap-fade S3); it never vetoes.
  * India VIX against its own recent range (percentile of today's reading in the daily closes vix_gate already fetches): a regime label, not a fixed level.
  * event records from the typed feed (analysis-intelligence GET /events/active) for the watchlist: type, polarity (+1 / -1 only), strength, age, whether results are out.
  * sector strength: average pre-open gap of each sector's names (preopen snapshot) and the rank, top and bottom third (feeds the S1 / S4 confirmation).
Cadence: first build 08:30, rebuilt 09:05, then refreshed every minute until 09:20 (PREMARKET_PLAN_*). In memory, one plan per IST day, nothing raises. Fail-safe: any source down leaves
its part empty and the rest still builds; an empty event list means no S1 / S4 trigger from this plan (the normal setups run as usual). Off with PREMARKET_PLAN_ENABLED=0.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import config
from tz_utils import ist_now, ist_today_str, is_nse_holiday

logger = logging.getLogger("position-stocks-premarket-plan")

CUE_SYMBOLS = ("ES=F", "NQ=F", "^GSPC", "^IXIC", "^FTSE", "^GDAXI", "^N225", "^HSI")
US_FUTURES = ("ES=F", "NQ=F")
RISK_OFF_AVG_PCT = -0.9
RISK_OFF_US_FUT_PCT = -1.2
MIN_MINUTES = (8 * 60 + 30, 9 * 60 + 5)          # first build, rebuild
EVERY_MINUTE_FROM, UNTIL = 9 * 60 + 5, 9 * 60 + 20

_plan: Dict[str, Any] = {"day": None, "built_at": None, "builds": 0, "last_minute": None, "plan": None}


def reset() -> None:
    _plan.update({"day": None, "built_at": None, "builds": 0, "last_minute": None, "plan": None})


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _prev_trading_day(d) -> Any:
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def cue_rows(body: Any, today) -> Dict[str, Any]:
    """Usable overseas cues from a /global-cues body ({"rows": {SYMBOL: row}}): {symbol: day % change}, plus the symbols dropped as stale.
    A row is stale when its bar_date is older than the last trading day before `today` (rows carry no age, so bar_date is the only freshness test); a row with NO bar_date is dropped too."""
    out: Dict[str, float] = {}
    stale: List[str] = []
    try:
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, dict):
            return {"cues": {}, "stale": [], "bar_date": None}
        want = _prev_trading_day(today)
        newest = None
        for sym, row in rows.items():
            if not isinstance(row, dict):
                continue
            s = str(sym).strip().upper()
            bd = str(row.get("bar_date") or "")[:10]
            try:
                d = datetime.strptime(bd, "%Y-%m-%d").date()
            except ValueError:
                stale.append(s)
                continue
            if d < want:
                stale.append(s)
                continue
            chg = _num(row.get("day_change_pct"))
            if chg is None:
                p, pc = _num(row.get("price")), _num(row.get("previous_close"))
                chg = (p - pc) / pc * 100.0 if p and pc and p > 0 and pc > 0 else None
            if chg is None or abs(chg) > 15.0:
                continue
            out[s] = chg
            newest = bd if newest is None or bd > newest else newest
        return {"cues": out, "stale": sorted(stale), "bar_date": newest}
    except Exception:  # noqa: BLE001
        return {"cues": {}, "stale": [], "bar_date": None}


def cue_verdict(cues: Dict[str, float]) -> Dict[str, Any]:
    """{avg_pct, us_fut_pct, risk_off, n}: average of the non-US-futures-excluded cues the carry rule uses (^GDAXI, ^FTSE, ^N225, ^HSI, ES=F, NQ=F), US futures worst reading.
    risk_off is True when the average is below -0.9 % or the worst US future below -1.2 %. Fewer than 3 usable cues gives risk_off None (unknown, nothing is assumed)."""
    try:
        used = [cues[s] for s in ("^GDAXI", "^FTSE", "^N225", "^HSI", "ES=F", "NQ=F") if s in cues]
        fut = [cues[s] for s in US_FUTURES if s in cues]
        if len(used) < 3:
            return {"avg_pct": None, "us_fut_pct": min(fut) if fut else None, "risk_off": None, "n": len(used)}
        avg = sum(used) / len(used)
        worst = min(fut) if fut else None
        return {"avg_pct": round(avg, 3), "us_fut_pct": round(worst, 3) if worst is not None else None,
                "risk_off": bool(avg < RISK_OFF_AVG_PCT or (worst is not None and worst < RISK_OFF_US_FUT_PCT)), "n": len(used)}
    except Exception:  # noqa: BLE001
        return {"avg_pct": None, "us_fut_pct": None, "risk_off": None, "n": 0}


def vix_regime(vix_now: Any, closes: Optional[Iterable[Any]]) -> Dict[str, Any]:
    """India VIX against its own recent range: {value, percentile, label}. label: CALM below the 33rd, NORMAL to the 67th, STRESSED above, UNKNOWN without 20+ closes."""
    try:
        v = _num(vix_now)
        h = [x for x in (_num(c) for c in (closes or [])) if x is not None]
        if v is None or len(h) < 20:
            return {"value": v, "percentile": None, "label": "UNKNOWN"}
        pct = 100.0 * sum(1 for x in h if x <= v) / len(h)
        return {"value": v, "percentile": round(pct, 1), "label": "CALM" if pct < 33.0 else ("NORMAL" if pct < 67.0 else "STRESSED")}
    except Exception:  # noqa: BLE001
        return {"value": None, "percentile": None, "label": "UNKNOWN"}


def event_records(body: Any, watchlist: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """Strongest typed event with KNOWN polarity (+1 / -1) per symbol from a GET /events/active body: {SYMBOL: {type, polarity, strength, age_min, announced_at, result_out}}.
    `result_out` is True when a results-type event is on record (the results are published). Unknown polarity never gets in. A body that is not ok gives {} (fail-safe: no trigger)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        if not isinstance(body, dict) or not body.get("ok"):
            return {}
        wl = {str(s).upper().replace(".NS", "") for s in watchlist} if watchlist is not None else None
        results_out = set()
        for e in body.get("events") or []:
            if isinstance(e, dict) and str(e.get("type")) == "results":
                results_out.add(str(e.get("symbol") or "").upper().replace(".NS", ""))
        for e in body.get("events") or []:
            if not isinstance(e, dict):
                continue
            sym = str(e.get("symbol") or "").upper().replace(".NS", "")
            if not sym or (wl is not None and sym not in wl):
                continue
            try:
                pol = int(float(e.get("polarity") or 0))
            except (TypeError, ValueError):
                continue
            if pol not in (1, -1):
                continue
            st = _num(e.get("strength")) or 0.0
            cur = out.get(sym)
            if cur is None or st > cur["strength"]:
                out[sym] = {"type": str(e.get("type") or "other"), "polarity": pol, "strength": round(st, 4),
                            "age_min": round((_num(e.get("age_s")) or 0.0) / 60.0, 1), "announced_at": e.get("published"), "result_out": sym in results_out}
        return out
    except Exception:  # noqa: BLE001
        return {}


def sector_strength(gaps: Dict[str, float], sector_of, min_names: int = 3) -> Dict[str, Dict[str, Any]]:
    """{sector: {avg_gap_pct, n, rank, third}} from per-symbol pre-open gaps. Sectors with fewer than `min_names` priced names, and 'Other', are left out. rank 1 = strongest;
    third is 'top' / 'mid' / 'bottom' (with fewer than 3 sectors every sector is 'mid')."""
    try:
        by: Dict[str, List[float]] = {}
        for sym, g in (gaps or {}).items():
            gv = _num(g)
            sec = str(sector_of(sym) or "Other")
            if gv is not None and sec != "Other":
                by.setdefault(sec, []).append(gv)
        rows = [(sec, sum(v) / len(v), len(v)) for sec, v in by.items() if len(v) >= max(1, int(min_names))]
        rows.sort(key=lambda r: r[1], reverse=True)
        k = len(rows)
        out: Dict[str, Dict[str, Any]] = {}
        for i, (sec, avg, n) in enumerate(rows):
            third = "mid" if k < 3 else ("top" if i < k / 3.0 else ("bottom" if i >= k - k / 3.0 else "mid"))
            out[sec] = {"avg_gap_pct": round(avg, 3), "n": n, "rank": i + 1, "third": third}
        return out
    except Exception:  # noqa: BLE001
        return {}


def build_plan(*, today, cues_body: Any = None, vix_now: Any = None, vix_closes: Optional[Iterable[Any]] = None, events_body: Any = None,
               watchlist: Optional[Iterable[str]] = None, gaps: Optional[Dict[str, float]] = None, sector_of=None) -> Dict[str, Any]:
    """Pure: the morning plan from already-fetched inputs. Every part degrades to empty / UNKNOWN on bad input. {cues, verdict, vix, events, sectors, notes}."""
    c = cue_rows(cues_body, today)
    verdict = cue_verdict(c["cues"])
    notes: List[str] = []
    if c["stale"]:
        notes.append("stale cue rows dropped: " + ",".join(c["stale"]))
    if verdict["risk_off"]:
        notes.append("overseas risk-off: gap-down risk, S4 and the gap-fade S3 matter today (size only, no veto)")
    ev = event_records(events_body, watchlist)
    return {"day": str(today), "cues": c["cues"], "cue_bar_date": c["bar_date"], "verdict": verdict, "vix": vix_regime(vix_now, vix_closes), "events": ev,
            "sectors": sector_strength(gaps or {}, sector_of or (lambda s: "Other")), "notes": notes,
            "event_counts": {"positive": sum(1 for r in ev.values() if r["polarity"] == 1), "negative": sum(1 for r in ev.values() if r["polarity"] == -1)}}


def due(now: Optional[datetime] = None) -> bool:
    """True when a (re)build is due: 08:30 and 09:05 once each, then once per minute from 09:05 to 09:20, on a trading day. Never raises."""
    try:
        if not getattr(config, "PREMARKET_PLAN_ENABLED", True):
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        if _plan["day"] != ist_today_str(now):
            _plan.update({"day": ist_today_str(now), "built_at": None, "builds": 0, "last_minute": None, "plan": None})
        m = n.hour * 60 + n.minute
        if m < MIN_MINUTES[0] or m >= UNTIL or _plan["last_minute"] == m:
            return False
        if _plan["builds"] == 0:
            return True
        return m >= EVERY_MINUTE_FROM
    except Exception:  # noqa: BLE001
        return False


def current(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Today's plan, or None (not built yet, or built on another day)."""
    try:
        return _plan["plan"] if _plan["day"] == ist_today_str(now) else None
    except Exception:  # noqa: BLE001
        return None


def event_for(symbol: str, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    p = current(now)
    return (p or {}).get("events", {}).get(str(symbol or "").upper().replace(".NS", "")) if p else None


def sector_third(symbol: str, now: Optional[datetime] = None, sector: Optional[str] = None) -> Optional[str]:
    """'top' / 'mid' / 'bottom' for the symbol's sector today, None when unknown (no confirmation is claimed from nothing)."""
    try:
        p = current(now)
        if not p:
            return None
        if not sector:
            from feed import sector_map
            sector = sector_map.sector_of(symbol)
        row = (p.get("sectors") or {}).get(sector)
        return row["third"] if row else None
    except Exception:  # noqa: BLE001
        return None


async def _get_json(url: str, params: Optional[dict] = None, timeout: float = 8.0) -> Any:
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


async def run(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Fetch the inputs and build today's plan. Each source has its own try/except (a failed one leaves its part empty). Never raises. Marks the minute so due() fires once per minute."""
    try:
        n = ist_now(now)
        _plan["last_minute"] = n.hour * 60 + n.minute
        cues_body = events_body = None
        try:
            cues_body = await _get_json(f"{config.MARKET_DATA_URL}/global-cues", {"symbols": ",".join(CUE_SYMBOLS)})
        except Exception as e:  # noqa: BLE001
            logger.info("premarket plan: cues unavailable (%s: %s)", type(e).__name__, e)
        try:
            events_body = await _get_json(f"{config.EVENT_URL}/events/active")
        except Exception as e:  # noqa: BLE001
            logger.info("premarket plan: events unavailable (%s: %s) - no S1 / S4 trigger from the plan", type(e).__name__, e)
        vix_now = closes = None
        try:
            from screening import vix_gate
            snap = await vix_gate.refresh()
            vix_now = (snap or {}).get("level") if isinstance(snap, dict) else None
            closes = vix_gate._hist.get("closes")
        except Exception:  # noqa: BLE001
            pass
        gaps: Dict[str, float] = {}
        wl: List[str] = []
        try:
            from screening import preopen
            gaps = {k: v for k, v in (preopen._snap.get("gaps") or {}).items() if _num(v) is not None}
            wl = preopen._universe()
        except Exception:  # noqa: BLE001
            pass
        try:
            from feed import sector_map
            sec = sector_map.sector_of
        except Exception:  # noqa: BLE001
            sec = None
        plan = build_plan(today=n.date(), cues_body=cues_body, vix_now=vix_now, vix_closes=closes, events_body=events_body, watchlist=wl or None, gaps=gaps, sector_of=sec)
        _plan.update({"day": ist_today_str(now), "plan": plan, "built_at": n.isoformat(), "builds": int(_plan["builds"]) + 1})
        logger.info("[premarket] plan built #%d: cues %d (risk_off=%s), vix %s, events +%d/-%d, sectors %d", _plan["builds"], len(plan["cues"]), plan["verdict"]["risk_off"],
                    plan["vix"]["label"], plan["event_counts"]["positive"], plan["event_counts"]["negative"], len(plan["sectors"]))
        return plan
    except Exception as e:  # noqa: BLE001
        logger.warning("premarket plan failed (ignored): %s", e)
        return None


def status() -> Dict[str, Any]:
    p = _plan["plan"] or {}
    return {"day": _plan["day"], "built_at": _plan["built_at"], "builds": _plan["builds"], "risk_off": (p.get("verdict") or {}).get("risk_off"),
            "vix": (p.get("vix") or {}).get("label"), "events": p.get("event_counts"), "notes": p.get("notes")}
