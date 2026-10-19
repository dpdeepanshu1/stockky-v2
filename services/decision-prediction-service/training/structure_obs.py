"""group374: the all-decision structure observation log (plan WS-6, finding F10).

Every /decide answer that carries a daily-structure block is logged ONCE per (symbol, observation day, engine version, policy mode) - BUY NOW,
PREPARE, WAIT, DO NOT BUY and the picks the structure policy demoted - so false negatives, the "no high-conviction opportunity" frequency and the
20-day / 5%-in-a-month metrics are measurable. Outcomes are scored later from daily bars:

  * entry  = OPEN of the first session AFTER the observation day (+ STRUCTURE_OBS_SLIPPAGE_PCT). Never a same-bar price.
  * T+N    = close of the Nth session counted from the entry session (entry session = 1), N in 1, 5, 10, 20.
  * mfe/mae over the first 20 sessions; hit_5pct_20 = a high reached entry +5%.
  * target_before_stop on the structural stop / target stored with the observation; when one bar touches both, the STOP wins (conservative).
  * nifty_ret_N over the same sessions when benchmark bars are supplied; unknown is None, never 0.

The core here is pure (stdlib only, never raises). The DB glue takes the model class and a session, so the same code runs on Postgres, SQLite and
Oracle (PortableJSON). Nothing in this module changes a decision."""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

try:  # group401: sector label -> sector index (same folder; the tests load this module by path, so fall back to a path import)
    import sector_index as _sx
except ImportError:  # pragma: no cover
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("sector_index", os.path.join(os.path.dirname(os.path.abspath(__file__)), "sector_index.py"))
    _sx = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_sx)

HORIZONS = (1, 5, 10, 20)
WINDOW = 20
MAX_BATCH = 100
MAX_JSON_BYTES = 6000
GIVE_UP_DAYS = 45
_DECISIONS = ("BUY NOW", "PREPARE TO BUY", "HOLD", "WAIT", "DO NOT BUY", "SELL", "AVOID")
_FEATURE_NUM = ("bos_bars", "choch_bars", "chase_atr", "stretch_atr", "room_atr", "rr", "stop", "target", "tqs", "groups", "groups_strict", "ms_price", "atr", "loc_score")
_FEATURE_STR = ("quality", "trend", "bos_dir", "choch_dir", "asof", "ver", "hash", "would_be")   # group422: would_be = the decision with every rule applied (record-only ones included)


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _pos(v: Any) -> Optional[float]:
    f = _num(v)
    return f if f is not None and f > 0 else None


def _txt(v: Any, n: int = 60) -> Optional[str]:
    return v.strip()[:n] if isinstance(v, str) and v.strip() else None


def _iso(v: Any) -> Optional[str]:
    """YYYY-MM-DD from a date / datetime / ISO string; None when it is not a date."""
    try:
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        s = str(v).strip()[:10]
        return date.fromisoformat(s).isoformat()
    except Exception:  # noqa: BLE001
        return None


def enabled() -> bool:
    return (os.getenv("STRUCTURE_OBS_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def slippage_pct() -> float:
    v = _num((os.getenv("STRUCTURE_OBS_SLIPPAGE_PCT") or "").strip() or None)
    return 0.1 if v is None or v < 0 or v > 5 else v


# ── input ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def sanitize(raw: Any) -> Optional[Dict[str, Any]]:
    """An allow-listed, size-capped observation from a decision-service payload, or None when it is unusable (no symbol, no observation day,
    no final decision). Unknown is never invented: absent numbers stay None."""
    try:
        if not isinstance(raw, dict):
            return None
        sym = _txt(raw.get("symbol"), 20)
        obs_date = _iso(raw.get("obs_date"))
        final = _txt(raw.get("final_decision"), 24)
        if not sym or not obs_date or not final:
            return None
        final = final.upper()
        legacy = (_txt(raw.get("legacy_decision"), 24) or final).upper()
        f = raw.get("features") if isinstance(raw.get("features"), dict) else {}
        feats: Dict[str, Any] = {k: _num(f.get(k)) for k in _FEATURE_NUM if _num(f.get(k)) is not None}
        feats.update({k: _txt(f.get(k), 40) for k in _FEATURE_STR if _txt(f.get(k), 40)})
        if isinstance(f.get("bos_failed"), bool):
            feats["bos_failed"] = f["bos_failed"]
        lv = raw.get("levels") if isinstance(raw.get("levels"), dict) else {}
        levels = {"stop": _pos(lv.get("stop")), "target": _pos(lv.get("target")), "rr": _num(lv.get("rr"))}
        rules = [t for t in (_txt(r, 24) for r in (raw.get("rules") if isinstance(raw.get("rules"), list) else [])[:8]) if t]
        out = {
            "symbol": sym.upper(), "obs_date": obs_date, "asof_date": _iso(raw.get("asof_date")),
            "engine_version": _txt(raw.get("engine_version"), 20) or "", "params_hash": _txt(raw.get("params_hash"), 20),
            "policy_mode": (_txt(raw.get("policy_mode"), 12) or "full").lower(),
            "price": _pos(raw.get("price")), "atr": _pos(raw.get("atr")),
            "final_decision": final, "legacy_decision": legacy, "policy_applied": 1 if raw.get("policy_applied") is True else 0,
            "rules": rules, "combined_score": _num(raw.get("combined_score")), "technical_score": _num(raw.get("technical_score")),
            "tqs": _num(raw.get("tqs")), "groups": _num(raw.get("groups")),
            "features": feats, "levels": levels, "sector": _sx.clean_label(raw.get("sector")),
        }
        if len(json.dumps(out["features"], default=str)) > MAX_JSON_BYTES:
            out["features"] = {k: v for k, v in feats.items() if k in ("quality", "trend", "rr", "chase_atr", "asof")}
        return out
    except Exception:  # noqa: BLE001
        return None


# ── outcome (pure) ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _clean_bars(bars: Any) -> List[Dict[str, Any]]:
    """Ascending, valid daily bars {date, open, high, low, close}; bad rows dropped."""
    out: List[Dict[str, Any]] = []
    for b in bars if isinstance(bars, (list, tuple)) else []:
        try:
            d = _iso(b.get("date"))
            o, h, l, c = _pos(b.get("open")), _pos(b.get("high")), _pos(b.get("low")), _pos(b.get("close"))
            if d and o and h and l and c and h >= l:
                out.append({"date": d, "open": o, "high": h, "low": l, "close": c})
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda r: r["date"])
    ded: Dict[str, Dict[str, Any]] = {}
    for r in out:
        ded[r["date"]] = r
    return [ded[k] for k in sorted(ded)]


def _pct(a: float, b: float) -> float:
    return round((a / b - 1.0) * 100.0, 4)


def evaluate(obs: Dict[str, Any], bars: Any, bench: Any = None, slip_pct: Optional[float] = None, sector_bench: Any = None) -> Dict[str, Any]:
    """Outcome fields for one observation. Always returns a dict with `eval_status` in pending | partial | done:
    pending = no session after the observation day yet; partial = some of the 20 sessions exist; done = all 20. Never raises."""
    res: Dict[str, Any] = {"eval_status": "pending", "bars_seen": 0}
    try:
        slip = slippage_pct() if slip_pct is None else float(slip_pct)
        obs_date = _iso(obs.get("obs_date"))
        bs = _clean_bars(bars)
        i0 = next((i for i, b in enumerate(bs) if obs_date and b["date"] > obs_date), None)
        if i0 is None:
            return res
        win = bs[i0:i0 + WINDOW]
        entry_raw = win[0]["open"]
        entry = entry_raw * (1.0 + slip / 100.0)
        res.update({"entry_date": win[0]["date"], "entry_price": round(entry, 4), "bars_seen": len(win)})
        for h in HORIZONS:
            if len(win) >= h:
                res[f"ret_{h}"] = _pct(win[h - 1]["close"], entry)
        res["mfe_20"] = _pct(max(b["high"] for b in win), entry)
        res["mae_20"] = _pct(min(b["low"] for b in win), entry)
        complete = len(win) >= WINDOW
        hit5 = any(b["high"] >= entry * 1.05 for b in win)
        res["hit_5pct_20"] = 1 if hit5 else (0 if complete else None)
        atr, px = _pos(obs.get("atr")), _pos(obs.get("price"))
        if atr and px:
            res["entry_chase_atr"] = round((entry_raw - px) / atr, 3)
        lv = obs.get("levels") if isinstance(obs.get("levels"), dict) else {}
        stop, target = _pos(lv.get("stop")), _pos(lv.get("target"))
        res["target_before_stop"], res["days_to_target"] = None, None
        if stop and target and stop < target:
            for j, b in enumerate(win):
                if b["low"] <= stop:          # stop first, even when the same bar also reaches the target
                    res["target_before_stop"] = 0
                    break
                if b["high"] >= target:
                    res["target_before_stop"], res["days_to_target"] = 1, j + 1
                    break
        bb = _clean_bars(bench)
        if bb:
            bmap = {b["date"]: b for b in bb}
            b0 = bmap.get(win[0]["date"])
            if b0:
                for h in HORIZONS:
                    if len(win) >= h and win[h - 1]["date"] in bmap:
                        res[f"nifty_ret_{h}"] = _pct(bmap[win[h - 1]["date"]]["close"], b0["open"])
        sb = _clean_bars(sector_bench)        # group401: the sector index over the same sessions, same open-to-close basis as nifty_ret_N
        if sb:
            smap = {b["date"]: b for b in sb}
            s0 = smap.get(win[0]["date"])
            if s0:
                for h in HORIZONS:
                    if len(win) >= h and win[h - 1]["date"] in smap:
                        res[f"sector_ret_{h}"] = _pct(smap[win[h - 1]["date"]]["close"], s0["open"])
        res["eval_status"] = "done" if complete else "partial"
        return res
    except Exception:  # noqa: BLE001
        return {"eval_status": "pending", "bars_seen": 0}


# ── report (pure) ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _mean(xs: Iterable[Optional[float]]) -> Optional[float]:
    v = [x for x in xs if isinstance(x, (int, float)) and math.isfinite(x)]
    return round(sum(v) / len(v), 3) if v else None


def _group(rows: List[Dict[str, Any]], min_n: int) -> Dict[str, Any]:
    ev = [r for r in rows if r.get("ret_5") is not None]
    done = [r for r in rows if r.get("eval_status") == "done"]
    hits = [r["hit_5pct_20"] for r in done if r.get("hit_5pct_20") is not None]
    tb = [r["target_before_stop"] for r in rows if r.get("target_before_stop") is not None]
    rel = [r["ret_20"] - r["nifty_ret_20"] for r in done if r.get("ret_20") is not None and r.get("nifty_ret_20") is not None]
    rel_s = [r["ret_20"] - r["sector_ret_20"] for r in done if r.get("ret_20") is not None and r.get("sector_ret_20") is not None]
    n_for_edge = len(done)
    return {
        "n": len(rows), "n_evaluated_5d": len(ev), "n_done_20d": len(done),
        "avg_ret_1": _mean(r.get("ret_1") for r in rows), "avg_ret_5": _mean(r.get("ret_5") for r in rows),
        "avg_ret_10": _mean(r.get("ret_10") for r in rows), "avg_ret_20": _mean(r.get("ret_20") for r in done),
        "win_rate_5d": round(100.0 * sum(1 for r in ev if r["ret_5"] > 0) / len(ev), 1) if ev else None,
        "hit_5pct_within_20d_pct": round(100.0 * sum(hits) / len(hits), 1) if hits else None,
        "target_before_stop_pct": round(100.0 * sum(tb) / len(tb), 1) if tb else None, "n_target_stop_decided": len(tb),
        "avg_mfe_20": _mean(r.get("mfe_20") for r in rows), "avg_mae_20": _mean(r.get("mae_20") for r in rows),
        "avg_entry_chase_atr": _mean(r.get("entry_chase_atr") for r in rows),
        "avg_rel_to_nifty_20": _mean(rel),
        "avg_rel_to_sector_20": _mean(rel_s), "n_with_sector_20": len(rel_s),
        "insufficient_sample": n_for_edge < min_n,
    }


def summarize(rows: Iterable[Dict[str, Any]], min_n: int = 30) -> Dict[str, Any]:
    """Aggregates per final decision plus the two groups the plan cares about: BUY NOW that passed the policy, and picks the policy DEMOTED
    (legacy BUY NOW, final not BUY NOW - the false-negative check). `edge` compares them and says "insufficient_sample" until both have min_n
    fully evaluated rows; nothing here is a verdict before that."""
    rs = [r for r in (rows or []) if isinstance(r, dict)]
    by_dec: Dict[str, List[Dict[str, Any]]] = {}
    for r in rs:
        by_dec.setdefault(str(r.get("final_decision") or "?"), []).append(r)
    passed = [r for r in rs if r.get("final_decision") == "BUY NOW"]
    demoted = [r for r in rs if r.get("legacy_decision") == "BUY NOW" and r.get("final_decision") != "BUY NOW"]
    days = sorted({r["obs_date"] for r in rs if r.get("obs_date")})
    buy_days = {r["obs_date"] for r in passed if r.get("obs_date")}
    g_pass, g_dem = _group(passed, min_n), _group(demoted, min_n)
    insufficient = g_pass["insufficient_sample"] or g_dem["insufficient_sample"]
    delta = None
    if not insufficient and g_pass["hit_5pct_within_20d_pct"] is not None and g_dem["hit_5pct_within_20d_pct"] is not None:
        delta = round(g_pass["hit_5pct_within_20d_pct"] - g_dem["hit_5pct_within_20d_pct"], 1)
    return {
        "total": len(rs), "days_observed": len(days), "no_buy_now_days": len([d for d in days if d not in buy_days]),
        "by_decision": {k: _group(v, min_n) for k, v in sorted(by_dec.items())},
        "passed_buy_now": g_pass, "policy_demoted": g_dem,
        "edge": {"hit_5pct_passed_minus_demoted_pts": delta, "insufficient_sample": insufficient, "min_n": min_n,
                 "note": "A positive number means the policy kept the better setups. Not a verdict until both groups reach min_n fully evaluated rows."},
        "prepare_upgrade": _prepare_upgrade(rs, min_n),
    }


def _prepare_upgrade(rs: List[Dict[str, Any]], min_n: int) -> Dict[str, Any]:
    """group379 (F2): the policy's P9 PREPARE TO BUY -> BUY NOW upgrade is the only place the policy makes a call MORE aggressive than the legacy
    engine, so it gets its own report. Three groups, all keyed on the pre-policy label (legacy_decision):
      upgraded         legacy PREPARE TO BUY, final BUY NOW   (the upgrade under test)
      held_at_prepare  legacy PREPARE TO BUY, final PREPARE TO BUY   (the control: same starting label, upgrade not applied)
      kept_buy_now     legacy BUY NOW, final BUY NOW          (context: what an ordinary BUY NOW did over the same window)
    `edge` = upgraded minus held_at_prepare (did the upgrade pick the better PREPAREs?) and, as context, upgraded minus kept_buy_now. A number is given
    only when both groups have min_n fully evaluated (20-session) rows; before that `insufficient_sample` is true and every delta is None. Nothing
    here is a verdict, and it cannot be one: a hit-rate difference without a confidence interval is a reason to look closer, not to switch the upgrade
    on or off (STRUCTURE_PREPARE_UPGRADE=1 turns it on; it is off by default since group 415)."""
    def pick(legacy: str, final: str) -> List[Dict[str, Any]]:
        return [r for r in rs if r.get("legacy_decision") == legacy and r.get("final_decision") == final]
    g_up = _group(pick("PREPARE TO BUY", "BUY NOW"), min_n)
    g_held = _group(pick("PREPARE TO BUY", "PREPARE TO BUY"), min_n)
    g_kept = _group(pick("BUY NOW", "BUY NOW"), min_n)

    def delta(a: Dict[str, Any], b: Dict[str, Any], key: str) -> Optional[float]:
        if a["insufficient_sample"] or b["insufficient_sample"]:
            return None
        x, y = a.get(key), b.get(key)
        return round(x - y, 3) if isinstance(x, (int, float)) and isinstance(y, (int, float)) else None
    return {
        "upgraded": g_up, "held_at_prepare": g_held, "kept_buy_now": g_kept,
        "edge": {
            "insufficient_sample_vs_held": g_up["insufficient_sample"] or g_held["insufficient_sample"],
            "insufficient_sample_vs_kept": g_up["insufficient_sample"] or g_kept["insufficient_sample"],
            "min_n": min_n,
            "hit_5pct_upgraded_minus_held_pts": delta(g_up, g_held, "hit_5pct_within_20d_pct"),
            "avg_ret_20_upgraded_minus_held_pts": delta(g_up, g_held, "avg_ret_20"),
            "hit_5pct_upgraded_minus_kept_buy_now_pts": delta(g_up, g_kept, "hit_5pct_within_20d_pct"),
            "avg_ret_20_upgraded_minus_kept_buy_now_pts": delta(g_up, g_kept, "avg_ret_20"),
            "note": "Positive vs held = the upgrade picked the better PREPAREs. Not a verdict until the groups reach min_n fully evaluated rows, and "
                    "even then there is no confidence interval here.",
        },
    }


# ── DB glue (model class + session passed in; works on Postgres / SQLite / Oracle) ─────────────────────────────────────────────────────

_OUTCOME_COLS = ("eval_status", "bars_seen", "entry_date", "entry_price", "ret_1", "ret_5", "ret_10", "ret_20", "mfe_20", "mae_20", "hit_5pct_20",
                 "target_before_stop", "days_to_target", "entry_chase_atr", "nifty_ret_1", "nifty_ret_5", "nifty_ret_10", "nifty_ret_20",
                 "sector_ret_1", "sector_ret_5", "sector_ret_10", "sector_ret_20")
_LINEAGE_COLS = ("asof_date", "params_hash", "sector")
_INPUT_COLS = ("asof_date", "params_hash", "price", "atr", "final_decision", "legacy_decision", "policy_applied", "rules", "combined_score",
               "technical_score", "tqs", "groups", "features", "levels", "sector")


def upsert_batch(db: Any, M: Any, items: Any, now: Optional[datetime] = None) -> Dict[str, int]:
    """Idempotent upsert of up to MAX_BATCH observations. Key = (symbol, obs_date, engine_version, policy_mode). An existing row whose outcome
    has not started (eval_status pending) takes the newest decision of the day; a row already being scored is never rewritten."""
    out = {"received": 0, "inserted": 0, "updated": 0, "kept": 0, "invalid": 0}
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)    # group 452: utcnow() is deprecated; same naive-UTC value
    for raw in (items if isinstance(items, list) else [])[:MAX_BATCH]:
        out["received"] += 1
        o = sanitize(raw)
        if o is None:
            out["invalid"] += 1
            continue
        row = db.query(M).filter_by(symbol=o["symbol"], obs_date=o["obs_date"], engine_version=o["engine_version"], policy_mode=o["policy_mode"]).first()
        if row is None:
            db.add(M(symbol=o["symbol"], obs_date=o["obs_date"], engine_version=o["engine_version"], policy_mode=o["policy_mode"], created_at=now,
                     eval_status="pending", eval_attempts=0, **{k: o[k] for k in _INPUT_COLS}))
            out["inserted"] += 1
        elif (getattr(row, "eval_status", "pending") or "pending") == "pending":
            for k in _INPUT_COLS:
                # group378: a later decision of the day that lacks lineage must not blank what an earlier one stored
                if k in _LINEAGE_COLS and o[k] is None and getattr(row, k, None) is not None:
                    continue
                setattr(row, k, o[k])
            out["updated"] += 1
        else:
            out["kept"] += 1
    db.commit()
    return out


def row_to_dict(r: Any) -> Dict[str, Any]:
    keys = ("symbol", "obs_date", "engine_version", "policy_mode") + _INPUT_COLS + _OUTCOME_COLS
    d = {k: getattr(r, k, None) for k in keys}
    for k in ("rules", "features", "levels"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:  # noqa: BLE001
                d[k] = None
    return d


def evaluate_pending(db: Any, M: Any, fetch_bars: Callable[[str], Any], fetch_bench: Optional[Callable[[], Any]] = None,
                     max_batch: int = 50, today: Optional[date] = None,
                     fetch_sector_bench: Optional[Callable[[str], Any]] = None) -> Dict[str, Any]:
    """Score observations that are not 'done'. One history fetch per symbol per call. A row whose bars never arrive is left pending and given up
    on (eval_status 'no_data') GIVE_UP_DAYS after its observation day. Returns counts; never raises."""
    res = {"considered": 0, "evaluated": 0, "done": 0, "waiting": 0, "no_data": 0, "errors": 0}
    today = today or date.today()
    try:
        rows = db.query(M).filter(M.eval_status.in_(("pending", "partial"))).limit(max(1, min(int(max_batch), 300)) * 4).all()
    except Exception:  # noqa: BLE001
        res["errors"] += 1
        return res
    rows = sorted(rows, key=lambda r: (str(getattr(r, "obs_date", "")), str(getattr(r, "symbol", ""))))[:max(1, min(int(max_batch), 300))]
    bench = None
    bars_cache: Dict[str, Any] = {}
    sector_cache: Dict[str, Any] = {}     # group401: one fetch per sector index per call; a failed or unmapped sector is cached as None
    for r in rows:
        res["considered"] += 1
        try:
            sym = r.symbol
            if sym not in bars_cache:
                bars_cache[sym] = fetch_bars(sym)
            if bench is None and fetch_bench is not None:
                bench = fetch_bench() or []
            sbars = None
            if fetch_sector_bench is not None:
                sidx = _sx.index_for(getattr(r, "sector", None))
                if sidx:
                    if sidx not in sector_cache:
                        try:
                            sector_cache[sidx] = fetch_sector_bench(sidx) or None
                        except Exception:  # noqa: BLE001
                            sector_cache[sidx] = None
                    sbars = sector_cache[sidx]
            out = evaluate(row_to_dict(r), bars_cache[sym], bench or None, sector_bench=sbars)
            r.eval_attempts = int(getattr(r, "eval_attempts", 0) or 0) + 1
            if out["eval_status"] == "pending":
                od = _iso(r.obs_date)
                if od and (today - date.fromisoformat(od)).days > GIVE_UP_DAYS:
                    r.eval_status = "no_data"
                    res["no_data"] += 1
                else:
                    res["waiting"] += 1
                continue
            for k in _OUTCOME_COLS:
                if k in out:
                    setattr(r, k, out[k])
            r.evaluated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            res["evaluated"] += 1
            if out["eval_status"] == "done":
                res["done"] += 1
        except Exception:  # noqa: BLE001
            res["errors"] += 1
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        res["errors"] += 1
    return res


def report(db: Any, M: Any, days: int = 60, min_n: int = 30, limit: int = 20000, today: Optional[date] = None) -> Dict[str, Any]:
    today = today or date.today()
    since = (today - timedelta(days=max(1, min(int(days), 400)))).isoformat()
    rows = [row_to_dict(r) for r in db.query(M).order_by(M.id.desc()).limit(limit).all()]
    rows = [r for r in rows if r.get("obs_date") and r["obs_date"] >= since]
    out = summarize(rows, min_n=min_n)
    out["window_days"] = days
    out["since"] = since
    return out
