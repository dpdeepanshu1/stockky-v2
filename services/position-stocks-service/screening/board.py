"""screening/board.py - group305/307: assembles the Market Board payloads from data already in memory (bar store, ws_client day
stats / exchange VWAP, prev_day normal move, F&O list, sector map, market_structure maths). No network, no DB, no orders.
A symbol with missing inputs is left out of the ranking (counted in `warming`), never guessed."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

import config
from feed import bar_backfill, bar_store, fno_list, sector_map, ws_client
from screening import market_structure as ms
from screening import prev_day

logger = logging.getLogger("position-stocks-board")

IST_OFFSET_S = 330 * 60
OPEN_SECONDS = 9 * 3600 + 15 * 60
COLD_START_TOLERANCE_S = 3 * 60          # first bar must start within 3 min of 09:15 to stand in for the day open

_lock = threading.Lock()
_cache: Dict[tuple, Tuple[float, dict]] = {}
_rank_hist: deque = deque(maxlen=64)      # (ts, {symbol: (side, rank)})
_zone_cache: Dict[str, Tuple[str, dict]] = {}


def reset() -> None:
    with _lock:
        _cache.clear(); _rank_hist.clear(); _zone_cache.clear()
    bar_backfill.reset()


def session_open_ts(day_number: Optional[int]) -> Optional[float]:
    if day_number is None:
        return None
    return float(day_number * 86400 - IST_OFFSET_S + OPEN_SECONDS)


def params_version() -> str:
    return getattr(config, "BOARD_PARAMS_VERSION", "b1")


def _day_inputs(symbol: str, bars: list, open_ts: Optional[float]) -> Optional[dict]:
    """open/high/low/prev_close/ltp, or None when the day open is not trustworthy (exchange open first, else the first minute
    bar - only when the bar store saw the session from the open)."""
    if not bars:
        return None
    ltp = bars[-1][4]
    ds = ws_client.get_day_stats(symbol) or (None, None, None, None)
    day_open, hi, lo, prev = ds
    if not day_open:
        if open_ts is not None and bars[0][0] - open_ts <= COLD_START_TOLERANCE_S and bars[0][1] > 0:
            day_open = bars[0][1]
        else:
            return None
    bar_hi = max(b[2] for b in bars)
    bar_lo = min(b[3] for b in bars)
    hi = max(hi or 0.0, bar_hi, day_open)
    lo = min(lo or 1e18, bar_lo, day_open)
    return {"ltp": ltp, "open": day_open, "high": hi, "low": lo, "prev_close": prev}


def _universe_symbols(universe: str) -> List[str]:
    syms = bar_store.symbols()
    if universe == "fno":
        fno = fno_list.get_fno_symbols()
        return [s for s in syms if s in fno]
    if universe == "liquid":
        n = getattr(config, "BOARD_LIQUID_TOP_N", 100)
        return sorted(syms, key=lambda s: bar_store.turnover(s), reverse=True)[:n]
    return syms


def _rank_delta(side: str, symbol: str, rank: int, now: float) -> Optional[int]:
    best = None
    for ts, snap in _rank_hist:
        age = now - ts
        if 45.0 <= age <= 300.0 and (best is None or abs(age - 60.0) < abs((now - best[0]) - 60.0)):
            best = (ts, snap)
    if best is None:
        return None
    prev = best[1].get(symbol)
    if not prev or prev[0] != side:
        return None
    return prev[1] - rank


def zone_view(symbol: str, side: str, ltp: float, bars1m: Optional[list] = None, pd: Optional[prev_day.PrevDay] = None,
              open_ts: Optional[float] = None) -> Optional[dict]:
    """Zone / levels / R:R / chase for one symbol and side on BOARD_ZONE_TF_MIN bars. None when zones are off or data is thin.
    The expensive part (zones, swings) is cached per symbol until the bars change."""
    if not getattr(config, "BOARD_ZONES_ENABLED", True):
        return None
    bars1m = bars1m if bars1m is not None else bar_store.get_bars(symbol)
    if len(bars1m) < 10:
        return None
    if pd is None:
        pd = prev_day.peek(symbol)
    if open_ts is None:
        open_ts = session_open_ts(bar_store.day_number())
    tf = config.BOARD_ZONE_TF_MIN
    bars = ms.aggregate(bars1m, tf)
    key = f"{symbol}|{tf}|{bars[-1][0]}|{len(bars1m)}|{params_version()}"
    with _lock:
        hit = _zone_cache.get(symbol)
    if hit and hit[0] == key:
        base = hit[1]
    else:
        zones = ms.find_zones(bars, atr_mult=config.BOARD_ZONE_ATR_MULT, full_range=config.BOARD_ZONE_FULL_RANGE)
        sw = ms.swings(bars)
        orr = ms.opening_range(bars1m, open_ts, 15) if open_ts else None
        pdd = {"high": pd.high, "low": pd.low, "close": pd.close} if pd else None
        base = {"zones": zones, "swings": sw, "or": orr, "prev": pdd, "bars": bars}
        with _lock:
            _zone_cache[symbol] = (key, base)
            if len(_zone_cache) > 600:
                for k in list(_zone_cache)[:100]:
                    _zone_cache.pop(k, None)
    zone = ms.pick_zone(base["zones"], side, ltp)
    lv = ms.levels(ltp, base["swings"], base["bars"], base["prev"], base["or"])
    return {"zone": zone, "zones": base["zones"], "levels": lv, "rr": ms.risk_reward(side, ltp, zone, lv),
            "chase_atr": ms.chase_atr(side, ltp, zone), "or": base["or"], "tf_min": tf, "quality": ms.zone_quality(zone)}


def _rule_label(side: str, rank: int, conv: int, chase: Optional[float]) -> str:
    word = "BULLISH" if side == "bull" else "BEARISH"
    if chase is not None and chase > config.BOARD_CHASE_ATR:
        return f"EXTENDED · {word} #{rank}"
    return f"{'PICK' if (rank <= 5 and conv >= 70) else 'WATCH'} · {word} #{rank}"


def build(universe: Optional[str] = None, limit: Optional[int] = None, open_symbols: Optional[Iterable[str]] = None,
          now: Optional[float] = None, use_cache: bool = True) -> dict:
    """Full board payload: market summary, sectors, ranked bullish / bearish rows. Cached BOARD_CACHE_S seconds."""
    now = time.time() if now is None else now
    universe = (universe or config.BOARD_UNIVERSE_DEFAULT).lower()
    if universe not in ("all", "fno", "liquid"):
        universe = config.BOARD_UNIVERSE_DEFAULT
    limit = max(5, min(config.BOARD_ROW_LIMIT if limit is None else int(limit), 300))
    held: Set[str] = {str(s).upper() for s in (open_symbols or [])}
    ckey = (universe, limit, tuple(sorted(held)))
    if use_cache and config.BOARD_CACHE_S > 0:
        with _lock:
            hit = _cache.get(ckey)
        if hit and now - hit[0] < config.BOARD_CACHE_S:
            return hit[1]

    open_ts = session_open_ts(bar_store.day_number())
    weights = ms.parse_weights(config.BOARD_CONVICTION_WEIGHTS)
    fno = fno_list.get_fno_symbols()
    syms = _universe_symbols(universe)
    rows: List[dict] = []
    warming = no_open = 0
    late: List[str] = []              # group311: first bar later than the cold-start tolerance -> backfill candidates
    up = down = flat = 0
    sector_stats: Dict[str, dict] = {}
    for s in sorted(syms, key=lambda x: bar_store.turnover(x), reverse=True)[: max(limit * 2, 60)]:
        prev_day.get(s)          # warm the previous-day cache (background fetch, never blocks)
    for s in syms:
        bars = bar_store.get_bars(s)
        if bars and open_ts is not None and bars[0][0] - open_ts > COLD_START_TOLERANCE_S and not bar_store.is_backfilled(s):
            late.append(s)
        d = _day_inputs(s, bars, open_ts)
        if d is None:
            no_open += 1
            continue
        ref = d["prev_close"] or d["open"]
        chg = (d["ltp"] - ref) / ref * 100.0 if ref else 0.0
        if chg > 0.0:
            up += 1
        elif chg < 0.0:
            down += 1
        else:
            flat += 1
        sec = sector_map.sector_of(s)
        st = sector_stats.setdefault(sec, {"name": sec, "count": 0, "sum_chg": 0.0, "bull": 0, "bear": 0})
        st["count"] += 1
        st["sum_chg"] += chg
        pd = prev_day.peek(s)
        avg = pd.avg_range_pct if pd else None
        sr = ms.strength(d["ltp"], d["open"], d["high"], d["low"], avg, config.BOARD_ONESIDED_RATIO)
        if sr is None:
            warming += 1
            continue
        vwap = ws_client.get_exchange_vwap(s) or ms.vwap_from_bars(bars)
        rows.append({
            "symbol": s, "name": s, "sector": sec, "is_fno": s in fno, "side": sr["side"], "score": sr["score"],
            "strength": round(sr["score"], 2), "move_pct": round(sr["move_pct"], 2), "change_pct": round(chg, 2),
            "ltp": d["ltp"], "open": d["open"], "day_high": d["high"], "day_low": d["low"], "vwap": vwap,
            "turnover": bar_store.turnover(s), "in_position": s in held, "_bars": bars, "_pd": pd,
        })

    if late and getattr(config, "BOARD_BACKFILL_ENABLED", True):
        try:
            late.sort(key=lambda x: bar_store.turnover(x), reverse=True)
            bar_backfill.request(late[: int(getattr(config, "BOARD_BACKFILL_TOP_N", 120))], now)
        except Exception as e:  # noqa: BLE001
            logger.debug("board: backfill request failed (%s)", e)
    bullish = ms.rank_rows(rows, "bull")
    bearish = ms.rank_rows(rows, "bear")
    sec_side: Dict[str, Dict[str, int]] = {}
    for r in bullish + bearish:
        sec_side.setdefault(r["sector"], {"bull": 0, "bear": 0})[r["side"]] += 1
    for sec, c in sec_side.items():
        sector_stats[sec]["bull"], sector_stats[sec]["bear"] = c["bull"], c["bear"]

    snap: Dict[str, Tuple[str, int]] = {}
    out_sides = {}
    for side, ranked in (("bull", bullish), ("bear", bearish)):
        shown = ranked[:limit]
        for i, r in enumerate(shown):
            snap[r["symbol"]] = (side, r["rank"])
            bars = r.pop("_bars")
            pd = r.pop("_pd")
            sc = sec_side.get(r["sector"], {})
            tot = (sc.get("bull", 0) + sc.get("bear", 0)) or 0
            agree = (sc.get(side, 0) / tot) if tot else None
            zv = zone_view(r["symbol"], side, r["ltp"], bars, pd, open_ts) if i < 40 else None
            zone = zv["zone"] if zv else None
            chase = zv["chase_atr"] if zv else None
            r["conviction"] = ms.conviction(r["percentile"], ms.volume_pace(bars), r["ltp"], r["vwap"], side, agree,
                                            zv["quality"] if zv else None, weights)
            r["rank_delta"] = _rank_delta(side, r["symbol"], r["rank"], now)
            orr = zv["or"] if zv else (ms.opening_range(bars, open_ts, 15) if open_ts else None)
            r["or_low"], r["or_high"] = (orr if orr else (None, None))
            r["zone_status"] = zone["status"] if zone else None
            r["zone_valid"] = zone["valid"] if zone else None
            r["chase_atr"] = chase
            r["extended"] = bool(chase is not None and chase > config.BOARD_CHASE_ATR)
            r["rr"] = zv["rr"]["rr"] if zv and zv["rr"] else None
            zr = ms.zone_risk(side, r["ltp"], zone)
            r["invalidation"], r["risk_per_share"] = (zr if zr else (None, None))
            r["zone_score"] = ms.zone_score(zone, r["rr"], chase, config.BOARD_CHASE_ATR)
            r["rule"] = _rule_label(side, r["rank"], r["conviction"], chase)
            r["profile"] = ms.profile(bars, 16)
            r["spark"] = [round(x, 2) for x in ms.spark(bars, 40)]
            r.pop("score", None)
        out_sides[side] = shown
        for r in ranked[limit:]:
            r.pop("_bars", None); r.pop("_pd", None)
    sectors = [{"name": st["name"], "count": st["count"], "avg_change_pct": round(st["sum_chg"] / st["count"], 2),
                "bull": st["bull"], "bear": st["bear"]} for st in sector_stats.values()]
    sectors.sort(key=lambda x: -x["avg_change_pct"])
    named = [x for x in sectors if x["name"] != "Other" and x["count"] >= 3]
    market = {"up": up, "down": down, "flat": flat, "tracked": up + down + flat,
              "bull_count": len(bullish), "bear_count": len(bearish),
              "leading_sector": named[0]["name"] if named else None,
              "leading_change_pct": named[0]["avg_change_pct"] if named else None,
              "lagging_sector": named[-1]["name"] if named else None,
              "lagging_change_pct": named[-1]["avg_change_pct"] if named else None}
    try:
        w = ws_client.ws_status()
        feed = {"source": w.get("source"), "connected": bool(w.get("connected")), "last_tick_at": w.get("last_tick_at"),
                "tick_age_s": (round(now - w["last_tick_at"], 1) if isinstance(w.get("last_tick_at"), (int, float)) else None)}
    except Exception as e:  # noqa: BLE001
        feed = {"source": None, "connected": False, "last_tick_at": None, "tick_age_s": None, "error": str(e)[:80]}
    payload = {"as_of": now, "universe": universe, "limit": limit, "params_version": params_version(),
               "feed": feed, "market": market, "sectors": sectors, "bullish": out_sides.get("bull", []),
               "bearish": out_sides.get("bear", []),
               "coverage": {"tracked": len(syms), "ranked": len(rows), "warming": warming, "no_day_open": no_open,
                            "late_start": len(late), "bar_store": bar_store.stats(), "backfill": bar_backfill.state()}}
    with _lock:
        if not _rank_hist or now - _rank_hist[-1][0] >= 15.0:
            _rank_hist.append((now, snap))
        _cache[ckey] = (now, payload)
        if len(_cache) > 40:
            for k in list(_cache)[:10]:
                _cache.pop(k, None)
    return payload


def symbol_view(symbol: str, held: Optional[dict] = None, now: Optional[float] = None) -> Optional[dict]:
    """Popup payload for one symbol (5m candles, VWAP series, zones, levels, OR, R:R, held-position overlay). None without bars."""
    symbol = str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    bars = bar_store.get_bars(symbol)
    if not bars:
        return None
    now = time.time() if now is None else now
    open_ts = session_open_ts(bar_store.day_number())
    d = _day_inputs(symbol, bars, open_ts)
    pd = prev_day.peek(symbol)
    ltp = bars[-1][4]
    sr = ms.strength(ltp, d["open"], d["high"], d["low"], pd.avg_range_pct if pd else None, config.BOARD_ONESIDED_RATIO) if d else None
    side = (sr or {}).get("side") or ("bull" if d and ltp >= d["open"] else "bear")
    zv = zone_view(symbol, side, ltp, bars, pd, open_ts)
    tf = config.BOARD_ZONE_TF_MIN
    bars_tf = ms.aggregate(bars, tf)
    cum_pv = cum_v = 0.0
    vwap_series = []
    for b in bars_tf:
        if b[5] > 0:
            cum_pv += (b[2] + b[3] + b[4]) / 3.0 * b[5]
            cum_v += b[5]
        vwap_series.append([b[0], round(cum_pv / cum_v, 2) if cum_v > 0 else None])
    return {"symbol": symbol, "sector": sector_map.sector_of(symbol), "is_fno": fno_list.is_fno(symbol), "as_of": now,
            "ltp": ltp, "side": side, "tf_min": tf, "params_version": params_version(),
            "day": d, "strength": sr, "vwap": ws_client.get_exchange_vwap(symbol) or ms.vwap_from_bars(bars),
            "candles": [[b[0], b[1], b[2], b[3], b[4], b[5]] for b in bars_tf], "vwap_series": vwap_series,
            "prev_day": ({"high": pd.high, "low": pd.low, "close": pd.close, "atr": pd.atr} if pd else None),
            "zones": zv["zones"] if zv else [], "zone": zv["zone"] if zv else None,
            "levels": zv["levels"] if zv else {"supports": [], "resistances": []},
            "rr": zv["rr"] if zv else None, "chase_atr": zv["chase_atr"] if zv else None,
            "extended": bool(zv and zv["chase_atr"] is not None and zv["chase_atr"] > config.BOARD_CHASE_ATR),
            "or": list(zv["or"]) if zv and zv["or"] else None, "position": held,
            "zone_score": ms.zone_score(zv["zone"], zv["rr"]["rr"] if zv and zv["rr"] else None, zv["chase_atr"], config.BOARD_CHASE_ATR) if zv else None,
            "invalidation_risk": (lambda zr: ({"invalidation": zr[0], "risk_per_share": zr[1]} if zr else None))(ms.zone_risk(side, ltp, zv["zone"])) if zv else None,
            "backfilled": bar_store.is_backfilled(symbol)}
