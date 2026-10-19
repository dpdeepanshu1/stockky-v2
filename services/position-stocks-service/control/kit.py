"""control/kit.py - group 442: runtime switches for the Real Money Calibration page. ONE engine, copied byte-for-byte into real-trade-service and
position-stocks-service (a test fails if the copies differ); each service has its own `control/catalog.py` (the list of switches).

WHAT IT DOES
  * keeps an OVERRIDE per switch in `<CONTROL_DIR>/overrides.json` (a docker volume; default /data/control) and an append-only `audit.jsonl`;
  * at boot (`control/boot.py`, imported before `config`) writes the overrides into os.environ, so every switch survives a restart with no .env edit;
  * when a switch is `live=True` (checked statically by scripts/check_control_catalog.py: every consumer reads the value at call time) it also rewrites the
    config attribute in the running process, so the change is effective on the next call. Other switches are stored and take effect at the next restart
    (the page says "restart needed", with the exact command). Nothing here restarts anything.
  * classifies a change as LOOSENS / TIGHTENS / NEUTRAL against the current value and refuses a LOOSENING change unless the caller confirms it.
  * never accepts a key that is not in the catalog, never a free-form value (bool / enum / int / float with a range), and never writes a secret.

It is intentionally small and has no dependency except the standard library (fastapi is imported only inside `mount`).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("control")

DEFAULT_DIR = "/data/control"
AUDIT_MAX_LINES = 2000
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
KINDS = ("bool", "enum", "int", "float")
LOOSER_BOOL = ("on", "off", "neutral")           # which bool value is LOOSER ("on": ON loosens, e.g. a log-only switch; "off": OFF loosens, e.g. a gate)
LOOSER_NUM = ("higher", "lower", "neutral")      # numeric: does a HIGHER value loosen (a max-chase limit) or a LOWER one (a minimum room)


@dataclass(frozen=True)
class Flag:
    key: str                                   # the environment variable (what .env / compose sets)
    title: str
    group: str
    kind: str                                  # bool | enum | int | float
    help: str
    targets: Tuple[Tuple[str, str], ...] = ()  # (module, attribute) rewritten when applied live
    live: bool = False                         # True = effective at once (proved by scripts/check_control_catalog.py)
    choices: Tuple[str, ...] = ()              # enum values, ordered STRICTEST -> LOOSEST
    lo: Optional[float] = None
    hi: Optional[float] = None
    shipped: Optional[str] = None              # the code default when the variable is unset (a test compares it with the real config)
    recommended: Optional[str] = None          # what the current repo guidance wants (None = no opinion)
    looser: str = "neutral"                    # see LOOSER_BOOL / LOOSER_NUM; enums use the order of `choices`
    evidence: str = ""                         # which gate / report decides this switch (shown next to it)
    import_time_ok_files: Tuple[str, ...] = () # files whose import-time read is only a normalisation / derived default (see the check script)
    derived: Tuple[Tuple[str, str, str], ...] = ()  # (module, attr, env_key): attrs that default to this value at import when env_key is unset (DEMO twins)
    zero_off: bool = False                     # numeric MAX-style limit where 0 means "test disabled" (so 0 is the LOOSEST value, not the strictest)
    same_rank: Tuple[str, ...] = ()            # enum values that are equally loose (e.g. "log" and "off" never block a trade)


class ControlError(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- values
def validate_flag(f: Flag) -> List[str]:
    """Problems with a catalog entry itself (used by scripts/check_control_catalog.py and the tests)."""
    p: List[str] = []
    if f.kind not in KINDS:
        return [f"kind must be one of {KINDS}"]
    if not f.key or f.key != f.key.upper() or " " in f.key:
        p.append("key must be an upper-case env name")
    if any(w in f.key for w in ("KEY", "TOKEN", "SECRET", "PASSWORD", "HASH")):
        p.append("a secret-looking name can never be a switch")
    if f.kind == "enum" and len(f.choices) < 2:
        p.append("enum needs >= 2 choices")
    if f.kind != "enum" and f.choices:
        p.append("choices only for enum")
    if any(x not in f.choices for x in f.same_rank):
        p.append("same_rank values must be choices")
    if f.kind == "bool" and f.looser not in LOOSER_BOOL:
        p.append(f"looser must be one of {LOOSER_BOOL} for bool")
    if f.kind in ("int", "float") and f.looser not in LOOSER_NUM:
        p.append(f"looser must be one of {LOOSER_NUM} for a number")
    if f.kind in ("int", "float") and (f.lo is None or f.hi is None or f.lo > f.hi):
        p.append("a number needs lo <= hi")
    for label, v in (("shipped", f.shipped), ("recommended", f.recommended)):
        if v is not None:
            try:
                canonical(f, v)
            except ValueError as e:
                p.append(f"{label} {v!r}: {e}")
    if f.live and not f.targets:
        p.append("live=True needs targets")
    if f.shipped is None:
        p.append("shipped (the code default) is required")
    return p


def canonical(f: Flag, raw: Any) -> str:
    """The one string form of a value (what is written to the environment and compared). Raises ValueError with the reason."""
    if raw is None:
        raise ValueError("empty value")
    if f.kind == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        s = str(raw).strip().lower()
        if s in _TRUE:
            return "true"
        if s in _FALSE:
            return "false"
        raise ValueError("expected true or false")
    s = str(raw).strip()
    if f.kind == "enum":
        s = s.lower()
        if s not in f.choices:
            raise ValueError(f"expected one of {', '.join(f.choices)}")
        return s
    try:
        n = float(s)
    except ValueError:
        raise ValueError("expected a number")
    if n != n or n in (float("inf"), float("-inf")):
        raise ValueError("expected a finite number")
    if f.lo is not None and n < f.lo:
        raise ValueError(f"must be >= {f.lo:g}")
    if f.hi is not None and n > f.hi:
        raise ValueError(f"must be <= {f.hi:g}")
    if f.kind == "int":
        if n != int(n):
            raise ValueError("expected a whole number")
        return str(int(n))
    return repr(round(n, 6)).rstrip("0").rstrip(".") if "." in repr(round(n, 6)) else repr(round(n, 6))


def parsed(f: Flag, canon: str) -> Any:
    if f.kind == "bool":
        return canon == "true"
    if f.kind == "int":
        return int(canon)
    if f.kind == "float":
        return float(canon)
    return canon


def classify(f: Flag, old: Optional[str], new: str) -> str:
    """LOOSENS | TIGHTENS | NEUTRAL | SAME, from the catalog's own direction. An unknown old value counts as NEUTRAL (never a silent pass for a loosening)."""
    if old is not None and old == new:
        return "SAME"
    if old is None:
        return "NEUTRAL"
    if f.kind == "bool":
        if f.looser == "neutral":
            return "NEUTRAL"
        loosen_to = "true" if f.looser == "on" else "false"
        return "LOOSENS" if new == loosen_to else "TIGHTENS"
    if f.kind == "enum":
        try:
            a, b = f.choices.index(old), f.choices.index(new)
        except ValueError:
            return "NEUTRAL"
        if old in f.same_rank and new in f.same_rank:
            return "NEUTRAL"
        if new in f.same_rank and old not in f.same_rank:
            b = max(b, max(f.choices.index(x) for x in f.same_rank if x in f.choices))
        if old in f.same_rank and new not in f.same_rank:
            a = max(a, max(f.choices.index(x) for x in f.same_rank if x in f.choices))
        return "LOOSENS" if b > a else ("TIGHTENS" if b < a else "NEUTRAL")
    if f.looser == "neutral":
        return "NEUTRAL"
    fo, fn = float(old), float(new)
    if f.zero_off and f.looser == "higher":
        fo = float("inf") if fo == 0 else fo
        fn = float("inf") if fn == 0 else fn
    up = fn > fo
    return ("LOOSENS" if up else "TIGHTENS") if f.looser == "higher" else ("TIGHTENS" if up else "LOOSENS")


def _module(name: str):
    return importlib.import_module(name)


def running_value(f: Flag) -> Optional[str]:
    """What the process is using right now (read from the first target), canonical; None when there is no target or it cannot be read."""
    if not f.targets:
        return None
    mod, attr = f.targets[0]
    try:
        return canonical(f, getattr(_module(mod), attr))
    except Exception:  # noqa: BLE001 - a clamped / renamed value must not break the page
        try:
            return str(getattr(_module(mod), attr))
        except Exception:  # noqa: BLE001
            return None


def _env_value(f: Flag) -> Optional[str]:
    raw = (os.environ.get(f.key) or "").strip()
    if not raw:
        return None
    try:
        return canonical(f, raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- store
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Overrides + audit on disk. A missing / unwritable directory keeps everything in memory and says so (`persistent=False`); nothing raises."""

    def __init__(self, directory: Optional[str] = None):
        self.dir = directory or (os.environ.get("CONTROL_DIR") or "").strip() or DEFAULT_DIR
        self._lock = threading.RLock()
        self._mem: Dict[str, Any] = {"v": 1, "overrides": {}}
        self._audit_mem: List[Dict[str, Any]] = []
        self.persistent = self._probe()
        self.last_error: Optional[str] = None
        if self.persistent:
            self._load()

    def _probe(self) -> bool:
        try:
            os.makedirs(self.dir, exist_ok=True)
            fd, p = tempfile.mkstemp(prefix=".w", dir=self.dir)
            os.close(fd)
            os.unlink(p)
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            return False

    @property
    def _path(self) -> str:
        return os.path.join(self.dir, "overrides.json")

    @property
    def _audit_path(self) -> str:
        return os.path.join(self.dir, "audit.jsonl")

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and isinstance(d.get("overrides"), dict):
                self._mem = {"v": 1, "overrides": {str(k): dict(v) for k, v in d["overrides"].items() if isinstance(v, dict)}}
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 - a damaged file is ignored (and kept aside), never fatal at boot
            self.last_error = f"overrides.json unreadable: {type(e).__name__}"[:200]
            try:
                os.replace(self._path, self._path + ".damaged")
            except OSError:
                pass

    def overrides(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._mem["overrides"].items()}

    def _save(self) -> None:
        if not self.persistent:
            return
        try:
            fd, tmp = tempfile.mkstemp(prefix=".ov", dir=self.dir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._mem, fh, indent=1, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"save failed: {type(e).__name__}: {e}"[:200]
            self.persistent = False

    def put(self, key: str, rec: Dict[str, Any]) -> None:
        with self._lock:
            self._mem["overrides"][key] = rec
            self._save()

    def delete(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._mem["overrides"].pop(key, None)
            self._save()
            return rec

    def audit(self, row: Dict[str, Any]) -> None:
        row = {"at": _now(), **row}
        with self._lock:
            self._audit_mem.append(row)
            self._audit_mem = self._audit_mem[-AUDIT_MAX_LINES:]
            if not self.persistent:
                return
            try:
                with open(self._audit_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
                if os.path.getsize(self._audit_path) > 1_500_000:        # keep the newest lines only
                    with open(self._audit_path, "r", encoding="utf-8") as fh:
                        lines = fh.readlines()[-AUDIT_MAX_LINES:]
                    with open(self._audit_path, "w", encoding="utf-8") as fh:
                        fh.writelines(lines)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"audit failed: {type(e).__name__}"[:200]

    def read_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            if self.persistent and os.path.exists(self._audit_path):
                try:
                    with open(self._audit_path, "r", encoding="utf-8") as fh:
                        rows = [json.loads(x) for x in fh.readlines()[-limit:] if x.strip()]
                    return rows[::-1]
                except Exception:  # noqa: BLE001
                    pass
            return self._audit_mem[-limit:][::-1]


# ---------------------------------------------------------------- controller
class Controller:
    def __init__(self, service: str, catalog: Sequence[Flag], store: Optional[Store] = None, restart_hint: str = ""):
        self.service = service
        self.catalog = list(catalog)
        self.by_key = {f.key: f for f in self.catalog}
        self.store = store or Store()
        self.restart_hint = restart_hint or f"docker compose restart {service}"
        self._lock = threading.RLock()

    # -- reading
    def _desired(self, f: Flag, rec: Optional[Dict[str, Any]]) -> Optional[str]:
        if rec and rec.get("value") is not None:
            return rec["value"]
        return _env_value(f) or f.shipped

    def describe(self) -> Dict[str, Any]:
        ov = self.store.overrides()
        rows: List[Dict[str, Any]] = []
        n_pending = n_off_rec = 0
        for f in self.catalog:
            rec = ov.get(f.key)
            run = running_value(f)
            desired = self._desired(f, rec)
            effective = run if run is not None else desired
            pending = bool(run is not None and desired is not None and run != desired)
            matches = None if f.recommended is None else (canonical(f, f.recommended) == effective)
            n_pending += 1 if pending else 0
            n_off_rec += 1 if matches is False else 0
            rows.append({
                "key": f.key, "title": f.title, "group": f.group, "kind": f.kind, "help": f.help, "choices": list(f.choices),
                "lo": f.lo, "hi": f.hi, "live": f.live, "shipped": f.shipped,
                "recommended": None if f.recommended is None else canonical(f, f.recommended),
                "looser": f.looser, "evidence": f.evidence,
                "running": run, "desired": desired, "effective": effective,
                "source": "override" if rec else ("env" if _env_value(f) else "default"),
                "override": None if not rec else {k: rec.get(k) for k in ("value", "by", "at", "note")},
                "pending_restart": pending, "matches_recommended": matches,
                "restart_needed_on_change": not f.live,
            })
        return {"service": self.service, "persistent": self.store.persistent, "state_dir": self.store.dir, "store_error": self.store.last_error,
                "restart_hint": self.restart_hint,
                "summary": {"flags": len(rows), "overrides": len(ov), "pending_restart": n_pending, "off_recommended": n_off_rec},
                "flags": rows}

    # -- writing
    def _flag(self, key: str) -> Flag:
        f = self.by_key.get((key or "").strip())
        if f is None:
            raise ControlError(404, f"unknown switch {key!r}: only the catalog's switches can be changed")
        return f

    def _apply_live(self, f: Flag, canon: Optional[str]) -> Tuple[bool, Optional[str]]:
        """Write the value into os.environ and the config attributes. `canon` None = restore the unset state. Returns (applied, error)."""
        shown = canon if canon is not None else (_env_value_orig(f) or f.shipped)
        try:
            val = parsed(f, shown)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        try:
            for mod, attr in f.targets:
                setattr(_module(mod), attr, val)
            for mod, attr, env_key in f.derived:
                if not (os.environ.get(env_key) or "").strip() and env_key not in self.store.overrides():
                    setattr(_module(mod), attr, val)
            back = running_value(f)
            return (back == canonical(f, shown)), (None if back == canonical(f, shown) else f"read back {back!r}")
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"[:200]

    def set(self, key: str, value: Any, by: str, confirm: bool = False, note: str = "") -> Dict[str, Any]:
        f = self._flag(key)
        try:
            new = canonical(f, value)
        except ValueError as e:
            raise ControlError(400, f"{f.key}: {e}")
        with self._lock:
            rec_old = self.store.overrides().get(f.key)
            cur = running_value(f) or self._desired(f, rec_old)
            change = classify(f, cur, new)
            if change == "SAME" and rec_old and rec_old.get("value") == new:
                return {"ok": True, "key": f.key, "change": "SAME", "value": new, "applied_live": f.live, "restart_required": False, "message": "no change"}
            if change == "LOOSENS" and not confirm:
                raise ControlError(409, {"needs_confirm": True, "change": "LOOSENS", "key": f.key, "from": cur, "to": new,
                                         "message": f"{f.title}: {cur} -> {new} LOOSENS live trading rules. Send confirm=true to apply."})
            orig = rec_old.get("orig_env") if rec_old and "orig_env" in rec_old else (os.environ.get(f.key))
            rec = {"value": new, "orig_env": orig, "by": by, "at": _now(), "note": (note or "")[:200]}
            self.store.put(f.key, rec)
            os.environ[f.key] = new
            applied, err = (self._apply_live(f, new) if f.live else (False, None))
            restart = not (f.live and applied)
            self.store.audit({"action": "set", "service": self.service, "key": f.key, "from": cur, "to": new, "change": change, "by": by,
                              "applied_live": applied, "restart_required": restart, "note": rec["note"], "error": err})
        return {"ok": True, "key": f.key, "change": change, "from": cur, "value": new, "applied_live": applied, "restart_required": restart,
                "restart_hint": self.restart_hint if restart else None, "persistent": self.store.persistent, "error": err,
                "message": ("applied now" if applied else f"saved; takes effect after: {self.restart_hint}") + ("" if self.store.persistent else " (WARNING: state dir not writable, this will be lost on restart)")}

    def clear(self, key: str, by: str) -> Dict[str, Any]:
        f = self._flag(key)
        with self._lock:
            rec = self.store.delete(f.key)
            if rec is None:
                return {"ok": True, "key": f.key, "cleared": False, "message": "no override"}
            orig = rec.get("orig_env")
            if orig is None:
                os.environ.pop(f.key, None)
            else:
                os.environ[f.key] = orig
            applied, err = (self._apply_live(f, None) if f.live else (False, None))
            restart = not (f.live and applied)
            self.store.audit({"action": "clear", "service": self.service, "key": f.key, "was": rec.get("value"), "by": by, "applied_live": applied,
                              "restart_required": restart, "error": err})
        return {"ok": True, "key": f.key, "cleared": True, "applied_live": applied, "restart_required": restart,
                "restart_hint": self.restart_hint if restart else None, "error": err,
                "message": "back to the .env / default value" + ("" if applied else f"; takes effect after: {self.restart_hint}")}

    def preset_recommended(self, by: str, dry_run: bool = True, confirm: bool = False) -> Dict[str, Any]:
        """Move every switch that has a recommended value to it. dry_run lists the changes only."""
        plan: List[Dict[str, Any]] = []
        for f in self.catalog:
            if f.recommended is None:
                continue
            rec = canonical(f, f.recommended)
            cur = running_value(f) or self._desired(f, self.store.overrides().get(f.key))
            if cur == rec:
                continue
            plan.append({"key": f.key, "title": f.title, "from": cur, "to": rec, "change": classify(f, cur, rec), "live": f.live})
        loosening = [p for p in plan if p["change"] == "LOOSENS"]
        if dry_run:
            return {"ok": True, "dry_run": True, "changes": plan, "loosening": len(loosening)}
        if loosening and not confirm:
            raise ControlError(409, {"needs_confirm": True, "change": "LOOSENS", "changes": plan, "message": f"{len(loosening)} of {len(plan)} changes LOOSEN live rules; send confirm=true."})
        done = [self.set(p["key"], p["to"], by, confirm=True, note="preset: recommended") for p in plan]
        return {"ok": True, "dry_run": False, "changes": plan, "applied": [d["key"] for d in done if d.get("applied_live")],
                "restart_required": [d["key"] for d in done if d.get("restart_required")]}


def _env_value_orig(f: Flag) -> Optional[str]:
    """The environment value as it is NOW (after a clear() restored it)."""
    return _env_value(f)


# ---------------------------------------------------------------- boot
def boot_apply(catalog: Sequence[Flag], directory: Optional[str] = None) -> Dict[str, str]:
    """Put the stored overrides into os.environ (only catalog keys, only values that still validate). Called before `config` is imported."""
    applied: Dict[str, str] = {}
    store = Store(directory)
    by_key = {f.key: f for f in catalog}
    for key, rec in store.overrides().items():
        f = by_key.get(key)
        if f is None:
            continue
        try:
            canon = canonical(f, rec.get("value"))
        except ValueError:
            logger.warning("control: stored value for %s no longer valid, ignored", key)
            continue
        os.environ[key] = canon
        applied[key] = canon
    return applied


# ---------------------------------------------------------------- http
def mount(app: Any, controller: Controller, admin_dep: Callable[..., Any], prefix: str = "/control") -> None:
    """Add GET/POST/DELETE routes under `prefix`. Every route needs the service's admin session (`admin_dep`)."""
    from fastapi import APIRouter, Depends, HTTPException
    from pydantic import BaseModel

    class SetBody(BaseModel):
        value: Any
        confirm: bool = False
        note: str = ""

    class PresetBody(BaseModel):
        dry_run: bool = True
        confirm: bool = False

    router = APIRouter(prefix=prefix, tags=["control"])

    def _wrap(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return fn()
        except ControlError as e:
            raise HTTPException(status_code=e.status, detail=e.detail)

    @router.get("/flags")
    def flags(admin: str = Depends(admin_dep)):
        return controller.describe()

    @router.post("/flags/{key}")
    def set_flag(key: str, body: SetBody, admin: str = Depends(admin_dep)):
        return _wrap(lambda: controller.set(key, body.value, admin or "admin", confirm=body.confirm, note=body.note))

    @router.delete("/flags/{key}")
    def clear_flag(key: str, admin: str = Depends(admin_dep)):
        return _wrap(lambda: controller.clear(key, admin or "admin"))

    @router.post("/preset/recommended")
    def preset(body: PresetBody, admin: str = Depends(admin_dep)):
        return _wrap(lambda: controller.preset_recommended(admin or "admin", dry_run=body.dry_run, confirm=body.confirm))

    @router.get("/audit")
    def audit(limit: int = 100, admin: str = Depends(admin_dep)):
        return {"service": controller.service, "rows": controller.store.read_audit(limit)}

    app.include_router(router)
