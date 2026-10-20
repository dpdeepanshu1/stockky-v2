"""group468: approve-first mode for the first few REAL entries after a settings change (item 11).

OFF unless APPROVE_FIRST_N > 0 (default 0). REAL only; DEMO is never held. When on, the service fingerprints the gate settings
(env names starting STRUCTURE_, PS_STRUCTURE_, CANDIDATE_, ENTRY_, RISK_, TIMESFM_, BROKER_STOP, minus secrets and APPROVE_FIRST_*).
Whenever the fingerprint (or N) differs from the stored one, the next N automatic REAL entries must each be approved by you
(POST /approvals/REAL/{symbol}/approve) before the BUY is sent. After N approved entries it goes quiet until the next change.

How a held entry behaves: the candidate stays queued (consumed back to False, no decision row) and is judged again next cycle, so
an approval made within APPROVE_FIRST_TTL_MIN (default 15) minutes lets that very candidate through on the following cycle. A
rejection blocks the symbol for the same window. The approval is only USED UP when the BUY was actually sent, so a broker
rejection does not burn it. Manual BUYs and exits are never held. Nothing here changes what the other gates decide.

State is one JSON row in the resilience cache (key approve_first:REAL). Every function that reads or writes it never raises:
on any storage failure the gate FAILS OPEN (the entry proceeds as it did before this feature), because a broken helper must not
block or change trading; the failure is logged.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("real-trade-entry")

PREFIXES = ("STRUCTURE_", "PS_STRUCTURE_", "CANDIDATE_", "ENTRY_", "RISK_", "TIMESFM_", "BROKER_STOP")
_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS|TOTP|PIN|DSN)", re.I)
_HISTORY_MAX = 20


def _key(mode: str) -> str:
    return f"approve_first:{str(mode).upper()}"


def n_required() -> int:
    try:
        return max(0, min(20, int(((os.getenv("APPROVE_FIRST_N") or "").strip() or 0))))
    except (TypeError, ValueError):
        return 0


def ttl_s() -> float:
    try:
        return max(60.0, float(((os.getenv("APPROVE_FIRST_TTL_MIN") or "").strip() or 15)) * 60.0)
    except (TypeError, ValueError):
        return 900.0


def fingerprint(environ: Optional[Dict[str, str]] = None) -> str:
    """12-hex digest of the gate settings in force. Pure."""
    env = os.environ if environ is None else environ
    items = sorted((k, str(v).strip()) for k, v in env.items()
                   if k.startswith(PREFIXES) and not k.startswith("APPROVE_FIRST") and not _SECRET_RE.search(k))
    return hashlib.sha256("\n".join(f"{k}={v}" for k, v in items).encode()).hexdigest()[:12]


def new_state() -> Dict[str, Any]:
    return {"fingerprint": None, "n": 0, "remaining": 0, "approved": {}, "pending": {}, "rejected": {}, "history": []}


def sync(state: Optional[Dict[str, Any]], fp: str, n: int, now: float) -> Dict[str, Any]:
    """Pure. A changed fingerprint or N starts a fresh round of `n` approvals (stale pending / approved / rejected entries are dropped).
    Expired approvals, rejections and pending rows are pruned either way."""
    s = new_state() if not isinstance(state, dict) else {**new_state(), **state}
    if s.get("fingerprint") != fp or s.get("n") != n:
        s.update({"fingerprint": fp, "n": n, "remaining": n, "approved": {}, "pending": {}, "rejected": {}})
    ttl = ttl_s()
    s["approved"] = {k: v for k, v in (s.get("approved") or {}).items() if v > now}
    s["rejected"] = {k: v for k, v in (s.get("rejected") or {}).items() if v > now}
    s["pending"] = {k: v for k, v in (s.get("pending") or {}).items() if now - float(v.get("since", now)) <= 2 * ttl}
    return s


def decide(state: Dict[str, Any], symbol: str, details: Dict[str, Any], now: float) -> Dict[str, Any]:
    """Pure over `state` (mutated in place). {"allowed", "reason", "new_pending", "remaining"}."""
    sym = str(symbol).upper()
    if state.get("remaining", 0) <= 0:
        return {"allowed": True, "reason": "approvals used up for this settings fingerprint", "new_pending": False, "remaining": 0}
    if sym in state["approved"]:
        return {"allowed": True, "reason": "approved", "new_pending": False, "remaining": state["remaining"]}
    if sym in state["rejected"]:
        return {"allowed": False, "reason": "you rejected this entry; blocked until the window ends", "new_pending": False, "remaining": state["remaining"]}
    fresh = sym not in state["pending"]
    row = state["pending"].get(sym) or {"since": now}
    row.update({k: v for k, v in (details or {}).items() if v is not None})
    state["pending"][sym] = row
    return {"allowed": False, "reason": f"awaiting your approval ({state['remaining']} approval(s) left after this settings change)",
            "new_pending": fresh, "remaining": state["remaining"]}


def approve(state: Dict[str, Any], symbol: str, now: float) -> bool:
    sym = str(symbol).upper()
    if sym not in state["pending"] and sym not in state["approved"]:
        return False
    state["pending"].pop(sym, None)
    state["rejected"].pop(sym, None)
    state["approved"][sym] = now + ttl_s()
    return True


def reject(state: Dict[str, Any], symbol: str, now: float) -> bool:
    sym = str(symbol).upper()
    if sym not in state["pending"] and sym not in state["approved"]:
        return False
    state["pending"].pop(sym, None)
    state["approved"].pop(sym, None)
    state["rejected"][sym] = now + ttl_s()
    return True


def consume(state: Dict[str, Any], symbol: str, now: float, details: Optional[Dict[str, Any]] = None) -> bool:
    """The BUY for `symbol` was sent: use up one approval. False when it was not an approved entry."""
    sym = str(symbol).upper()
    if sym not in state["approved"] or state.get("remaining", 0) <= 0:
        return False
    state["approved"].pop(sym, None)
    state["remaining"] -= 1
    state["history"] = (state.get("history") or [])[-(_HISTORY_MAX - 1):] + [{"symbol": sym, "at": now, **(details or {})}]
    return True


# ── storage wrappers (resilience cache); every one fails open / safe ─────────────────────────────────────────────────────────
def _load(db, mode: str) -> Dict[str, Any]:
    from resilience.local_cache import load_snapshot
    return load_snapshot(db, _key(mode)) or new_state()


def _save(db, mode: str, state: Dict[str, Any]) -> None:
    from resilience.local_cache import save_snapshot
    save_snapshot(db, _key(mode), state)


def active(mode: str) -> bool:
    return str(mode).upper() == "REAL" and n_required() > 0


def gate(db, mode: str, symbol: str, details: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Called once per REAL entry about to be sent. Never raises; fails OPEN (allowed=True) on any error."""
    if not active(mode):
        return {"allowed": True, "reason": "approve-first is off", "new_pending": False, "remaining": 0}
    try:
        t = time.time() if now is None else now
        state = sync(_load(db, mode), fingerprint(), n_required(), t)
        res = decide(state, symbol, details or {}, t)
        _save(db, mode, state)
        return res
    except Exception as e:  # noqa: BLE001
        logger.error("approve-first gate failed open for %s: %s: %s", symbol, type(e).__name__, e)
        return {"allowed": True, "reason": "approve-first error (failed open)", "new_pending": False, "remaining": 0}


def mark_entered(db, mode: str, symbol: str, details: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> None:
    """Call after the REAL BUY was sent. Never raises."""
    if not active(mode):
        return
    try:
        t = time.time() if now is None else now
        state = sync(_load(db, mode), fingerprint(), n_required(), t)
        if consume(state, symbol, t, details):
            _save(db, mode, state)
    except Exception as e:  # noqa: BLE001
        logger.error("approve-first mark_entered failed for %s: %s: %s", symbol, type(e).__name__, e)


def _act(db, mode: str, symbol: str, fn) -> Dict[str, Any]:
    t = time.time()
    state = sync(_load(db, mode), fingerprint(), n_required(), t)
    ok = fn(state, symbol, t)
    _save(db, mode, state)
    return {"ok": ok, **view(state, t)}


def do_approve(db, mode: str, symbol: str) -> Dict[str, Any]:
    return _act(db, mode, symbol, approve)


def do_reject(db, mode: str, symbol: str) -> Dict[str, Any]:
    return _act(db, mode, symbol, reject)


def do_reset(db, mode: str) -> Dict[str, Any]:
    """Start a fresh round of N approvals now, as if the settings had just changed."""
    t = time.time()
    state = sync(None, fingerprint(), n_required(), t)
    _save(db, mode, state)
    return {"ok": True, **view(state, t)}


def view(state: Dict[str, Any], now: float) -> Dict[str, Any]:
    return {"enabled": n_required() > 0, "n": state.get("n"), "remaining": state.get("remaining"),
            "fingerprint": state.get("fingerprint"), "ttl_min": round(ttl_s() / 60.0, 1),
            "pending": [{"symbol": k, **v, "waiting_s": int(now - float(v.get("since", now)))} for k, v in sorted((state.get("pending") or {}).items())],
            "approved": [{"symbol": k, "expires_in_s": int(v - now)} for k, v in sorted((state.get("approved") or {}).items())],
            "rejected": [{"symbol": k, "expires_in_s": int(v - now)} for k, v in sorted((state.get("rejected") or {}).items())],
            "history": list(state.get("history") or [])}


def status(db, mode: str) -> Dict[str, Any]:
    """Read-only view for GET /approvals/{mode}. Never raises."""
    try:
        t = time.time()
        if not active(mode):
            return {"enabled": False, "n": n_required(), "remaining": 0, "pending": [], "approved": [], "rejected": [], "history": []}
        state = sync(_load(db, mode), fingerprint(), n_required(), t)
        return view(state, t)
    except Exception as e:  # noqa: BLE001
        logger.error("approve-first status failed: %s: %s", type(e).__name__, e)
        return {"enabled": n_required() > 0, "error": f"{type(e).__name__}: {e}", "pending": [], "approved": [], "rejected": [], "history": []}
