"""calibration-runner-service (group 442): runs the calibration / evidence / report scripts for the Real Money Calibration page and keeps their txt output.

Auth: every route except /health needs the admin session bearer token (same SESSION_SECRET as the other services).
It never changes a trading switch: the switches live in real-trade-service and position-stocks-service (GET/POST /control/*); this service only
runs read-only reports, the history downloads, the backtests, and the few POSTs a person would otherwise type (retrain, evaluate, structure refresh).
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import config
import jobs as J
from auth import mint_service_token, require_admin
from runner import Runner
from scheduler import Scheduler

app = FastAPI(title="calibration-runner-service", version="1.0.0")
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

_tok: Dict[str, Any] = {"v": "", "exp": 0.0}


def _token() -> str:
    import time
    if time.time() > _tok["exp"] - 120:
        _tok["v"] = mint_service_token()
        _tok["exp"] = time.time() + config.TOKEN_TTL_S
    return _tok["v"]


def _ctx_base() -> Dict[str, Any]:
    return {"python": config.PYTHON, "scripts": str(config.SCRIPTS_DIR), "bars": str(config.BAR_DIR), "rt": config.REAL_TRADE_URL, "ps": config.POSITION_STOCKS_URL,
            "md": config.MARKET_DATA_URL, "gw": config.GATEWAY_URL, "analysis": config.ANALYSIS_URL, "decision": config.DECISION_URL, "notify": config.NOTIFICATION_URL,
            "kronos": config.KRONOS_URL, "tsfm": config.TIMESFM_URL}


runner: Optional[Runner] = None
sched: Optional[Scheduler] = None


def _safe_token() -> str:
    try:
        return _token()
    except Exception:  # noqa: BLE001 - SESSION_SECRET missing: jobs that need a token fail with a clear message, redaction just has nothing to hide
        return ""


@app.on_event("startup")
def _startup() -> None:
    global runner, sched
    from notify import make_notifier, make_skip_notifier
    from runner import Redactor
    notifier = make_notifier(config.NOTIFICATION_URL, config.NOTIFY_ENABLED, config.NOTIFY_MANUAL, Redactor(lambda: [_safe_token(), config.SESSION_SECRET]))
    runner = Runner(str(config.DATA_DIR), str(config.SCRIPTS_DIR), _ctx_base(), J.JOBS, J.PACK_MAP, _safe_token, extra_secrets=lambda: [config.SESSION_SECRET],
                    max_output=config.MAX_OUTPUT_BYTES, nice=config.NICE, keep_runs=config.KEEP_RUNS, keep_days=config.KEEP_DAYS, guard_market=config.GUARD_MARKET_HOURS,
                    notify_fn=notifier, keep_latest_only=config.KEEP_LATEST_ONLY)
    runner.start()
    skip_notifier = make_skip_notifier(config.NOTIFICATION_URL, config.NOTIFY_ENABLED, lambda p: (J.PACK_MAP.get(p) or {}).get("title", p or ""),
                                       Redactor(lambda: [_safe_token(), config.SESSION_SECRET]))
    sched = Scheduler(str(config.DATA_DIR / "state" / "schedule.json"), J.SCHEDULE_DEFAULT, runner.submit, list(J.PACK_MAP), enabled_default=config.SCHEDULER_ENABLED,
                      on_skip=skip_notifier)
    sched.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if runner:
        runner.shutdown()
    if sched:
        sched.stop.set()


def _r() -> Runner:
    if runner is None:
        raise HTTPException(503, "runner not started")
    return runner


@app.get("/health")
def health() -> Dict[str, Any]:
    ok_scripts = config.SCRIPTS_DIR.is_dir()
    return {"status": "ok" if ok_scripts and config.SESSION_SECRET else "degraded", "service": "calibration-runner-service", "scripts_dir": ok_scripts,
            "session_secret_set": bool(config.SESSION_SECRET), "data_dir_writable": os.access(config.DATA_DIR, os.W_OK) if config.DATA_DIR.exists() else False}


@app.get("/auth/session")
def auth_session(claims: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"ok": True, "user": claims.get("sub")}


@app.get("/catalog")
def catalog(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    c = J.public_catalog()
    c["data_present"] = _data_present()
    return c


def _data_present() -> Dict[str, Any]:
    cal = config.DATA_DIR / "calibration_out"
    exp = config.DATA_DIR / "exports"
    hist = sorted(p.name for p in cal.glob("hist_*") if p.is_dir()) if cal.exists() else []
    bars = 0
    try:
        bars = sum(1 for _ in config.BAR_DIR.glob("*")) if config.BAR_DIR.exists() else 0
    except OSError:
        pass
    return {"history_folders": hist, "exports": sorted(p.name for p in exp.glob("*.json"))[-10:] if exp.exists() else [], "bar_files": bars,
            "has_summary": (cal / "summary.txt").exists()}


class RunReq(BaseModel):
    kind: str = "job"            # job | pack
    target: str
    params: Dict[str, Any] = {}
    confirm: bool = False


@app.post("/runs")
def create_run(req: RunReq, claims: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        r = _r().submit(req.kind, req.target, req.params, by=str(claims.get("sub") or ""), trigger="manual", confirm=req.confirm)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return r.public()


@app.get("/runs")
def list_runs(limit: int = Query(60, ge=1, le=200), target: str = "", _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"state": _r().state(), "runs": _r().list_runs(limit, target)}


@app.get("/runs/{rid}")
def get_run(rid: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    d = _r().get(rid)
    if not d:
        raise HTTPException(404, "no such run")
    return d


@app.get("/runs/{rid}/tail")
def tail(rid: str, offset: int = Query(0, ge=0), _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    d = _r().tail(rid, offset)
    if not d.get("found"):
        raise HTTPException(404, "no such run")
    return d


@app.post("/runs/{rid}/cancel")
def cancel(rid: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"cancelled": _r().cancel(rid)}


@app.get("/runs/{rid}/download")
def download(rid: str, file: str = "output.txt", _: Dict[str, Any] = Depends(require_admin)):
    p = _r().file_path(rid, file)
    if not p:
        raise HTTPException(404, "no such file")
    return FileResponse(p, filename=f"{rid}_{os.path.basename(file)}", media_type="text/plain" if p.endswith((".txt", ".md", ".csv", ".json")) else "application/octet-stream")


@app.get("/runs/{rid}/zip")
def download_zip(rid: str, _: Dict[str, Any] = Depends(require_admin)):
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    if not _r().zip_run(rid, tmp.name):
        os.unlink(tmp.name)
        raise HTTPException(404, "no such run")
    return FileResponse(tmp.name, filename=f"{rid}.zip", media_type="application/zip")


@app.get("/latest/{target}")
def latest(target: str, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"run": _r().latest(target)}


@app.get("/summary")
def summary(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Last finished run of every job: what the page's checklist shows (green / amber / red / never run)."""
    out = {}
    for j in J.JOB_LIST:
        r = _r().latest(j["id"])
        out[j["id"]] = {"status": r["status"], "finished_at": r["finished_at"], "run_id": r["id"], "headline": r["headline"][:3]} if r else None
    packs = {p["id"]: (lambda r: {"status": r["status"], "finished_at": r["finished_at"], "run_id": r["id"]} if r else None)(_r().latest(p["id"])) for p in J.PACKS}
    return {"jobs": out, "packs": packs, "state": _r().state()}


@app.get("/calibration-summary")
def calibration_summary(_: Dict[str, Any] = Depends(require_admin)):
    p = config.DATA_DIR / "calibration_out" / "summary.txt"
    if not p.exists():
        raise HTTPException(404, "no calibration summary yet (run the full calibration)")
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace")[-400_000:])


class SchedReq(BaseModel):
    enabled: Optional[bool] = None
    entries: Optional[Any] = None


@app.get("/schedule")
def get_schedule(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    assert sched is not None
    return sched.get()


@app.put("/schedule")
def put_schedule(req: SchedReq, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    assert sched is not None
    try:
        return sched.put(req.enabled, req.entries)
    except ValueError as e:
        raise HTTPException(400, str(e))
