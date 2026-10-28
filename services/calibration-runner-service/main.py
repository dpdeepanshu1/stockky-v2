"""calibration-runner-service (group 442): runs the calibration / evidence / report scripts for the Real Money Calibration page and keeps their txt output.

Auth: every route except /health needs the admin session bearer token (same SESSION_SECRET as the other services).
It never changes a trading switch: the switches live in real-trade-service and position-stocks-service (GET/POST /control/*); this service only
runs read-only reports, the history downloads, the backtests, and the few POSTs a person would otherwise type (retrain, evaluate, structure refresh).
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import auth
import config
import jobs as J
import session_keys
from auth import mint_service_token, require_admin
from runner import Runner
from scheduler import Scheduler

app = FastAPI(title="calibration-runner-service", version="1.0.0")
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

def _all_secrets() -> list:
    """Every session secret this process holds, so job logs / notifications can redact them."""
    return [v for v in (session_keys.own_secret(k) for k in session_keys.SERVICE_ENV) if v]


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
auto: Optional[Any] = None       # group 476: AutoCalibrator
weather: Optional[Any] = None    # Part 1 step 4: Market Weather (shadow)
weather_control: Optional[Any] = None   # E112: the same ControlClient, for the card's saved / today / expiry view and the revert button


def _safe_token() -> str:
    try:
        return _token()
    except Exception:  # noqa: BLE001 - SESSION_SECRET missing: jobs that need a token fail with a clear message, redaction just has nothing to hide
        return ""


@app.on_event("startup")
def _startup() -> None:
    global runner, sched, auto, weather, weather_control
    import notify as N
    from notify import make_notifier, make_skip_notifier, Outbox, start_outbox_flusher
    from runner import Redactor
    ob = Outbox(str(config.DATA_DIR / "outbox"), config.OUTBOX_MAX_AGE_S)      # group 466: an undelivered Telegram message is kept and retried
    notifier = make_notifier(config.NOTIFICATION_URL, config.NOTIFY_ENABLED, config.NOTIFY_MANUAL, Redactor(lambda: [_safe_token(), config.SESSION_SECRET, *_all_secrets()]), outbox=ob)
    import autocalib as AC
    redact = Redactor(lambda: [_safe_token(), config.SESSION_SECRET, *_all_secrets()])
    runner = Runner(str(config.DATA_DIR), str(config.SCRIPTS_DIR), _ctx_base(), J.JOBS, J.PACK_MAP, _safe_token, extra_secrets=lambda: [config.SESSION_SECRET, *_all_secrets()],
                    max_output=config.MAX_OUTPUT_BYTES, nice=config.NICE, keep_runs=config.KEEP_RUNS, keep_days=config.KEEP_DAYS, guard_market=config.GUARD_MARKET_HOURS,
                    notify_fn=notifier, keep_latest_only=config.KEEP_LATEST_ONLY, preflight_s=config.PREFLIGHT_S,
                    after_fn=lambda run: auto.on_run_finished(run, J.PACK_MAP) if auto else None,
                    workers=config.WORKERS, max_heavy=config.MAX_HEAVY, min_free_mb=config.MIN_FREE_MB)

    def _tell(title: str, message: str, urgency: str) -> Optional[str]:
        text = redact(message)
        res = N.send(config.NOTIFICATION_URL, title, text, urgency)
        if N.retryable(res) and ob.add(title, text, urgency):
            res = str(res) + " - will be sent again when the notification service is reachable"
        return res
    auto = AC.AutoCalibrator(str(config.DATA_DIR / "state" / "autocalib.json"),
                             AC.ControlClient({AC.RT: config.REAL_TRADE_URL, AC.PS: config.POSITION_STOCKS_URL}, _safe_token),
                             AC.RunnerEvidence(runner, J.PACK_MAP, config.AUTOCALIB_MAX_AGE_DAYS * 86400), notify=_tell,
                             enabled=config.AUTOCALIB_ENABLED, dry_run=config.AUTOCALIB_DRY_RUN, cooldown_s=config.AUTOCALIB_COOLDOWN_H * 3600,
                             step_pct=config.AUTOCALIB_STEP_PCT / 100.0)
    runner.start()
    start_outbox_flusher(ob, config.NOTIFICATION_URL, config.OUTBOX_RETRY_S)
    skip_notifier = make_skip_notifier(config.NOTIFICATION_URL, config.NOTIFY_ENABLED, lambda p: (J.PACK_MAP.get(p) or {}).get("title", p or ""),
                                       Redactor(lambda: [_safe_token(), config.SESSION_SECRET, *_all_secrets()]), outbox=ob)
    sched = Scheduler(str(config.DATA_DIR / "state" / "schedule.json"), J.SCHEDULE_DEFAULT, runner.submit, list(J.PACK_MAP), enabled_default=config.SCHEDULER_ENABLED,
                      on_skip=skip_notifier)
    sched.start()
    # Market Weather: shadow unless WEATHER_APPLY is on AND the evidence gate passes AND the service itself is live (weather_apply.py); weather.py itself cannot build a dial
    import weather as W
    import weather_apply as WA

    def _safe(fn):
        """E115: run one best-effort call; a failure is swallowed (the Time Machine scan trigger must never disturb Market Weather)."""
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None
    cc = AC.ControlClient({AC.RT: config.REAL_TRADE_URL, AC.PS: config.POSITION_STOCKS_URL}, _safe_token)
    weather_control = cc
    weather = W.Weather(str(config.DATA_DIR / "weather"), config.MARKET_DATA_URL, notify=_tell,
                        stamp=lambda svc, body: cc.overlay(AC.RT if svc == "real-trade" else AC.PS, body),
                        enabled=config.WEATHER_ENABLED, stamp_on=config.WEATHER_STAMP, notify_on=config.WEATHER_NOTIFY,
                        apply=WA.Applier(cc, lambda: WA.evidence_gate(lambda job, age: AC.RunnerEvidence(runner, J.PACK_MAP, age).get(job), config.WEATHER_GATE_MODE), AC.RT, AC.PS,
                                         mode_fn=lambda: config.WEATHER_GATE_MODE),
                        apply_on=config.WEATHER_APPLY,
                        after_lock=lambda: [_safe(lambda s=s: cc.time_machine_scan(s, "weather lock")) for s in (AC.RT, AC.PS)])
    weather.start()
    # same-day gate: if the gate needs a replay and none is fresh, queue it now (light job, any hour) so the gate can open today
    logging.getLogger("calibration-runner").info("weather gate %s: %s", config.WEATHER_GATE_MODE,
                                                 WA.ensure_replay(lambda job, age: AC.RunnerEvidence(runner, J.PACK_MAP, age).get(job), runner.submit, config.WEATHER_GATE_MODE))


@app.on_event("shutdown")
def _shutdown() -> None:
    if runner:
        runner.shutdown()
    if sched:
        sched.stop.set()
    if weather:
        weather.stop.set()


def _svc(service: str) -> str:
    """E235: the page tab a request comes from (rt / ps); unknown -> 400, empty = everything."""
    sv = (service or "").strip().lower()
    if sv not in ("", "rt", "ps"):
        raise HTTPException(400, "service must be rt or ps")
    return sv


def _r() -> Runner:
    if runner is None:
        raise HTTPException(503, "runner not started")
    return runner


@app.get("/health")
def health() -> Dict[str, Any]:
    ok_scripts = config.SCRIPTS_DIR.is_dir()
    return {"status": "ok" if ok_scripts and auth._any_key_configured() and session_keys.can_sign("calibration-runner", config.SESSION_SECRET) else "degraded", "service": "calibration-runner-service", "scripts_dir": ok_scripts,
            "session_secret_set": bool(config.SESSION_SECRET), "session_signing_mode": session_keys.diagnostics("calibration-runner", auth._ACCEPT, config.SESSION_SECRET)["signing_mode"], "data_dir_writable": os.access(config.DATA_DIR, os.W_OK) if config.DATA_DIR.exists() else False}


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
    resume: bool = False         # group 466: continue from the symbols a stopped run already saved
    service: str = ""            # E235: the page tab that starts it: rt | ps | "" (shared)


@app.post("/runs")
def create_run(req: RunReq, claims: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    try:
        r = _r().submit(req.kind, req.target, req.params, by=str(claims.get("sub") or ""), trigger="manual",
                        confirm=req.confirm, resume=req.resume, service=_svc(req.service))
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return r.public()


@app.get("/runs")
def list_runs(limit: int = Query(60, ge=1, le=200), target: str = "", service: str = "", _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    sv = _svc(service)
    return {"state": _r().state(sv), "runs": _r().list_runs(limit, target, sv)}


class WorkersReq(BaseModel):
    workers: int


@app.put("/runner/workers")
def set_workers(req: WorkersReq, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """E235: how many tests may run at the same time (1-8); saved, so it survives a restart."""
    return {"workers": _r().set_workers(req.workers)}


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


@app.get("/results/latest.zip")
def download_latest_zip(service: str = "", _: Dict[str, Any] = Depends(require_admin)):
    """group 471: the latest result of every test in one zip (the 'Download all latest results' button)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    sv = _svc(service)
    _r().zip_latest(tmp.name, service=sv)
    return FileResponse(tmp.name, filename=(f"latest_results_{sv}.zip" if sv else "latest_results.zip"), media_type="application/zip")


@app.get("/latest/{target}")
def latest(target: str, service: str = "", _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"run": _r().latest(target, _svc(service))}


@app.get("/summary")
def summary(service: str = "", _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Last finished run of every job: what the page's checklist shows (green / amber / red / never run)."""
    sv = _svc(service)
    out = {}
    for j in J.JOB_LIST:
        r = _r().latest(j["id"], sv)
        out[j["id"]] = {"status": r["status"], "finished_at": r["finished_at"], "run_id": r["id"], "headline": r["headline"][:3]} if r else None
    packs = {p["id"]: (lambda r: {"status": r["status"], "finished_at": r["finished_at"], "run_id": r["id"]} if r else None)(_r().latest(p["id"], sv)) for p in J.PACKS}
    return {"jobs": out, "packs": packs, "state": _r().state(sv)}


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


# ----------------------------------------------------------------------------------------------- group 476: auto-calibration
def _a() -> Any:
    if auto is None:
        raise HTTPException(503, "auto-calibration not started")
    return auto


@app.get("/autocalib/status")
def autocalib_status(coverage: bool = True, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Settings, what each switch waits for, the change history, and (coverage=true) which switch of both services is automated / protected / manual."""
    return _a().status(with_coverage=coverage)


class AutoSettings(BaseModel):
    enabled: Optional[bool] = None
    dry_run: Optional[bool] = None
    pinned: Optional[Any] = None


@app.put("/autocalib/settings")
def autocalib_settings(req: AutoSettings, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    pinned = req.pinned
    if pinned is not None and (not isinstance(pinned, list) or not all(isinstance(x, str) for x in pinned)):
        raise HTTPException(400, "pinned must be a list of switch names")
    try:
        return _a().configure(req.enabled, req.dry_run, pinned)
    except ValueError as e:
        raise HTTPException(400, str(e))


class AutoRun(BaseModel):
    dry_run: Optional[bool] = None


@app.post("/autocalib/run")
def autocalib_run(req: AutoRun, claims: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Evaluate every rule now. dry_run=true shows what would change and applies nothing (also the only mode while auto-calibration is switched off)."""
    return _a().evaluate(trigger=f"manual by {claims.get('sub') or '-'}", force=True, dry_run=req.dry_run)


# ----------------------------------------------------------------------------------------------- Part 1 step 4: Market Weather (shadow)
def _w() -> Any:
    if weather is None:
        raise HTTPException(503, "market weather not started")
    return weather


@app.get("/weather/status")
def weather_status(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Shadow mode, settings, today's current state, the profile table and the last log rows."""
    return _w().status()


@app.get("/weather/log")
def weather_log(limit: int = Query(100, ge=1, le=1000), _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return {"rows": _w().read_log(limit)}


class WeatherSettings(BaseModel):
    enabled: Optional[bool] = None
    stamp: Optional[bool] = None
    notify: Optional[bool] = None
    apply: Optional[bool] = None


@app.put("/weather/settings")
def weather_settings(req: WeatherSettings, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    return _w().configure(req.enabled, req.stamp, req.notify, req.apply)


@app.get("/weather/overlay")
def weather_overlay(_: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """E112: per service, each Market Weather dial's saved value, today's running value and when the overlay ends. Read only."""
    import weather_overlay as WO
    if weather_control is None:
        raise HTTPException(503, "market weather not started")
    return WO.overlay_view(weather_control)


class WeatherRevert(BaseModel):
    service: str


@app.post("/weather/overlay/revert")
def weather_overlay_revert(req: WeatherRevert, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """E112: put one service's overlaid dials back to their saved values now (a person's click). Does not switch "Send dials" off."""
    import weather_overlay as WO
    if weather_control is None:
        raise HTTPException(503, "market weather not started")
    try:
        return WO.revert(weather_control, req.service)
    except ValueError as e:
        raise HTTPException(400, str(e))


class WeatherRun(BaseModel):
    stage: str


@app.post("/weather/run")
def weather_run(req: WeatherRun, _: Dict[str, Any] = Depends(require_admin)) -> Dict[str, Any]:
    """Run one stage now (morning / refine / lock / tick) and send its message. Shadow: changes no switch."""
    try:
        return _w().run_stage(req.stage, force=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
