"""fno/readiness.py - Round 262, slice 3: the "may REAL orders start?" checklist, computed from data the service already has.

Each item is green (ok True), red (ok False) or unknown (ok None). Items marked `hard` are the ones the REAL lock waits for: execution.real_allowed closes a lock while any hard item is not green
(switch FNO_REAL_REQUIRES_READINESS, ON by default; the env file or the Calibration switch can turn the wait off). The list never says "safe": it only says what has not been shown yet.
`evaluate` is pure (tested without a database); `collect` / `publish` read the store, the saved rolling backtests, the capital board and the last reconcile. Nothing here raises.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import capital, strategies
from .store import CLOSED, day_of

MIN_DEMO_DAYS = 20            # about 4 trading weeks
MIN_DEMO_TRADES = 60
MIN_BACKTEST_TRADES = 30
MIN_SNAPSHOT_DAYS = 10
MAX_BACKTEST_AGE_DAYS = 14    # round 305: a rolling result saved longer ago than this no longer counts (the code, the parameters and the market have moved on); re-run scripts/fno_rolling_backtest.py
RECONCILE_MAX_AGE_S = 1200.0  # a reconcile older than 20 minutes says nothing about now
PUBLISH_EVERY_S = 30.0

# things only the person can verify in Dhan; they confirm them once on the Overview page (kept in the service's own key-value table)
MANUAL: Dict[str, str] = {
    "dhan_fno_segment": "F&O segment is active at the exchange (not only approved)",
    "dhan_trader_control": "Dhan > Trader Control: Derivatives is switched ON",
    "dhan_static_ip": "Dhan static IP is whitelisted for this server",
    "probe_charges": "Real-order probe done and its charges matched Dhan's contract note",
}
MANUAL_KV = "fno_readiness_manual"

_last_pub = {"t": 0.0}
_bt_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}      # path -> (mtime, the few fields we need): saved results can be megabytes, the list is polled


def _item(key: str, group: str, label: str, ok: Optional[bool], hard: bool, detail: str) -> Dict[str, Any]:
    return {"key": key, "group": group, "label": label, "ok": ok, "hard": hard, "detail": detail}


def _all(oks: List[Optional[bool]]) -> Optional[bool]:
    """False if any is False, True only if all are True, else unknown."""
    if any(o is False for o in oks):
        return False
    return True if oks and all(o is True for o in oks) else None


def backtest_check(r: Optional[Dict[str, Any]], now: Optional[float] = None) -> Tuple[Optional[bool], str]:
    """One saved rolling result against the Phase 1 exit rule: data_quality.ok, at least MIN_BACKTEST_TRADES trades, net positive after charges, verdict PASSED_THIS_TEST_ONLY."""
    if not r:
        return None, "no rolling backtest saved yet"
    why: List[str] = []
    if r.get("dq_ok") is not True:
        why.append("data quality not OK")
    if int(r.get("trades") or 0) < MIN_BACKTEST_TRADES:
        why.append(f"{int(r.get('trades') or 0)} trades (< {MIN_BACKTEST_TRADES})")
    if (r.get("net") or 0) <= 0:
        why.append("net after charges not positive")
    if r.get("verdict") != "PASSED_THIS_TEST_ONLY":
        why.append(f"verdict {r.get('verdict') or '?'}")
    if now is not None and r.get("mtime") is not None:                       # round 305: an old pass used to stay green for ever
        age_days = (now - float(r["mtime"])) / 86400.0
        if age_days > MAX_BACKTEST_AGE_DAYS:
            why.append(f"result saved {age_days:.0f} days ago (> {MAX_BACKTEST_AGE_DAYS}): run it again")
    tag = f"{r.get('trades')} trades, net {r.get('net')}"
    return (False, "; ".join(why)) if why else (True, tag)


def evaluate(inp: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: the input dict (see `collect`) -> items, counts and the list of hard items that are not green."""
    now = float(inp.get("now") or time.time())
    items: List[Dict[str, Any]] = []

    demo = inp.get("demo") or {}
    days, trades, net = int(demo.get("days") or 0), int(demo.get("trades") or 0), float(demo.get("net") or 0.0)
    items.append(_item("demo_days", "DEMO evidence", f"At least {MIN_DEMO_DAYS} DEMO trading days with a closed trade", days >= MIN_DEMO_DAYS, True, f"{days} day(s) so far"))
    items.append(_item("demo_trades", "DEMO evidence", f"At least {MIN_DEMO_TRADES} closed DEMO trades", trades >= MIN_DEMO_TRADES, True, f"{trades} trade(s) so far"))
    items.append(_item("demo_net", "DEMO evidence", "DEMO net after charges is positive", None if trades == 0 else net > 0, True, "no closed DEMO trade yet" if trades == 0 else f"net {round(net, 2)} over {trades} trade(s)"))

    bts = inp.get("backtests") or {}
    if not bts:
        items.append(_item("backtest", "Backtests", "Rolling backtest passed for every strategy REAL would trade", None, True, "no strategy to check (CORE is switched off?)"))
    for strat, per_und in sorted(bts.items()):
        res = {u: backtest_check(r, inp.get("now")) for u, r in per_und.items()}
        ok = _all([v[0] for v in res.values()])
        detail = " | ".join(f"{u}: {v[1]}" for u, v in res.items()) or "no underlying selected"
        items.append(_item(f"backtest_{strat.lower()}", "Backtests", f"Rolling backtest passed: {strat}", ok, True, detail))

    rec = inp.get("reconcile") or {}
    ts = rec.get("ts")
    if ts is None or rec.get("ok") is None:
        items.append(_item("reconcile", "Dhan", "Reconcile with Dhan is clean", None, True, rec.get("note") or "not checked yet (needs REAL mode with Dhan credentials)"))
    elif rec.get("ok") is False:
        diffs = rec.get("differences") or []
        items.append(_item("reconcile", "Dhan", "Reconcile with Dhan is clean", False, True, f"{len(diffs)} difference(s): check the Dhan app"))
    elif now - float(ts) > RECONCILE_MAX_AGE_S:
        items.append(_item("reconcile", "Dhan", "Reconcile with Dhan is clean", None, True, f"last check is {int((now - float(ts)) // 60)} min old"))
    else:
        items.append(_item("reconcile", "Dhan", "Reconcile with Dhan is clean", True, True, "our REAL positions match Dhan's"))

    board = inp.get("board") or {}
    cap = float(board.get("capital") or 0.0)
    items.append(_item("capital_board", "Capital", "A fresh capital board gives F&O money to size from", cap > 0, True,
                       f"F&O share Rs {round(cap):,}" if cap > 0 else str(board.get("why") or "no fresh capital board")))

    items.append(_item("dhan_credentials", "Dhan", "Dhan client id and access token are loaded", True if inp.get("creds") else None, False,
                       "loaded" if inp.get("creds") else "not loaded: pressing Arm today borrows them from Real Trade"))

    snap = inp.get("snapshots") or {}
    sd, st = int(snap.get("days") or 0), int(snap.get("today") or 0)
    items.append(_item("snapshot_days", "DEMO evidence", f"Chains recorded on at least {MIN_SNAPSHOT_DAYS} days (for the replay backtest)", sd >= MIN_SNAPSHOT_DAYS, True, f"{sd} day(s) recorded"))
    items.append(_item("snapshots_today", "DEMO evidence", "Chains recorded today", True if st > 0 else None, False, f"{st} snapshot(s) today" if st > 0 else "none yet today (market closed, or recording is off)"))

    manual = inp.get("manual") or {}
    for k, label in MANUAL.items():
        m = manual.get(k)
        done = isinstance(m, dict) and bool(m.get("ok"))
        items.append(_item(k, "Checks only you can make", label, True if done else None, True,
                           f"confirmed {day_of(float(m['ts']))}" if done and m.get("ts") else "you have not confirmed this yet"))
        items[-1]["manual"] = True

    blockers = [f"{i['label']} ({i['detail']})" for i in items if i["hard"] and i["ok"] is not True]
    green = sum(1 for i in items if i["ok"] is True)
    red = sum(1 for i in items if i["ok"] is False)
    return {"ts": now, "items": items, "blockers": blockers, "all_green": not blockers,
            "counts": {"green": green, "red": red, "unknown": len(items) - green - red, "hard_open": len(blockers)}}


# ── reading the service's own data ──────────────────────────────────────────────────────────────────────────────
def demo_evidence(store: Any) -> Dict[str, Any]:
    ps = [p for p in store.positions(mode="DEMO", limit=100000) if p.status == CLOSED]
    roots = [p for p in ps if p.parent_id is None]
    return {"days": len({day_of(p.entry_ts) for p in roots}), "trades": len(roots), "net": round(sum(float(p.net_pnl or 0.0) for p in ps), 2)}


def latest_rolling(data_dir: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(strategy, underlying) -> the newest saved ROLLING result (only the fields the checklist needs). Unreadable files are skipped."""
    d = os.path.join(data_dir, "backtests")
    out: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
    try:
        names = [f for f in os.listdir(d) if f.endswith(".json") and f.startswith("fno_rolling")]
    except OSError:
        return {}
    for f in names:
        path = os.path.join(d, f)
        try:
            mt = os.path.getmtime(path)
            hit = _bt_cache.get(path)
            if hit and hit[0] == mt:
                row = hit[1]
            else:
                r = json.load(open(path, encoding="utf-8"))
                o = (r.get("metrics") or {}).get("overall") or {}
                row = {"name": f, "kind": str(r.get("kind") or "").upper(), "strategy": str(r.get("strategy") or "CORE").upper(), "underlying": str(r.get("underlying") or "").upper(),
                       "trades": o.get("trades"), "net": o.get("net"), "verdict": (r.get("verdict") or {}).get("status"), "dq_ok": (r.get("data_quality") or {}).get("ok"), "mtime": mt}
                _bt_cache[path] = (mt, row)
        except (OSError, ValueError, AttributeError):
            continue
        if row["kind"] != "ROLLING":
            continue
        k = (row["strategy"], row["underlying"])
        if k not in out or mt > out[k][0]:
            out[k] = (mt, row)
    return {k: v[1] for k, v in out.items()}


def required_backtests(cfg: Any, data_dir: str) -> Dict[str, Dict[str, Optional[Dict[str, Any]]]]:
    """Per strategy REAL would trade (expiry-day ones only when FNO_EXPIRY_REAL is on), per underlying the engine trades: its newest rolling result or None."""
    und = str(getattr(cfg, "FNO_UNDERLYINGS", "BOTH")).upper()
    unds = ["NIFTY", "SENSEX"] if und == "BOTH" else [und] if und in ("NIFTY", "SENSEX") else []
    keys = strategies.enabled_keys(cfg) if bool(getattr(cfg, "FNO_EXPIRY_REAL", False)) else strategies.enabled_keys(cfg, expiry=False)
    saved = latest_rolling(data_dir)
    return {k: {u: saved.get((k, u)) for u in unds} for k in keys}


def collect(store: Any, cfg: Any, data_dir: str, reconcile: Optional[Dict[str, Any]], board: Optional[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """Gather the inputs from the live objects and `evaluate` them. A source that cannot be read leaves its items unknown (never green)."""
    t = now if now is not None else time.time()
    inp: Dict[str, Any] = {"now": t, "reconcile": reconcile or {}, "board": {}, "backtests": {}, "demo": {"days": 0, "trades": 0, "net": 0.0}, "snapshots": {}, "manual": {}}
    try:
        inp["demo"] = demo_evidence(store)
    except Exception:  # noqa: BLE001
        pass
    try:
        inp["backtests"] = required_backtests(cfg, data_dir)
    except Exception:  # noqa: BLE001
        pass
    try:
        b = board or {}
        inp["board"] = {"capital": float(b.get("real_sizing_capital") or 0.0), "why": b.get("real_sizing_source") or ""}
    except Exception:  # noqa: BLE001
        pass
    try:
        days = store.snapshot_days()
        today = day_of(t)
        inp["snapshots"] = {"days": len({r["day"] for r in days}), "today": sum(int(r["n"]) for r in days if r["day"] == today)}
    except Exception:  # noqa: BLE001
        pass
    try:
        inp["manual"] = store.kv_get(MANUAL_KV, {}) or {}
    except Exception:  # noqa: BLE001
        pass
    inp["creds"] = bool(getattr(cfg, "DHAN_CLIENT_ID", "") and getattr(cfg, "DHAN_ACCESS_TOKEN", ""))
    return evaluate(inp)


def set_manual(store: Any, key: str, ok: bool, now: Optional[float] = None) -> Dict[str, Any]:
    """Record (or withdraw) the person's confirmation of one check that only Dhan can show. Raises KeyError for an unknown key."""
    if key not in MANUAL:
        raise KeyError(key)
    cur = dict(store.kv_get(MANUAL_KV, {}) or {})
    cur[key] = {"ok": bool(ok), "ts": now if now is not None else time.time()}
    store.kv_set(MANUAL_KV, cur)
    return cur


def publish(cfg: Any, store: Any, data_dir: str, reconcile: Optional[Dict[str, Any]], armed_day: Optional[str], now: Optional[float] = None, force: bool = False) -> bool:
    """Recompute the list (at most every PUBLISH_EVERY_S) and leave the blockers on `cfg.FNO_READINESS_BLOCKERS`, where execution.real_allowed reads them.
    If the list cannot be computed the blocker is the failure itself: REAL stays closed (fail closed). Never raises."""
    try:
        t = now if now is not None else time.time()
        if not force and t - _last_pub["t"] < PUBLISH_EVERY_S:
            return False
        _last_pub["t"] = t
        try:
            r = collect(store, cfg, data_dir, reconcile, capital.status(cfg, armed_day, day_of(t)), t)
            cfg.FNO_READINESS_BLOCKERS = list(r["blockers"])
        except Exception as e:  # noqa: BLE001
            cfg.FNO_READINESS_BLOCKERS = [f"the readiness list could not be computed ({type(e).__name__})"]
        return True
    except Exception:  # noqa: BLE001
        return False
