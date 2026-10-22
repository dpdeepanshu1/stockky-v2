"""Run engine of calibration-runner-service (group 442): one queue, one job at a time, every run leaves a txt + artifacts under DATA_DIR/runs/<id>/.

Status of a run: queued | running | success | attention | failed | timeout | cancelled | blocked
  attention = the script ran and its own verdict says look at it (FAIL / NO_GO / NOT_ENOUGH_DATA); failed = it did not run properly.
Nothing here is a shell; output is redacted of the tokens; output.txt is capped.
"""
from __future__ import annotations

import glob as _glob
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
FINAL = ("success", "attention", "failed", "timeout", "cancelled", "blocked")
OUT_FLAGS = {"--out", "--csv", "--json", "--save", "--manifest", "--out-dir", "--header", "--checkpoint", "--store"}   # group470: --checkpoint / --store are created by the job that owns them, so a missing one is not "input missing"
HEADLINE_RE = re.compile(r"(\bverdict\b|\bPASS\b|\bFAIL\b|\bNO_GO\b|\bGO\b|NOT_ENOUGH_DATA|NO_INPUT|\bWARN\b|\bSET [A-Z_]+=|KEEP THE CURRENT|\bCOMPLETE\b|\bINCOMPLETE\b|\bparity_ok\b)", re.I)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.=]{12,}")
# group470: a token that reaches a log by another route (a `--token VALUE` argument, a `set -x` trace line, TOKEN=VALUE) was printed in clear
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}")
_TOKEN_ARG = re.compile(r"(--token(?:=|\s+)|\b(?:PS_)?TOKEN=)[^\s'\"]{8,}")


def ist_now() -> datetime:
    return datetime.now(IST)


def market_open_now(now: Optional[datetime] = None) -> bool:
    """Mon-Fri 09:00-15:45 IST. Exchange holidays are not known here: on a holiday the guard is just stricter than needed."""
    n = (now or ist_now()).astimezone(IST)
    return n.weekday() < 5 and (9 * 60 <= n.hour * 60 + n.minute <= 15 * 60 + 45)


def _iso(t: Optional[float]) -> Optional[str]:
    return None if t is None else datetime.fromtimestamp(t, IST).isoformat(timespec="seconds")


def _limit_text(seconds: Any) -> str:
    s = float(seconds or 0)
    return f"{s / 3600:g} h" if s >= 3600 else f"{s:g} s"


class Redactor:
    def __init__(self, secrets_fn: Callable[[], List[str]]):
        self.secrets_fn = secrets_fn

    def __call__(self, text: str) -> str:
        for s in self.secrets_fn():
            if s and len(s) >= 8:
                text = text.replace(s, "***")
        text = _BEARER.sub(r"\1***", text)
        text = _TOKEN_ARG.sub(r"\1***", text)
        return _JWT.sub("***", text)


class Run:
    def __init__(self, rid: str, kind: str, target: str, title: str, params: Dict[str, Any], by: str, trigger: str):
        self.d: Dict[str, Any] = {"id": rid, "kind": kind, "target": target, "title": title, "params": params, "by": by, "trigger": trigger, "status": "queued",
                                  "queued_at": time.time(), "started_at": None, "finished_at": None, "steps": [], "jobs": [], "headline": [], "note": "", "exit_summary": ""}

    def public(self) -> Dict[str, Any]:
        d = dict(self.d)
        for k in ("queued_at", "started_at", "finished_at", "notified_at"):
            if k in d:
                d[k] = _iso(d[k])
        return d


class Runner:
    def __init__(self, data_dir: str, scripts_dir: str, ctx_base: Dict[str, Any], jobs: Dict[str, Dict[str, Any]], packs: Dict[str, Dict[str, Any]],
                 token_fn: Callable[[], str], extra_secrets: Optional[Callable[[], List[str]]] = None, max_output: int = 5 * 1024 * 1024, nice: int = 10,
                 keep_runs: int = 120, keep_days: int = 45, guard_market: bool = True, now_fn: Callable[[], datetime] = ist_now,
                 notify_fn: Optional[Callable[[Dict[str, Any], str], Optional[str]]] = None, keep_latest_only: bool = False, preflight_s: float = 60.0,
                 after_fn: Optional[Callable[[Dict[str, Any]], Any]] = None):
        self.data = data_dir
        self.runs_dir = os.path.join(data_dir, "runs")
        self.ctx_base = dict(ctx_base)
        self.jobs, self.packs = jobs, packs
        self.token_fn = token_fn
        self.redact = Redactor(lambda: [token_fn()] + (extra_secrets() if extra_secrets else []))
        self.max_output, self.nice = max_output, nice
        self.keep_runs, self.keep_days = keep_runs, keep_days
        self.guard_market = guard_market
        self.now_fn = now_fn
        self.after_fn = after_fn        # group 476: called (in its own thread) with the finished run's public dict, e.g. auto-calibration
        self.notify_fn = notify_fn      # group 447: (run dict, run dir) -> "sent" | note | None; announces automatic runs (see notify.py)
        self.keep_latest_only = keep_latest_only   # group 447: a new result replaces the older one of the same job / pack (saves disk)
        self.preflight_s = max(0.0, float(preflight_s))   # group 466: an automatic run waits at most this long for the stack to answer
        self.auto_path = os.path.join(data_dir, "state", "last_automatic.json")
        self.auto: Dict[str, Any] = {"last": None, "by_pack": {}}
        os.makedirs(self.runs_dir, exist_ok=True)
        for sub in ("calibration_out", "exports"):
            os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
        self.lock = threading.RLock()
        self.queue: Deque[Run] = deque()
        self.current: Optional[Run] = None
        self.cancel_flag = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.index: Dict[str, Run] = {}
        self._interrupted: List[Run] = []      # runs that were active when the container went down; announced once in start()
        self._load_history()
        self._load_auto()
        if self.keep_latest_only:
            self.supersede_all()

    # ---------------------------------------------------------------- history on disk
    def _run_dir(self, rid: str) -> str:
        return os.path.join(self.runs_dir, rid)

    def _load_history(self) -> None:
        for name in sorted(os.listdir(self.runs_dir)):
            p = os.path.join(self.runs_dir, name, "meta.json")
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            r = Run(d["id"], d["kind"], d["target"], d.get("title", ""), d.get("params", {}), d.get("by", ""), d.get("trigger", ""))
            r.d.update(d)
            if r.d["status"] in ("queued", "running"):       # the container restarted in the middle of it
                r.d["status"] = "failed"
                r.d["note"] = "the runner restarted while this run was active"
                r.d["finished_at"] = r.d.get("finished_at") or time.time()
                try:
                    self._save(r)               # written back, so the next restart does not find it 'active' again and announce it twice
                except OSError:
                    pass
                self._interrupted.append(r)
            self.index[r.d["id"]] = r

    def _save(self, r: Run) -> None:
        d = self._run_dir(r.d["id"])
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "meta.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(r.d, f)
        os.replace(tmp, os.path.join(d, "meta.json"))

    def prune(self) -> int:
        """Keep the newest keep_runs and nothing older than keep_days. Never touches queued / running runs."""
        done = sorted([r for r in self.index.values() if r.d["status"] in FINAL], key=lambda r: r.d["queued_at"], reverse=True)
        cutoff = time.time() - self.keep_days * 86400
        drop = [r for i, r in enumerate(done) if i >= self.keep_runs or r.d["queued_at"] < cutoff]
        for r in drop:
            shutil.rmtree(self._run_dir(r.d["id"]), ignore_errors=True)
            self.index.pop(r.d["id"], None)
        return len(drop)

    # ---------------------------------------------------------------- public API
    def ctx(self, run_dir: str) -> Dict[str, Any]:
        c = dict(self.ctx_base)
        c.update({"run": run_dir, "cal": os.path.join(self.data, "calibration_out"), "exp": os.path.join(self.data, "exports"), "scripts": os.path.join(self.scripts_dir_value())})
        return c

    def scripts_dir_value(self) -> str:
        return self.ctx_base["scripts"]

    def submit(self, kind: str, target: str, params: Optional[Dict[str, Any]] = None, by: str = "", trigger: str = "manual", confirm: bool = False,
               resume: bool = False) -> Run:
        if kind == "job":
            job = self.jobs.get(target)
            if not job:
                raise KeyError(f"unknown job {target}")
            from jobs import coerce_params
            p = coerce_params(job, params)
            title, heavy, window = job["title"], job["heavy"], job["window"]
        elif kind == "pack":
            pack = self.packs.get(target)
            if not pack:
                raise KeyError(f"unknown pack {target}")
            p, title = {}, pack["title"]
            heavy = any(self.jobs[j]["heavy"] for j, _ in pack["jobs"])
            window = "closed" if heavy else "any"
        else:
            raise KeyError("kind must be job or pack")
        rid = f"{self.now_fn().strftime('%Y%m%d-%H%M%S')}-{target}-{secrets.token_hex(2)}"
        r = Run(rid, kind, target, title, p, by, trigger)
        r.d["resume"] = bool(resume)      # group 466: continue from the symbols a stopped run already saved (scripts started with --resume)
        with self.lock:
            if any(q.d["target"] == target and q.d["kind"] == kind and q.d["params"] == p for q in self.queue):
                raise ValueError(f"{title} is already waiting in the queue")
            if len(self.queue) >= 20:
                raise ValueError("the queue is full (20)")
            blocked = self._guard(window, kind == "pack", confirm)
            if blocked:
                r.d.update(status="blocked", note=blocked, finished_at=time.time())
                self.index[rid] = r
                self._save(r)
            else:
                self.index[rid] = r
                self.queue.append(r)
                self._save(r)
        if blocked:
            self.supersede(kind, target, p)
            if trigger == "schedule":       # an automatic run that did not happen is exactly what you want to hear about
                threading.Thread(target=self._after_run, args=(r,), name="notify-blocked", daemon=True).start()
            return r
        self.wake.set()
        return r

    def _guard(self, window: str, is_pack: bool, confirm: bool) -> str:
        if confirm or not self.guard_market:
            return ""
        opn = market_open_now(self.now_fn())
        if window == "closed" and opn:
            return ("Blocked: this run downloads or replays a lot and the trading containers share this machine. Market hours are 09:00-15:45 IST on weekdays. "
                    "Run it after 15:45 or at the weekend, or confirm that you accept the load.")
        if window == "open" and not opn:
            return "Blocked: this check reads the live market state and is only meaningful 09:00-15:45 IST on a trading day. Confirm to run it anyway."
        return ""

    def cancel(self, rid: str) -> bool:
        with self.lock:
            r = self.index.get(rid)
            if not r:
                return False
            if r in self.queue:
                self.queue.remove(r)
                r.d.update(status="cancelled", finished_at=time.time(), note="cancelled while queued")
                self._save(r)
                return True
            if self.current is r:
                self.cancel_flag.set()
                p = self.proc
                if p and p.poll() is None:
                    self._signal_group(p, signal.SIGTERM)
                return True
        return False

    def list_runs(self, limit: int = 60, target: str = "") -> List[Dict[str, Any]]:
        with self.lock:
            rs = sorted(self.index.values(), key=lambda r: r.d["queued_at"], reverse=True)
            if target:
                rs = [r for r in rs if r.d["target"] == target]
            return [r.public() for r in rs[:limit]]

    def get(self, rid: str) -> Optional[Dict[str, Any]]:
        r = self.index.get(rid)
        if not r:
            return None
        d = r.public()
        d["files"] = self.files(rid)
        return d

    def files(self, rid: str) -> List[Dict[str, Any]]:
        base = self._run_dir(rid)
        out = []
        for root, _, names in os.walk(base):
            for n in sorted(names):
                p = os.path.join(root, n)
                rel = os.path.relpath(p, base)
                if rel in ("meta.json", "meta.json.tmp"):
                    continue
                out.append({"name": rel, "bytes": os.path.getsize(p)})
        return sorted(out, key=lambda f: (f["name"] != "output.txt", f["name"]))

    def file_path(self, rid: str, name: str) -> Optional[str]:
        base = os.path.realpath(self._run_dir(rid))
        p = os.path.realpath(os.path.join(base, name))
        return p if p.startswith(base + os.sep) and os.path.isfile(p) else None

    def tail(self, rid: str, offset: int = 0) -> Dict[str, Any]:
        r = self.index.get(rid)
        if not r:
            return {"found": False}
        p = os.path.join(self._run_dir(rid), "output.txt")
        text, size = "", 0
        if os.path.isfile(p):
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                f.seek(max(0, min(offset, size)))
                text = f.read(256 * 1024).decode("utf-8", "replace")
        return {"found": True, "status": r.d["status"], "offset": min(size, max(0, offset) + len(text.encode("utf-8"))), "size": size, "text": text}

    def zip_run(self, rid: str, dest: str) -> Optional[str]:
        if rid not in self.index:
            return None
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for f in self.files(rid):
                z.write(os.path.join(self._run_dir(rid), f["name"]), f["name"])
        return dest

    def zip_latest(self, dest: str, targets: Optional[List[str]] = None) -> int:
        """One zip with the LATEST finished result of every job (and pack): <target>/<its files>, plus _INDEX.txt (status, time, headline per target).
        Returns how many results went in (0 = nothing has run yet; the zip then only holds the index)."""
        ids = list(targets) if targets is not None else list(self.jobs) + list(self.packs)
        rows: List[str] = []
        n = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for t in ids:
                r = self.latest(t)
                if not r:
                    rows.append(f"{t}: never run")
                    continue
                n += 1
                head = (r.get("headline") or [""])[0]
                rows.append(f"{t}: {str(r.get('status')).upper()}  finished {r.get('finished_at') or '-'}  run {r['id']}" + (f"  -> {head}" if head else ""))
                for f in self.files(r["id"]):
                    z.write(os.path.join(self._run_dir(r["id"]), f["name"]), f"{t}/{f['name']}")
            z.writestr("_INDEX.txt", "Latest result of every test (status OK / LOOK / FAILED ...):\n\n" + "\n".join(rows) + "\n")
        return n

    def latest(self, target: str) -> Optional[Dict[str, Any]]:
        rs = [r for r in self.index.values() if r.d["target"] == target and r.d["status"] in FINAL and r.d["status"] != "blocked"]
        return max(rs, key=lambda r: r.d["queued_at"]).public() if rs else None

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return {"current": self.current.public() if self.current else None, "queued": [r.public() for r in self.queue], "market_open_now": market_open_now(self.now_fn()),
                    "now_ist": self.now_fn().astimezone(IST).isoformat(timespec="seconds"),
                    "last_automatic": self.auto.get("last"), "last_automatic_by_pack": dict(self.auto.get("by_pack") or {}),
                    "keep_latest_only": self.keep_latest_only}

    # ---------------------------------------------------------------- worker
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="runner", daemon=True)
        self.thread.start()
        if self._interrupted:               # group 447: a restart in the middle of a run used to leave no Telegram and no last-automatic record
            pending, self._interrupted = self._interrupted, []
            threading.Thread(target=lambda: [self._after_run(r) for r in pending], name="notify-interrupted", daemon=True).start()

    def shutdown(self) -> None:
        self.stop.set()
        self.cancel_flag.set()
        self.wake.set()
        p = self.proc
        if p is not None and p.poll() is None:      # group 466: do not leave pool workers running after the service stops
            self._signal_group(p, signal.SIGTERM)

    @staticmethod
    def _signal_group(p: "subprocess.Popen", sig: int) -> None:
        """group 466: signal the child's whole process group (the child is started as its own session leader, so pgid == pid): pool workers die with the job."""
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.send_signal(sig)
            except Exception:  # noqa: BLE001 - already gone
                pass

    def drain(self, timeout: float = 60.0) -> bool:
        """Tests: wait until nothing is queued or running."""
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if not self.queue and self.current is None:
                    return True
            time.sleep(0.05)
        return False

    def _loop(self) -> None:
        last_prune = 0.0
        while not self.stop.is_set():
            with self.lock:
                r = self.queue.popleft() if self.queue else None
                self.current = r
            if r is None:
                if time.time() - last_prune > 3600:
                    self.prune()
                    last_prune = time.time()
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.cancel_flag.clear()
            try:
                try:
                    self._execute(r)
                except Exception as e:  # noqa: BLE001
                    r.d.update(status="failed", note=f"internal error: {type(e).__name__}: {e}", finished_at=time.time())
                    self._save(r)
                self._after_run(r)          # group 447: before `current` is cleared, so drain() / the page see the run complete with its Telegram note
            finally:
                with self.lock:
                    self.current = None
                    self.proc = None

    # ---------------------------------------------------------------- after a run (group 447)
    def _after_run(self, r: Run) -> None:
        """Telegram for automatic runs, remember the last automatic run, then let the new result replace the older one. Never raises."""
        self._notify_done(r)
        try:
            self._record_auto(r)
            self.supersede(r.d["kind"], r.d["target"], r.d.get("params") or {})
        except Exception:  # noqa: BLE001 - bookkeeping must never touch the run
            pass
        if self.after_fn and r.d.get("status") != "blocked":
            pub = r.public()
            threading.Thread(target=self._call_after, args=(pub,), name="after-run", daemon=True).start()

    def _call_after(self, pub: Dict[str, Any]) -> None:
        try:
            self.after_fn(pub)
        except Exception:  # noqa: BLE001 - a hook problem never touches a run
            pass

    def _notify_done(self, r: Run) -> None:
        if not self.notify_fn:
            return
        try:
            res = self.notify_fn(dict(r.d), self._run_dir(r.d["id"]))
        except Exception as e:  # noqa: BLE001
            res = f"error: {type(e).__name__}"
        if res:
            r.d.update(notified=res, notified_at=time.time())
            try:
                self._save(r)
            except OSError:
                pass

    @staticmethod
    def _brief(r: Run) -> Dict[str, Any]:
        return {"id": r.d["id"], "title": r.d.get("title", ""), "target": r.d["target"], "status": r.d["status"],
                "finished_at": _iso(r.d.get("finished_at")), "telegram": r.d.get("notified") or ""}

    def _load_auto(self) -> None:
        """Kept in its own small file so replacing old run folders never loses 'when did the timetable last run'."""
        try:
            d = json.load(open(self.auto_path, encoding="utf-8"))
            if isinstance(d, dict):
                self.auto = {"last": d.get("last"), "by_pack": d.get("by_pack") or {}}
                return
        except Exception:  # noqa: BLE001
            pass
        rs = sorted([r for r in self.index.values() if r.d.get("trigger") == "schedule" and r.d["status"] in FINAL and r.d.get("finished_at")],
                    key=lambda x: x.d["finished_at"])      # first start after an upgrade: rebuild from the runs still on disk
        for r in rs:
            self.auto["last"] = self._brief(r)
            if r.d.get("kind") == "pack":
                self.auto["by_pack"][r.d["target"]] = self._brief(r)

    def _record_auto(self, r: Run) -> None:
        if r.d.get("trigger") != "schedule" or r.d["status"] not in FINAL or not r.d.get("finished_at"):
            return
        with self.lock:
            b = self._brief(r)
            self.auto["last"] = b
            if r.d.get("kind") == "pack":
                self.auto["by_pack"][r.d["target"]] = b
            snap = json.loads(json.dumps(self.auto))
        try:
            os.makedirs(os.path.dirname(self.auto_path), exist_ok=True)
            tmp = self.auto_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f)
            os.replace(tmp, self.auto_path)
        except OSError:
            pass

    # a run that holds a real result vs one that only says "did not run properly / was held back / was stopped"
    _RESULT = ("success", "attention", "failed", "timeout")

    @staticmethod
    def _same_settings(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
        return json.dumps(a or {}, sort_keys=True, default=str) == json.dumps(b or {}, sort_keys=True, default=str)

    def supersede(self, kind: str, target: str, params: Optional[Dict[str, Any]] = None) -> int:
        """keep_latest_only: for one job / pack WITH THE SAME SETTINGS keep the newest run that has a result, plus a newer blocked / cancelled note if there is one;
        delete the rest. (group 449: runs of one job with different options, e.g. daily_trades 10 days vs 30 days, no longer overwrite each other.)
        params=None means every settings variant of that target at once (the old behaviour).
        A blocked or cancelled run never replaces a real result. Queued / running runs are never touched."""
        if not self.keep_latest_only:
            return 0
        with self.lock:
            mine = sorted([r for r in self.index.values() if r.d["kind"] == kind and r.d["target"] == target and r.d["status"] in FINAL
                           and (params is None or self._same_settings(r.d.get("params"), params))],
                          key=lambda x: x.d["queued_at"], reverse=True)
            keep: List[Run] = []
            newest_result = next((r for r in mine if r.d["status"] in self._RESULT), None)
            if newest_result:
                keep.append(newest_result)
            newest_note = next((r for r in mine if r.d["status"] not in self._RESULT), None)
            if newest_note and (not newest_result or newest_note.d["queued_at"] > newest_result.d["queued_at"]):
                keep.append(newest_note)
            drop = [r for r in mine if r not in keep]
            for r in drop:
                shutil.rmtree(self._run_dir(r.d["id"]), ignore_errors=True)
                self.index.pop(r.d["id"], None)
            return len(drop)

    def supersede_all(self) -> int:
        groups = {(r.d["kind"], r.d["target"], json.dumps(r.d.get("params") or {}, sort_keys=True, default=str)) for r in list(self.index.values())}
        return sum(self.supersede(k, t, json.loads(pj)) for k, t, pj in groups)

    # ---------------------------------------------------------------- one run
    def _execute(self, r: Run) -> None:
        d = self._run_dir(r.d["id"])
        os.makedirs(d, exist_ok=True)
        r.d.update(status="running", started_at=time.time())
        self._save(r)
        out = _Out(os.path.join(d, "output.txt"), self.max_output, self.redact)
        out.line(f"# {r.d['title']}   [{r.d['kind']} {r.d['target']}]")
        out.line(f"# started {self.now_fn().astimezone(IST).isoformat(timespec='seconds')} IST   by {r.d['by'] or '-'}   trigger {r.d['trigger']}")
        if r.d["params"]:
            out.line("# parameters: " + json.dumps(r.d["params"]))
        out.line("# Nothing in this run changes a trading setting unless a step below says POST.")
        out.line("")
        ctx = self.ctx(d)
        ctx["resume"] = bool(r.d.get("resume"))     # group 466: jobs.py adds --resume to the checkpointed scripts
        if ctx["resume"]:
            out.line("# RESUME: scripts reuse the symbols the earlier run already saved (a changed history file or setting is recomputed)")
        if r.d["trigger"] == "schedule":
            self._preflight(out, ctx)
        if r.d["kind"] == "job":
            seq = [(r.d["target"], r.d["params"])]
        else:
            seq = [(jid, p) for jid, p in self.packs[r.d["target"]]["jobs"]]
        from jobs import coerce_params
        worst = "success"
        rank = {"success": 0, "attention": 1, "failed": 2, "timeout": 3}
        t0 = time.time()
        for jid, given in seq:
            job = self.jobs[jid]
            if self.cancel_flag.is_set():
                worst = "cancelled"
                break
            p = coerce_params(job, given)
            out.line(f"\n{'=' * 78}\n## JOB {job['title']}  ({jid})\n{'=' * 78}")
            job_status = "success"
            try:
                steps = job["builder"](ctx, p)
            except Exception as e:  # noqa: BLE001
                steps = []
                job_status = "failed"
                out.line(f"could not build the job: {e}")
            jstart = time.time()
            for i, st in enumerate(steps, 1):
                if self.cancel_flag.is_set():
                    job_status = "cancelled"
                    break
                left = job["timeout_s"] - (time.time() - jstart)
                if left <= 0:
                    job_status = "timeout"
                    out.line(f"-- job time limit reached ({_limit_text(job['timeout_s'])}; heavy jobs: RUNNER_HEAVY_TIMEOUT_S in seconds, default 21600)")
                    break
                res = self._step(st, i, len(steps), out, left, ctx)
                if res[0] == "timeout":
                    out.line(f"-- stopped at the {_limit_text(job['timeout_s'])} limit of this job (heavy jobs: RUNNER_HEAVY_TIMEOUT_S in seconds, default 21600)"
                             + ("; press Resume on this run to continue from the symbols already saved" if job.get("resumable") else ""))
                r.d["steps"].append({"job": jid, "n": i, "title": st["title"], "status": res[0], "seconds": round(res[1], 1), "detail": res[2]})
                if res[0] in ("failed", "timeout", "cancelled") and not st.get("soft"):
                    job_status = res[0]
                    break
                if res[0] == "attention" and rank.get(job_status, 0) < 1:
                    job_status = "attention"
                self._save(r)
            r.d["jobs"].append({"id": jid, "title": job["title"], "status": job_status, "seconds": round(time.time() - jstart, 1)})
            out.line(f"-- job result: {job_status.upper()}   ({round(time.time() - jstart)} s)")
            if job.get("after") and job_status in ("attention", "failed"):
                out.line(f"-- what to do: {job['after']}")
            if job_status == "cancelled":
                worst = "cancelled"
                break
            if rank.get(job_status, 0) > rank.get(worst, 0):
                worst = job_status
        out.line(f"\n# finished in {round(time.time() - t0)} s: {worst.upper()}")
        out.close()
        r.d["headline"] = _headline(os.path.join(d, "output.txt"))
        r.d["exit_summary"] = ", ".join(f"{j['id']}={j['status']}" for j in r.d["jobs"])
        r.d.update(status=worst, finished_at=time.time())
        self._save(r)

    def _preflight(self, out: "_Out", ctx: Dict[str, Any]) -> None:
        """group 466: an AUTOMATIC run (16:05 close pack) used to start while the other containers were still restarting / starved and lost its Telegram message.
        Wait (at most preflight_s) until the services answer /health, then go on regardless: a run must never be blocked by this."""
        if self.preflight_s <= 0:
            return
        hard = [(k, ctx.get(k)) for k in ("gw", "rt", "ps", "md", "decision", "notify") if ctx.get(k)]    # analysis / kronos / tsfm are optional: never waited for
        end = time.time() + self.preflight_s
        waited = False
        while True:
            down = [k for k, u in hard if not self._answers(str(u), min(3.0, max(0.5, end - time.time())))]
            if not down:
                out.line("-- services ready")
                return
            if time.time() >= end or self.cancel_flag.is_set() or self.stop.is_set():
                out.line(f"-- services still not answering after {int(self.preflight_s)} s ({', '.join(down)}), running anyway")
                return
            if not waited:
                out.line(f"-- waiting for services ({', '.join(down)}) up to {int(self.preflight_s)} s")
                waited = True
            time.sleep(min(2.0, max(0.1, end - time.time())))

    @staticmethod
    def _answers(base: str, timeout: float) -> bool:
        try:
            urllib.request.urlopen(urllib.request.Request(base.rstrip("/") + "/health", method="GET"), timeout=timeout).close()   # noqa: S310 - internal URL
            return True
        except urllib.error.HTTPError:
            return True                      # it answered (401 / 404 is still an answer)
        except Exception:  # noqa: BLE001 - refused / DNS / timeout
            return False

    def _env(self, extra: Dict[str, str]) -> Dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONPATH")}
        base["PYTHONUNBUFFERED"] = "1"
        base["PYTHONIOENCODING"] = "utf-8"
        tok = self.token_fn()
        for k, v in extra.items():
            base[k] = str(v).replace("{token}", tok)
        return base

    def _step(self, st: Dict[str, Any], i: int, n: int, out: "_Out", left_s: float, ctx: Dict[str, Any]) -> Tuple[str, float, str]:
        t0 = time.time()
        out.line(f"\n--- step {i}/{n}: {st['title']}")
        try:
            if st["kind"] == "http":
                res = self._http(st, out, left_s, ctx)
            else:
                res = self._cmd(st, out, left_s, ctx)
        except Exception as e:  # noqa: BLE001
            out.line(f"step error: {type(e).__name__}: {e}")
            res = ("failed", f"{type(e).__name__}: {e}")
        out.line(f"--- step result: {res[0]}" + (f" ({res[1]})" if res[1] else ""))
        return res[0], time.time() - t0, res[1]

    # ---- cmd
    def _expand(self, st: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[Optional[List[str]], str]:
        argv: List[str] = []
        toks = list(st["argv"])
        drop = set(st.get("drop_if_missing") or [])
        if drop:
            k = 0
            kept: List[str] = []
            while k < len(toks):
                if k + 1 < len(toks) and toks[k].startswith("--") and toks[k + 1] in drop and not os.path.exists(toks[k + 1]):
                    k += 2
                    continue
                kept.append(toks[k])
                k += 1
            toks = kept
        tok = self.token_fn()
        prev = ""
        guard = [os.path.join(ctx["cal"], ""), os.path.join(ctx["exp"], ""), os.path.join(ctx.get("bars", "/nonexistent"), "")]
        for t in toks:
            if t.startswith("globflag:"):
                # group470: "globflag:--ab:<pattern>" -> `--ab f1 --ab f2 ...` for scripts whose flag is action="append" (a bare list of paths is a usage error)
                _, flag, pat = t.split(":", 2)
                m = sorted(_glob.glob(pat))
                if not m:
                    return None, f"input missing: nothing matches {pat} (run the job that creates it first)"
                for f_ in m:
                    argv += [flag, f_]
            elif t.startswith("glob:"):
                m = sorted(_glob.glob(t[5:]))
                if not m:
                    return None, f"input missing: nothing matches {t[5:]} (run the job that creates it first)"
                argv += m
            else:
                if prev not in OUT_FLAGS and any(t.startswith(g) for g in guard):
                    if not os.path.exists(t) or (os.path.isdir(t) and not os.listdir(t)):
                        return None, f"input missing: {t} (run the job that creates it first)"
                    # group490: an EMPTY .json input is what a job that was killed or failed leaves behind (the walk-forward's backtest.json
                    # is its redirected output); it is not usable data, so it is the same "needs data first" case, not a script crash
                    if os.path.isfile(t) and t.endswith(".json") and os.path.getsize(t) == 0:
                        return None, f"input missing: {t} is empty (the job that writes it did not finish; run it again)"
                argv.append(t.replace("{token}", tok))
            prev = t
        return argv, ""

    def _cmd(self, st: Dict[str, Any], out: "_Out", left_s: float, ctx: Dict[str, Any]) -> Tuple[str, str]:
        argv, why = self._expand(st, ctx)
        if argv is None:
            out.line(why)
            # group 446: a missing input is a prerequisite that has not run yet (Saturday's weekly calibration), not a broken job: LOOK (amber), not FAILED (red)
            if why.startswith("input missing"):
                return "attention", "needs data first: " + why[len("input missing: "):]
            return "failed", why
        script = next((a for a in argv[1:2] if a.endswith((".py", ".sh"))), None)
        if script and not os.path.isfile(script):
            out.line(f"script not found: {script}")
            return "failed", "script not found"
        out.line("$ " + self.redact(" ".join(argv)))
        stdout_file = st.get("out_file") or ""
        # group491: the output goes to <out_file>.part and replaces <out_file> only when the command finished (see below). Writing
        # straight to out_file emptied the previous good file at the START of every run, so a run cut off by a restart (the 21 Oct
        # calibration_full) left an empty backtest.json and the next job that reads it had nothing.
        part_file = stdout_file + ".part" if stdout_file else ""
        fh = open(part_file, "wb") if stdout_file else None
        try:
            p = subprocess.Popen(argv, stdout=fh or subprocess.PIPE, stderr=subprocess.STDOUT if not fh else subprocess.PIPE, env=self._env(st.get("env") or {}),
                                 cwd=os.path.dirname(os.path.dirname(argv[1])) if script else None, preexec_fn=(lambda: os.nice(self.nice)) if self.nice else None,
                                 start_new_session=True)      # group 466: own process group, so a timeout / cancel kills the pool workers too
        except OSError as e:
            out.line(f"could not start: {e}")
            if fh:
                fh.close()
                self._drop(part_file)
            return "failed", str(e)
        self.proc = p
        deadline = time.time() + left_s
        timed_out = False
        err_lines: List[str] = []

        def pump(stream, sink):
            for raw in iter(stream.readline, b""):
                sink(raw.decode("utf-8", "replace").rstrip("\n"))

        t = threading.Thread(target=pump, args=(p.stdout if not fh else p.stderr, out.line if not fh else err_lines.append), daemon=True)
        t.start()
        while p.poll() is None:
            if self.cancel_flag.is_set():
                self._signal_group(p, signal.SIGTERM)
                break
            if time.time() > deadline:
                timed_out = True
                self._signal_group(p, signal.SIGTERM)
                break
            time.sleep(0.1)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._signal_group(p, signal.SIGKILL)
            p.wait()
        if timed_out or self.cancel_flag.is_set():
            self._signal_group(p, signal.SIGKILL)      # a pool worker that ignored SIGTERM
        t.join(timeout=5)
        self.proc = None
        if fh:
            fh.close()
            for ln in err_lines:
                out.line(ln)
            try:
                with open(part_file, "rb") as f:
                    body = f.read(200 * 1024).decode("utf-8", "replace")
                out.line(body.rstrip("\n"))
                if os.path.getsize(part_file) > 200 * 1024:
                    out.line(f"[...truncated here; the full output is in {os.path.basename(stdout_file)}]")
            except OSError:
                pass
            self._copy_artifact(part_file, ctx, os.path.basename(stdout_file))      # the run folder always shows what this run produced
        rc = p.returncode
        if fh:
            finished = not timed_out and not self.cancel_flag.is_set() and (rc in st.get("ok", [0]) or rc in st.get("attention", [1]))
            self._publish_out_file(part_file, stdout_file, finished)
        if self.cancel_flag.is_set():
            return "cancelled", "cancelled"
        if timed_out:
            return "timeout", "time limit"
        if rc in st.get("ok", [0]):
            return "success", ""
        if rc in st.get("attention", [1]):
            return "attention", f"exit {rc}: the script's own verdict says look at it"
        return "failed", f"exit {rc}"

    def _copy_artifact(self, path: str, ctx: Dict[str, Any], name: str = "") -> None:
        try:
            shutil.copy2(path, os.path.join(ctx["run"], name or os.path.basename(path)))
        except OSError:
            pass

    @staticmethod
    def _drop(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def _publish_out_file(self, part: str, final: str, finished: bool) -> None:
        """group491: a command that finished (exit code it counts as a result) replaces the saved file with its output; one that was
        cut off, timed out, was cancelled or crashed leaves the previous good file untouched. A finished command with EMPTY output
        also keeps the previous file when there is one."""
        try:
            if finished and (os.path.getsize(part) > 0 or not os.path.exists(final)):
                os.replace(part, final)
            else:
                self._drop(part)
        except OSError:
            self._drop(part)

    # ---- http
    def _http(self, st: Dict[str, Any], out: "_Out", left_s: float, ctx: Dict[str, Any]) -> Tuple[str, str]:
        url, method = st["url"], st.get("method", "GET")
        hdr = {"Accept": "application/json"}
        hdr["Authorization"] = "Bearer " + self.token_fn()     # services that need no login ignore it; the others accept this service token
        out.line(f"{method} {self.redact(url)}")
        expect = set(st.get("expect", [200]))
        poll = st.get("poll")
        deadline = time.time() + (min(left_s, float(poll["max_s"])) if poll else min(left_s, 180.0))
        while True:
            code, body = self._request(method if not poll else "GET", url, hdr, timeout=min(60.0, max(5.0, left_s)))
            if poll is None or self.cancel_flag.is_set():
                break
            try:
                val = str(json.loads(body).get(poll["key"], "")).lower()
            except Exception:  # noqa: BLE001
                val = ""
            out.line(f"  poll: {poll['key']}={val or '?'} (http {code})")
            if code in expect and val not in poll["until_not"]:
                break
            if time.time() > deadline:
                out.line("  (stopped waiting; the work may still be running in that service)")
                return "attention", "still running when the wait ended"
            time.sleep(float(poll.get("every_s", 20)))
        text = body if isinstance(body, str) else str(body)
        shown = text[:20000]
        try:
            shown = json.dumps(json.loads(text), indent=1)[:20000]
        except Exception:  # noqa: BLE001
            pass
        out.line(f"HTTP {code}")
        out.line(shown + ("\n[...truncated]" if len(text) > len(shown) and len(shown) >= 20000 else ""))
        if st.get("save") and code in expect:
            name = st["save"].replace("{date}", self.now_fn().strftime("%Y-%m-%d"))
            path = name if os.path.isabs(name) else os.path.join(ctx["run"], name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.redact(text))
            out.line(f"saved {path}")
            if not path.startswith(ctx["run"]):
                self._copy_artifact(path, ctx)
        if code in expect:
            return "success", ""
        if code == 0:
            return "failed", f"no answer: {text[:160]}"
        return "failed", f"HTTP {code}"

    def _request(self, method: str, url: str, headers: Dict[str, str], timeout: float) -> Tuple[int, str]:
        data = b"" if method in ("POST", "PUT", "PATCH") else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed internal URLs from the catalog
                return resp.status, resp.read(5 * 1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read(65536).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return 0, f"{type(e).__name__}: {e}"


class _Out:
    """output.txt writer: redacted, flushed per line (the page tails it), capped."""

    def __init__(self, path: str, cap: int, redact: Callable[[str], str]):
        self.f = open(path, "w", encoding="utf-8")
        self.cap, self.redact, self.n, self.capped = cap, redact, 0, False
        self.lock = threading.Lock()

    def line(self, s: str) -> None:
        with self.lock:
            if self.capped:
                return
            s = self.redact(s) + "\n"
            self.n += len(s.encode("utf-8"))
            if self.n > self.cap:
                self.f.write(f"\n[output cap of {self.cap // 1024} KB reached: the rest of the output is dropped. Result lines of the steps below may be missing.]\n")
                self.capped = True
            else:
                self.f.write(s)
            self.f.flush()

    def close(self) -> None:
        with self.lock:
            self.capped = False
            self.f.close()


def _headline(path: str, limit: int = 8) -> List[str]:
    out: List[str] = []
    try:
        for ln in open(path, encoding="utf-8", errors="replace"):
            s = ln.strip()
            if not s or s.startswith(("#", "$", "---", "===", "-- ")) or len(s) > 220:
                continue
            if HEADLINE_RE.search(s) and s not in out:
                out.append(s)
    except OSError:
        return out
    return out[:limit]
