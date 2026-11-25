"""Round 659: one WAIT decision row per symbol + reason per window (D3 of the 20 Nov plan).

Why: real-trade wrote 6,417 WAIT rows in 3 days (about 2,100 a day), nearly all the same symbol and the same reason every 180 s cycle.
Rows that say nothing new cost DB writes, 176 MB + 80 MB of table, and slow every decisions query.

Rule (applied to pending ENTRY / WAIT rows just before the entry cycle flushes or commits, from write_behind.divert_pending_waits):
  - a WAIT row is written when it is the first for its (mode, symbol), when its signature differs from the last row written for that symbol
    (a change of reason, gate tag or verdict reason - numbers in the text are ignored so "0.83%" -> "0.85%" is not a change), or when
    WAIT_DEDUPE_MINUTES (default 15) have passed since the last row written;
  - otherwise the row is dropped from the session (the cycle's in-memory entry_details, counters and the shortlist are unaffected);
  - an ENTER row (or any non-WAIT ENTRY row) for the symbol resets its state, so the next WAIT after it is always written.
Exit rows, orders, fills and every non-WAIT row are never touched. WAIT_DEDUPE_MINUTES=0 turns it off. Nothing here raises.

Round 682 (opt-in, default unchanged): WAIT_DEDUPE_MODE=any_sig remembers EVERY signature written for the symbol inside the window, not only the last one, so a name that
flips between two gate tags (A, B, A, B every cycle: 23 Nov wrote 1,491 rows, the busiest names 32-39 each, more than the 22 a day one unchanged signature allows) writes
each signature once per window instead of a row at every flip. The first row of every (symbol, signature) is still written, so the name, the gate tag and the first-block
time of every refusal stay in the table; only the repeat count shrinks. Unset / blank / any other value = the rule above ("last"). Switch it on only after
scripts/wait_dedupe_sim.py shows it drops 30 % or more of a day's rows.

Round 684 (catch-all, default ON, WAIT_DEDUPE_CATCHALL=off turns it off): the rule above only ran where write_behind.divert_pending_waits is called, so a WAIT row
committed or flushed anywhere else reached the table unchecked (25 Nov: 258 rows evaluated, 1,710 stored). A Session before_flush hook now runs the SAME rule on every
pending ENTRY / WAIT TradeDecision that has not been checked yet and expunges the repeats, so every writer is covered whatever its path. It never touches a row that has
an id (an order hangs off it), an ENTER row or an exit row, and never raises. Each row it had to check itself is counted in status()['unchecked'] with the call chain
that flushed it, so the writer is still named while the rule is enforced; status()['caught_dropped'] counts the repeats it removed.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

_NUM = re.compile(r"-?\d+(?:[.,]\d+)*")
_SPACE = re.compile(r"\s+")
_MAX_KEYS = 20000

_lock = threading.Lock()
_last: Dict[Tuple[str, str], Tuple[str, float]] = {}
_seen: Dict[Tuple[str, str], Dict[str, float]] = {}       # round 682, any_sig mode: every signature written for the key -> time written
_MAX_SIGS = 40                                            # signatures remembered per symbol (oldest dropped first)
_stats = {"kept": 0, "dropped": 0, "unchecked": 0, "caught_dropped": 0}
_unchecked_samples: list = []                             # round 683: first few WAIT rows that reached the DB without passing is_repeat (who wrote them)
_MAX_SAMPLES = 25


def window_s() -> float:
    raw = (os.environ.get("WAIT_DEDUPE_MINUTES") or "").strip() or "15"
    try:
        v = float(raw)
    except ValueError:
        return 900.0
    return v * 60.0 if v > 0 else 0.0


def mode() -> str:
    """'any_sig' only when WAIT_DEDUPE_MODE says so (case and spaces ignored); anything else, including unset or blank, is the 'last' rule."""
    return "any_sig" if (os.environ.get("WAIT_DEDUPE_MODE") or "").strip().lower() == "any_sig" else "last"


def signature(obj) -> str:
    """Gate tag + reasoning + risk verdict reason with every number replaced by '#', whitespace collapsed, 300 chars."""
    parts = (getattr(obj, "gate_tag", None), getattr(obj, "reasoning", None), getattr(obj, "risk_verdict", None),
             getattr(obj, "risk_verdict_reason", None))
    txt = "|".join(str(p or "") for p in parts)
    return _SPACE.sub(" ", _NUM.sub("#", txt)).strip()[:300]


def is_repeat(obj, now: Optional[float] = None) -> bool:
    """True when this pending WAIT row repeats the last one written for its symbol inside the window. Records the row when it is kept."""
    try:
        try:
            obj._wd_seen = True                             # round 683: the insert probe below tells a checked row from one that bypassed this rule
        except Exception:  # noqa: BLE001
            pass
        win = window_s()
        key = (str(getattr(obj, "mode", "") or ""), str(getattr(obj, "symbol", "") or ""))
        t = time.monotonic() if now is None else now
        if getattr(obj, "action", None) != "WAIT":          # ENTER or anything else: the next WAIT is a change, write it
            with _lock:
                _last.pop(key, None)
                _seen.pop(key, None)
            return False
        if win <= 0 or not key[1]:
            return False
        sig = signature(obj)
        if mode() == "any_sig":
            with _lock:
                seen = _seen.get(key)
                if seen is None:
                    if len(_seen) >= _MAX_KEYS:
                        for k in sorted(_seen, key=lambda k: max(_seen[k].values(), default=0.0))[: _MAX_KEYS // 10]:
                            _seen.pop(k, None)
                    seen = _seen[key] = {}
                t0 = seen.get(sig)
                if t0 is not None and (t - t0) < win:
                    _stats["dropped"] += 1
                    return True
                seen[sig] = t
                if len(seen) > _MAX_SIGS:
                    for k in sorted(seen, key=seen.get)[: len(seen) - _MAX_SIGS]:
                        seen.pop(k, None)
                _stats["kept"] += 1
            return False
        with _lock:
            prev = _last.get(key)
            if prev is not None and prev[0] == sig and (t - prev[1]) < win:
                _stats["dropped"] += 1
                return True
            if len(_last) >= _MAX_KEYS and key not in _last:
                for k in sorted(_last, key=lambda k: _last[k][1])[: _MAX_KEYS // 10]:
                    _last.pop(k, None)
            _last[key] = (sig, t)
            _stats["kept"] += 1
        return False
    except Exception:  # noqa: BLE001
        return False


def catchall_on() -> bool:
    """Round 684: the before_flush catch-all is ON unless WAIT_DEDUPE_CATCHALL is 0 / off / false / no (blank or unset = on)."""
    return (os.environ.get("WAIT_DEDUPE_CATCHALL") or "").strip().lower() not in ("0", "off", "false", "no")


def _note_unchecked(target, label: str = "") -> None:
    """Count one WAIT row that arrived without passing is_repeat; keep the first few with symbol, gate tag and the call chain. Never raises."""
    try:
        import traceback
        frames = [f for f in traceback.extract_stack(limit=18)
                  if "site-packages" not in f.filename and os.path.basename(f.filename) != "wait_dedupe.py"]
        where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno}:{f.name}" for f in reversed(frames[-5:]))
        with _lock:
            _stats["unchecked"] += 1
            if len(_unchecked_samples) < _MAX_SAMPLES:
                _unchecked_samples.append({"symbol": getattr(target, "symbol", None), "gate_tag": getattr(target, "gate_tag", None),
                                           "where": where, "via": label})
    except Exception:  # noqa: BLE001
        pass


def _is_plain_new_wait(obj) -> bool:
    return (type(obj).__name__ == "TradeDecision" and getattr(obj, "decision_type", None) == "ENTRY" and getattr(obj, "action", None) == "WAIT"
            and getattr(obj, "id", None) is None and not getattr(obj, "_wd_seen", False))


def _before_flush(session, _ctx=None, _instances=None) -> None:
    """Round 684 (Session before_flush): apply the dedupe rule to every pending ENTRY / WAIT TradeDecision that did not pass it yet and expunge the repeats.
    Skips rows already checked (_wd_seen), rows with an id, and anything that is not a plain new WAIT row. Never raises."""
    try:
        if not catchall_on() or window_s() <= 0:
            return
        for obj in list(session.new):
            try:
                if not _is_plain_new_wait(obj):
                    continue
                _note_unchecked(obj, "before_flush")
                if is_repeat(obj):
                    session.expunge(obj)
                    with _lock:
                        _stats["caught_dropped"] += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass


def _probe_insert(_mapper, _conn, target) -> None:
    """round 683 (before_insert on TradeDecision): count an ENTRY / WAIT row being written by the ORM without having passed is_repeat.
    With the round 684 catch-all on, a row only gets here unchecked when the catch-all is off or the window is 0. Read only, never raises."""
    try:
        if getattr(target, "decision_type", None) != "ENTRY" or getattr(target, "action", None) != "WAIT" or getattr(target, "_wd_seen", False):
            return
        _note_unchecked(target, "before_insert")
    except Exception:  # noqa: BLE001
        pass


def install_insert_probe() -> bool:
    """Register the probe once (called at service start). False when SQLAlchemy / models are not importable. Never raises."""
    try:
        from sqlalchemy import event
        import models
        if not event.contains(models.TradeDecision, "before_insert", _probe_insert):
            event.listen(models.TradeDecision, "before_insert", _probe_insert)
        from sqlalchemy.orm import Session as _Session          # round 684: the catch-all covers every session in the process
        if not event.contains(_Session, "before_flush", _before_flush):
            event.listen(_Session, "before_flush", _before_flush)
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    with _lock:
        s = dict(_stats)
        s["unchecked_samples"] = list(_unchecked_samples[:_MAX_SAMPLES])
        s["tracked"] = len(_seen) if mode() == "any_sig" else len(_last)
    s["window_s"] = window_s()
    s["mode"] = mode()
    s["catchall"] = catchall_on()
    return s


def reset_for_tests() -> None:
    with _lock:
        _last.clear()
        _seen.clear()
        _stats.update(kept=0, dropped=0, unchecked=0, caught_dropped=0)
        _unchecked_samples.clear()
