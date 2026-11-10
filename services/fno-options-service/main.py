"""fno-options-service - NIFTY / SENSEX index-options (F&O) engine: data views, signals, DEMO trades, backtests, and a guarded REAL execution path.

Port 8007 (FNO / PORT). Every /fno/* route and /control/* needs the same admin bearer token as the other pages. The engine ships OFF, in DEMO mode, with REAL orders locked
(see config.py and fno/execution.py). Run the tests with:  cd services/fno-options-service && python3 -m pytest tests -q
"""
import control.boot  # noqa: F401  (applies saved Calibration overrides to config before anything reads it - must stay the first import)

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import config
from auth import require_admin, require_admin_or_runner
from backtest import metrics as bt_metrics, portfolio as bt_portfolio, replay as bt_replay, report as bt_report, sim as bt_sim
from fno import analytics, costs, creds as dhan_creds, engine as eng, execution, gates, probecheck, readiness, recorder, signals, stages, strategies
from fno import capital as fno_capital
from fno import liveparams
from fno.params import Params
from fno.provider import HttpProvider
from fno.runtime import Runner, market_open
from fno.store import Store, day_of

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("fno-options-service")

app = FastAPI(title="fno-options-service", version="1.0.0")

os.makedirs(config.DATA_DIR, exist_ok=True)
store = Store(os.path.join(config.DATA_DIR, "fno.db"))
provider = HttpProvider(config.MARKET_DATA_URL, config.MARKET_DATA_TIMEOUT_S, daily_exclude_today=config.DAILY_EXCLUDE_TODAY)
broker = execution.DhanBroker(config.DHAN_BASE_URL, config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN, config.ORDER_TIMEOUT_S) if (config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN) else None
engine = eng.Engine(store, provider, config, broker=broker, holidays=tuple(config.HOLIDAYS))
runner = Runner(engine, config, config.DATA_DIR)

# Real Money Calibration switches (GET/POST/DELETE /control/..., admin session required). See control/kit.py - the same engine as real-trade / position-stocks.
from control import kit as _control_kit  # noqa: E402
from control.catalog import CATALOG as _CONTROL_CATALOG  # noqa: E402

try:  # a bug in the control page must never stop the service from booting
    from control.guard import FnoController as _FnoController  # noqa: E402  (round 286: the page refuses a time that cannot work with the other times)
    _control = _FnoController("fno-options-service", _CONTROL_CATALOG, restart_hint="docker compose restart fno-options-service")
    _control_kit.mount(app, _control, require_admin)
except Exception as _control_err:  # noqa: BLE001
    log.error("control routes NOT mounted (%s: %s)", type(_control_err).__name__, _control_err)


@app.on_event("startup")
def _startup() -> None:
    if os.getenv("FNO_RUNNER", "1") != "0":
        runner.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    runner.stop()


def _today() -> str:
    return day_of(time.time())


def _locks() -> Dict[str, Any]:
    ok, closed = execution.real_allowed(config, store.kv_get("fno_armed_day"), _today())
    return {"engine_enabled": config.FNO_ENGINE_ENABLED, "mode": str(config.FNO_MODE).upper(), "real_enabled": config.FNO_REAL_ENABLED, "armed_today": store.kv_get("fno_armed_day") == _today(),
            "broker_configured": broker is not None, "real_orders_possible": ok, "closed_locks": closed}


# ── health / status ──────────────────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "fno-options-service", "runner_alive": runner.alive(), "engine_enabled": config.FNO_ENGINE_ENABLED, "mode": str(config.FNO_MODE).upper()}


@app.get("/fno/overview")
def overview(admin=Depends(require_admin)) -> Dict[str, Any]:
    mode = str(config.FNO_MODE).upper() if str(config.FNO_MODE).upper() in ("DEMO", "REAL") else "DEMO"
    opens = []
    for p in store.open_positions():
        ch = provider.chain(p.underlying, p.expiry)
        row = ch.row(p.strike) if ch else None
        side = row.side(p.opt_type) if row else None
        prem = side.mid if side else None
        unreal = round((prem - p.entry_premium) * p.qty, 2) if prem else None
        est = None                                       # Round 270: what closing NOW would cost (same costs.round_trip as the stored charges)
        if prem:
            try:
                est = round(costs.round_trip(p.underlying, p.entry_premium, prem, p.qty, liveparams.current(), child=p.parent_id is not None).total, 2)
            except Exception:  # noqa: BLE001
                est = None
        opens.append({**p.to_json(), "label": p.label(), "premium": prem, "unrealized": unreal, "est_charges": est, "exit_doubt": bool(p.broker_order_ids.get("exit_doubt")), "stop_doubt": bool(p.broker_order_ids.get("sl_doubt")),
                      "unrealized_net": round(unreal - est, 2) if unreal is not None and est is not None else None})
    return {"now": datetime.now().isoformat(timespec="seconds"), "market_open": market_open(eng.now_ist(), config.HOLIDAYS), "locks": _locks(), "day": store.day_summary(_today(), mode),
            "open_positions": opens, "last_tick": engine.last_tick, "runner": {"alive": runner.alive(), "error": runner.last_error, "snapshots_today": runner.snapshots_today},
            "underlyings": engine.underlyings(), "params": liveparams.current().to_json(), "param_problems": gates.param_problems(liveparams.current()),
            "switches": {"underlyings": str(config.FNO_UNDERLYINGS).upper(), "record_snapshots": bool(config.FNO_RECORD_SNAPSHOTS)}}


@app.get("/fno/capital")
def capital_board_status(admin=Depends(require_admin)) -> Dict[str, Any]:
    """Round 242: the three-way capital split as F&O sees it: the board Real Trade writes, and the rupees REAL sizing would use (DEMO keeps FNO_DEMO_CAPITAL). Read only."""
    out = fno_capital.status(config, store.kv_get("fno_armed_day"), _today())
    out["switch"] = str(config.CAPITAL_ALLOC_MODE)      # the live value of the calibration switch CAPITAL_ALLOC_MODE (engine and runner read the same config module)
    return out


def _readiness() -> Dict[str, Any]:
    board = fno_capital.status(config, store.kv_get("fno_armed_day"), _today())
    return readiness.collect(store, config, config.DATA_DIR, engine.last_reconcile, board)


@app.get("/fno/readiness")
def readiness_list(admin=Depends(require_admin)) -> Dict[str, Any]:
    """Round 262: may REAL orders start? Green / red / unknown items from DEMO evidence, saved rolling backtests, the capital board, reconcile and the checks only you can make in Dhan.
    While a hard item is not green REAL stays closed (switch FNO_REAL_REQUIRES_READINESS). The list says what has not been shown yet; it never says 'safe'."""
    r = _readiness()
    config.FNO_READINESS_BLOCKERS = list(r["blockers"])               # refresh what execution.real_allowed reads
    return {**r, "real_waits_for_it": bool(config.FNO_REAL_REQUIRES_READINESS), "locks": _locks(),
            "thresholds": {"demo_days": readiness.MIN_DEMO_DAYS, "demo_trades": readiness.MIN_DEMO_TRADES, "backtest_trades": readiness.MIN_BACKTEST_TRADES, "snapshot_days": readiness.MIN_SNAPSHOT_DAYS}}


class ReadinessConfirm(BaseModel):
    key: str
    ok: bool = True


@app.post("/fno/readiness/confirm")
def readiness_confirm(body: ReadinessConfirm, admin=Depends(require_admin)) -> Dict[str, Any]:
    """You confirm (or withdraw) one check only Dhan can show: segment active, Trader Control ON, static IP, probe charges matched. Logged in the journal."""
    if body.key not in readiness.MANUAL:
        raise HTTPException(status_code=400, detail=f"key must be one of {list(readiness.MANUAL)}")
    readiness.set_manual(store, body.key, body.ok)
    store.add_event("manual", {"action": "readiness_confirm", "key": body.key, "ok": body.ok, "by": str(admin.get("sub"))})
    return readiness_list(admin)


@app.get("/fno/pipeline")
def pipeline(admin=Depends(require_admin)) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    md = provider._get("/dhan/options/contracts", {"underlying": "NIFTY", "limit": 1})
    steps.append({"step": "market-data reachable", "ok": md is not None, "detail": provider.last_error or ""})
    st = (md or {}).get("status") or {}
    steps.append({"step": "option contracts loaded (scrip master)", "ok": bool(st.get("contracts")), "detail": json.dumps({"contracts": st.get("contracts"), "lot_sizes": st.get("lot_sizes")})})
    for und in ("NIFTY", "SENSEX"):
        ex = provider.expiries(und)
        steps.append({"step": f"{und} expiries", "ok": bool(ex), "detail": ", ".join(ex[:3])})
    steps.append({"step": "India VIX", "ok": provider.vix() is not None, "detail": str(provider.vix())})
    steps.append(gates.settings_step(liveparams.current()))             # round 290: boot-time env values the page's guard never saw
    steps.append({"step": "engine runner thread", "ok": runner.alive(), "detail": runner.last_error or ""})
    lt = engine.last_tick
    steps.append({"step": "last engine cycle", "ok": bool(lt.get("ok")) if lt.get("ts") else None, "detail": (lt.get("error") or "") + (f" ({int(time.time() - lt['ts'])}s ago)" if lt.get("ts") else "no cycle yet")})
    steps.append({"step": "snapshots recorded today", "ok": runner.snapshots_today > 0 or not market_open(eng.now_ist(), config.HOLIDAYS), "detail": str(runner.snapshots_today)})
    steps.append({"step": "recorded days available for replay", "ok": None, "detail": str(len(recorder.list_days(config.DATA_DIR)))})
    stg: List[Dict[str, Any]] = []
    try:                                                              # round 264: the same facts as ordered stages (market data > ... > reconcile); never breaks the flat list above
        ok_of = {x["step"]: x["ok"] for x in steps}
        evs = {k: store.events(k, stages.EVENT_LIMIT) for k in stages.KINDS}
        stg = stages.build({"now": time.time(), "enabled": bool(config.FNO_ENGINE_ENABLED), "market_open": market_open(eng.now_ist(), config.HOLIDAYS),
                            "md_ok": bool(ok_of.get("market-data reachable")), "vix_ok": bool(ok_of.get("India VIX")),
                            "expiries_ok": bool(ok_of.get("NIFTY expiries")) and bool(ok_of.get("SENSEX expiries")),
                            "md_detail": provider.last_error or "", "last_tick": engine.last_tick, "events": evs, "open_positions": len(store.open_positions()),
                            "broker": broker is not None, "reconcile": engine.last_reconcile}, day_of)
    except Exception:  # noqa: BLE001
        log.exception("pipeline stages failed")
        stg = []
    return {"steps": steps, "stages": stg, "stage_summary": stages.summary(stg) if stg else None, "locks": _locks()}


# ── round 265: the real-order probe's result and its charges against Dhan's contract note ───────────────────────────────────────────────────────
PROBE_NOTE_KV = "fno_probe_note"


class ProbeNote(BaseModel):
    file: Optional[str] = None                  # a probe_<time>.json name; blank = the newest
    brokerage: Optional[float] = None
    stt: Optional[float] = None
    exchange: Optional[float] = None
    sebi: Optional[float] = None
    gst: Optional[float] = None
    stamp: Optional[float] = None
    total: Optional[float] = None


def _probe_dir() -> str:
    return probecheck.PROBE_DIR if os.path.isdir(probecheck.PROBE_DIR) else os.path.join(config.DATA_DIR, "fno_real_probe")


def _probe_doc(file: Optional[str]) -> Dict[str, Any]:
    d = _probe_dir()
    name, probe = probecheck.load(d, file)
    doc: Dict[str, Any] = {"dir": d, "dir_exists": os.path.isdir(d), "files": probecheck.list_files(d)[:20], "probe": None, "note": None, "comparison": None}
    if probe is None:
        return doc
    summ = probecheck.summarize(probe, name or "")
    saved = (store.kv_get(PROBE_NOTE_KV, {}) or {}).get(name or "")
    doc.update(probe=summ, note=saved, comparison=probecheck.compare(probe, (saved or {}).get("lines") or {}, Params.from_config(config)))
    return doc


@app.get("/fno/probe")
def probe_result(file: Optional[str] = None, admin=Depends(require_admin)) -> Dict[str, Any]:
    """The newest (or a named) real-order probe result: what was sent, what Dhan said, what filled, gross / charges / net, and the charges check against the note you entered."""
    if file and not probecheck.NAME_RE.match(file):
        raise HTTPException(status_code=400, detail="file must look like probe_YYYYMMDD_HHMMSS.json")
    return _probe_doc(file)


@app.post("/fno/probe/note")
def probe_note(body: ProbeNote, admin=Depends(require_admin)) -> Dict[str, Any]:
    """Save the six charge lines you copied from Dhan's contract note for one probe and compare them with our model. Read-only toward the engine: no rate is changed and the readiness
    item stays yours to confirm."""
    if body.file and not probecheck.NAME_RE.match(body.file):
        raise HTTPException(status_code=400, detail="file must look like probe_YYYYMMDD_HHMMSS.json")
    name, probe = probecheck.load(_probe_dir(), body.file)
    if probe is None:
        raise HTTPException(status_code=404, detail="no probe result found")
    lines = {k: getattr(body, k) for k in (*probecheck.LINES, "total") if getattr(body, k) is not None}
    if any(v < 0 for v in lines.values()):
        raise HTTPException(status_code=400, detail="a charge cannot be negative")
    cur = dict(store.kv_get(PROBE_NOTE_KV, {}) or {})
    cur[name or ""] = {"lines": lines, "ts": time.time()}
    for old in sorted(cur, key=lambda k: cur[k].get("ts", 0.0))[:-20]:
        cur.pop(old, None)
    store.kv_set(PROBE_NOTE_KV, cur)
    store.add_event("manual", {"action": "probe_note", "file": name, "lines": lines, "by": str(admin.get("sub"))})
    return _probe_doc(name)


# ── data views ───────────────────────────────────────────────────────────────────────────────────────────────
@app.get("/fno/expiries")
def expiries(underlying: str = "NIFTY", admin=Depends(require_admin)) -> Dict[str, Any]:
    u = underlying.upper()
    if u not in ("NIFTY", "SENSEX"):
        raise HTTPException(status_code=400, detail="underlying must be NIFTY or SENSEX")
    return {"underlying": u, "expiries": provider.expiries(u)}


@app.get("/fno/chain")
def chain(underlying: str = "NIFTY", expiry: str = "", admin=Depends(require_admin)) -> Dict[str, Any]:
    u = underlying.upper()
    if u not in ("NIFTY", "SENSEX"):
        raise HTTPException(status_code=400, detail="underlying must be NIFTY or SENSEX")
    if expiry and not _DAY_RE.match(expiry):
        raise HTTPException(status_code=400, detail="expiry must be YYYY-MM-DD")
    p = liveparams.current()
    exp = expiry or engine.pick_expiry(u, eng.now_ist().date(), p) or ""
    ch = provider.chain(u, exp) if exp else None
    if ch is None:
        raise HTTPException(status_code=503, detail="option chain unavailable (market-data / Dhan)")
    summ = analytics.chain_summary(ch, p.strikes_each_side)
    bias = signals.index_bias(provider.daily(u), provider.intraday(u), summ, p)
    return {"chain": ch.to_json(), "summary": summ, "bias": bias, "expiries": provider.expiries(u), "age_s": round(ch.age_s(), 1)}


@app.get("/fno/strategies")
def strategies_view(days: int = Query(30, le=400), admin=Depends(require_admin)) -> Dict[str, Any]:
    """Every strategy: its switch, its rule, today's entries and the last `days` of closed trades. Plus the ad names that were NOT built, and why."""
    mode = str(config.FNO_MODE).upper() if str(config.FNO_MODE).upper() in ("DEMO", "REAL") else "DEMO"
    today = store.day_summary(_today(), mode).get("by_strategy", {})
    since = day_of(time.time() - days * 86400)
    closed = [p for p in store.positions(mode=mode, since_day=since, limit=5000) if p.status == "CLOSED"]
    rows = []
    on = liveparams.strategy_switches()
    report_cap = fno_capital.report_capital(config, mode)          # round 251: a REAL report is measured against the board's capital, not FNO_DEMO_CAPITAL
    for k in strategies.ORDER:
        spec = strategies.STRATEGIES[k]
        o = bt_metrics.summarize([p for p in closed if (p.strategy or "CORE") == k], report_cap)["overall"]
        rows.append({"key": k, "label": spec["label"], "ad": spec["ad"], "sides": spec["sides"], "rule": spec["rule"], "flag": spec["flag"], "expiry_day_only": bool(spec.get("expiry")),
                     "enabled": on[k], "today": today.get(k, {"entries": 0, "open": 0, "closed": 0, "net": 0.0}),
                     "recent": {"days": days, "trades": o["trades"], "net": o["net"], "win_rate_pct": o["win_rate_pct"], "profit_factor": o["profit_factor"], "max_drawdown": o["max_drawdown"]}})
    return {"mode": mode, "strategies": rows, "not_built": strategies.NOT_BUILT, "max_per_day": int(config.FNO_STRATEGY_MAX_PER_DAY),
            "expiry_rules": {"entry_end": config.FNO_EXPIRY_ENTRY_END, "squareoff_at": config.FNO_EXPIRY_SQUAREOFF_AT, "risk_mult": config.FNO_EXPIRY_RISK_MULT, "real_allowed": bool(config.FNO_EXPIRY_REAL)},
            "note": "The strategy names come from an Instagram ad that showed names only. The rules are this project's own and unproven: DEMO / SIM evidence first, REAL never by default."}


@app.get("/fno/signals")
def signals_log(limit: int = Query(100, le=500), underlying: Optional[str] = None, admin=Depends(require_admin)) -> Dict[str, Any]:
    out = []
    for kind in ("signal", "skip", "veto", "strategy_error", "entry", "exit", "real_blocked", "order_failed", "stale"):
        out += store.events(kind, limit, underlying.upper() if underlying else None)
    out.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": out[:limit]}


@app.get("/fno/log")
def log_events(limit: int = Query(200, le=1000), kind: Optional[str] = None, admin=Depends(require_admin)) -> Dict[str, Any]:
    return {"events": store.events(kind, limit)}


@app.get("/fno/positions")
def positions(status: str = "all", mode: Optional[str] = None, day: Optional[str] = None, limit: int = Query(200, le=2000), admin=Depends(require_admin)) -> Dict[str, Any]:
    st = status.upper() if status.lower() in ("open", "closed") else None      # round 276: filtered in the query, before LIMIT (an old leftover OPEN row used to fall off the newest-200 list)
    ps = store.positions(mode=mode.upper() if mode else None, day=day, limit=limit, status=st)
    return {"positions": [{**p.to_json(), "label": p.label()} for p in ps]}


@app.get("/fno/history")
def history(days: int = Query(30, le=400), mode: str = "DEMO", admin=Depends(require_admin)) -> Dict[str, Any]:
    mode = mode.upper()                                              # round 251: "real" / "demo" used to match nothing
    since = day_of(time.time() - days * 86400)
    ps = [p for p in store.positions(mode=mode, since_day=since, limit=5000) if p.status == "CLOSED"]
    m = bt_metrics.summarize(ps, fno_capital.report_capital(config, mode))
    return {"metrics": m, "verdict": bt_metrics.verdict(m), "trades": [{**p.to_json(), "label": p.label()} for p in ps]}


@app.get("/fno/charges")
def charges(days: int = Query(30, le=400), mode: str = "DEMO", admin=Depends(require_admin)) -> Dict[str, Any]:
    mode = mode.upper()
    p = liveparams.current()
    since = day_of(time.time() - days * 86400)
    tot = {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "gst": 0.0, "stamp": 0.0, "total": 0.0}
    rows = []
    for pos in store.positions(mode=mode, since_day=since, limit=5000):
        if pos.status != "CLOSED" or pos.exit_premium is None:
            continue
        c = costs.round_trip(pos.underlying, pos.entry_premium, pos.exit_premium, pos.qty, p, child=pos.parent_id is not None)
        for k in tot:
            tot[k] += getattr(c, k)
        rows.append({"id": pos.id, "label": pos.label(), "turnover": round((pos.entry_premium + pos.exit_premium) * pos.qty, 2), **c.to_json()})
    return {"totals": {k: round(v, 2) for k, v in tot.items()}, "trades": rows, "rates": {k: v for k, v in p.to_json().items() if k in (
        "brokerage_per_order", "brokerage_pct_cap", "stt_sell_pct", "exch_txn_pct_nse", "exch_txn_pct_bse", "sebi_pct", "stamp_buy_pct", "gst_pct")},
        "note": "rates are the configured values (FNO_* env); compare them with a real contract note"}


# ── actions ──────────────────────────────────────────────────────────────────────────────────────────────────────
@app.get("/fno/reconcile")
def reconcile(refresh: bool = False, admin=Depends(require_admin)) -> Dict[str, Any]:
    """Our open REAL positions vs Dhan's net F&O positions. Read-only; refresh=true asks Dhan now."""
    return engine.reconcile(eng.now_ist(), force=bool(refresh)) if broker is not None else {"ts": None, "ok": None, "note": "no Dhan credentials loaded (ARM borrows them)"}


@app.post("/fno/squareoff")
def squareoff(admin=Depends(require_admin)) -> Dict[str, Any]:
    closed = engine.squareoff_all()
    store.add_event("manual", {"action": "squareoff", "closed": closed, "by": str(admin.get("sub"))})
    return {"closed": closed}


@app.post("/fno/exit-doubt/clear")
def exit_doubt_clear(position: int, admin=Depends(require_admin)) -> Dict[str, Any]:
    """Round 282: an exit SELL whose call failed (network / HTTP 5xx) may be live at Dhan, so the engine sends no second SELL and no stop until Dhan's positions show the contract flat.
    Once you have looked in the Dhan app and the SELL is NOT there, this clears the mark and the engine may sell again. It never places an order itself. Logged in the journal."""
    out = engine.clear_exit_doubt(position)
    if out.get("error"):
        raise HTTPException(status_code=404, detail=out["error"])
    return out


@app.post("/fno/stop-doubt/clear")
def stop_doubt_clear(position: int, admin=Depends(require_admin)) -> Dict[str, Any]:
    """Round 292: a stop-loss order whose call failed (network / HTTP 5xx) may be resting at Dhan, so the engine places no second stop until Dhan's order lookup (by the correlationId it sent) says what became of it.
    If that lookup cannot answer, you can look in the Dhan app yourself: when no stop is there (or you cancelled it), this clears the mark and the engine may place a stop again. It never places an order itself. Logged."""
    out = engine.clear_stop_doubt(position)
    if out.get("error"):
        raise HTTPException(status_code=404, detail=out["error"])
    return out


_borrowed: Dict[str, Any] = {"prev": None}      # set while the Dhan token on config was borrowed from real-trade-service (memory only)


@app.post("/fno/arm")
def arm(authorization: str = Header(default=""), admin=Depends(require_admin)) -> Dict[str, Any]:
    note = "env credentials"
    if not (config.DHAN_CLIENT_ID and config.DHAN_ACCESS_TOKEN) or _borrowed["prev"] is not None:
        try:
            c = dhan_creds.fetch(config.REAL_TRADE_URL, authorization, config.MARKET_DATA_TIMEOUT_S)
            prev = dhan_creds.apply(config, c)
            if _borrowed["prev"] is None:
                _borrowed["prev"] = prev
            engine.broker = execution.DhanBroker(config.DHAN_BASE_URL, config.DHAN_CLIENT_ID, config.DHAN_ACCESS_TOKEN, config.ORDER_TIMEOUT_S)
            globals()["broker"] = engine.broker
            note = f"borrowed from real-trade-service ({c.get('dhan_env') or '?'})"
        except dhan_creds.CredsError as e:
            note = f"no Dhan credentials: {e}"
    store.kv_set("fno_armed_day", _today())
    store.add_event("manual", {"action": "arm", "by": str(admin.get("sub")), "credentials": note})
    return {"armed_day": _today(), "credentials": note, "locks": _locks()}


@app.post("/fno/disarm")
def disarm(admin=Depends(require_admin)) -> Dict[str, Any]:
    store.kv_set("fno_armed_day", None)
    if _borrowed["prev"] is not None:
        dhan_creds.drop(config, _borrowed["prev"])
        _borrowed["prev"] = None
        engine.broker = None
        globals()["broker"] = None
    store.add_event("manual", {"action": "disarm", "by": str(admin.get("sub"))})
    return {"locks": _locks()}


# ── backtests (replay and a quick sim run inside the service; the file-based EOD / SIM runs are scripts/fno_*_backtest.py) ──────────
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BacktestBody(BaseModel):
    kind: str                                   # replay | sim
    underlyings: List[str] = ["NIFTY"]
    days: Optional[List[str]] = None            # replay: recorded days (default all)
    overrides: Dict[str, Any] = {}              # Params fields for sim; FNO_* config names for replay
    strategy: str = "CORE"                      # sim only: one strategy name (fno/strategies.py) or ALL (every strategy side by side)


def _run_job(job_id: str, body: BacktestBody) -> None:
    try:
        if body.kind == "replay":
            days = body.days or recorder.list_days(config.DATA_DIR)
            daily = {u: provider.daily(u) for u in body.underlyings}
            res = bt_replay.run(config.DATA_DIR, days, daily, body.overrides, body.underlyings, list(config.HOLIDAYS))   # round 276: holidays (they decide the first trading day of the week and the holiday-shifted expiry)
        else:
            u = body.underlyings[0]
            p = liveparams.current().with_(**body.overrides)
            hist = provider.intraday_history(u) if hasattr(provider, "intraday_history") else provider.intraday(u)
            res = (bt_portfolio.run(u, provider.daily(u), hist, None, p, holidays=list(config.HOLIDAYS)) if body.strategy.upper() == "ALL"
                   else bt_sim.run(u, provider.daily(u), hist, None, p, strategy=body.strategy.upper(), holidays=list(config.HOLIDAYS)))
            res["notes"].append("data: market-data daily + the intraday candles it serves (a few recent days only); use scripts/fno_sim_backtest.py with CSV files for a long run")
        path = bt_report.save(config.DATA_DIR, bt_report.job_name(body.kind, body.underlyings, body.strategy), res)       # round 285: kind + index + strategy in the name, never overwritten
        txt = path[:-5] + ".txt"
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write(bt_report.render_text(res))
        with _jobs_lock:
            _jobs[job_id].update(state="done", file=os.path.basename(path), trades=res["metrics"]["overall"]["trades"], verdict=res["verdict"]["status"])
    except Exception as e:  # noqa: BLE001
        log.exception("backtest job failed")
        with _jobs_lock:
            _jobs[job_id].update(state="failed", error=f"{type(e).__name__}: {str(e)[:200]}")


@app.post("/fno/backtests/run-scheduled")
def backtest_run_scheduled(kind: str = "sim", underlyings: str = "NIFTY,SENSEX", strategy: str = "ALL", admin=Depends(require_admin_or_runner)) -> Dict[str, Any]:
    """No-body variant for the calibration-runner's scheduled pack. A replay with no recorded day is skipped (200, job None), never an error."""
    unds = [u.strip().upper() for u in underlyings.split(",") if u.strip()]
    if kind == "replay" and not recorder.list_days(config.DATA_DIR):
        return {"job": None, "skipped": "no recorded chain days yet"}
    if kind == "sim":
        unds = unds[:1] or ["NIFTY"]          # the sim runs one index per job
    return backtest_run(BacktestBody(kind=kind, underlyings=unds, strategy=strategy), admin)


@app.get("/fno/backtests/latest-job")
def backtest_latest_job(admin=Depends(require_admin_or_runner)) -> Dict[str, Any]:
    with _jobs_lock:
        jobs = list(_jobs.values())
    return jobs[-1] if jobs else {"state": "none"}


@app.post("/fno/backtests/run")
def backtest_run(body: BacktestBody, admin=Depends(require_admin)) -> Dict[str, Any]:
    if body.kind not in ("replay", "sim"):
        raise HTTPException(status_code=400, detail="kind must be replay or sim")
    if body.days is not None and any(not _DAY_RE.match(str(d)) for d in body.days):
        raise HTTPException(status_code=400, detail="days must be YYYY-MM-DD (they name folders under snapshots/)")
    if any(u.upper() not in ("NIFTY", "SENSEX") for u in body.underlyings) or not body.underlyings:
        raise HTTPException(status_code=400, detail="underlyings must be NIFTY and/or SENSEX")
    body.underlyings = [u.upper() for u in body.underlyings]
    if body.kind == "sim" and body.strategy.upper() != "ALL" and body.strategy.upper() not in strategies.STRATEGIES:
        raise HTTPException(status_code=400, detail=f"strategy must be ALL or one of {list(strategies.ORDER)}")
    with _jobs_lock:
        if any(j["state"] == "running" for j in _jobs.values()):
            raise HTTPException(status_code=409, detail="a backtest is already running")
        job_id = f"{body.kind}-{int(time.time())}-{len(_jobs)}"        # round 16: kind + second alone repeated when a job ended and the next of the same kind started within the same second, and the new record overwrote the old one
        _jobs[job_id] = {"id": job_id, "kind": body.kind, "state": "running", "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, body), daemon=True).start()
    return {"job": job_id}


@app.get("/fno/backtests")
def backtests(admin=Depends(require_admin_or_runner)) -> Dict[str, Any]:
    d = os.path.join(config.DATA_DIR, "backtests")
    files = []
    if os.path.isdir(d):
        jsons = [f for f in os.listdir(d) if f.endswith(".json")]
        for f in sorted(jsons, key=lambda n: (os.path.getmtime(os.path.join(d, n)), n), reverse=True)[:200]:      # round 285: newest first by write time (the names now differ by index / strategy, so a name sort is no longer chronological)
            try:
                r = json.load(open(os.path.join(d, f), encoding="utf-8"))
                o = (r.get("metrics") or {}).get("overall") or {}
                files.append({"name": f, "kind": r.get("kind"), "underlying": r.get("underlying"), "from": r.get("from"), "to": r.get("to"), "days": r.get("days"),
                              "trades": o.get("trades"), "net": o.get("net"), "win_rate_pct": o.get("win_rate_pct"), "max_drawdown": o.get("max_drawdown"),
                              "verdict": (r.get("verdict") or {}).get("status")})
            except (OSError, ValueError):
                continue
    with _jobs_lock:
        jobs = list(_jobs.values())[-10:]
    return {"results": files, "jobs": jobs, "recorded_days": recorder.list_days(config.DATA_DIR), "snapshot_days": store.snapshot_days()[-60:]}


def _safe_result_path(name: str) -> str:
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="bad name")
    p = os.path.join(config.DATA_DIR, "backtests", name)
    if not os.path.exists(p):
        raise HTTPException(status_code=404, detail="no such result")
    return p


@app.get("/fno/backtests/{name}")
def backtest_result(name: str, admin=Depends(require_admin)) -> Dict[str, Any]:
    with open(_safe_result_path(name), encoding="utf-8") as fh:
        return json.load(fh)


@app.get("/fno/backtests/{name}/text", response_class=PlainTextResponse)
def backtest_text(name: str, admin=Depends(require_admin)) -> str:
    with open(_safe_result_path(name), encoding="utf-8") as fh:
        return bt_report.render_text(json.load(fh))
