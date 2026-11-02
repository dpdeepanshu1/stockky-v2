"""orders/gate_policy_job.py - E285 batch 5: the nightly job behind the adaptive gate policy (position-stocks longs).

Every trading day after GATE_POLICY_JOB_AT_IST (16:30, bars are complete) the job
  1. reads the last GATE_POLICY_DAYS (15) days of scalp_candidate_log rows of the four adaptable gates (orders/gate_policy.py): SKIPPED rows (the gate refused) and SOFT rows
     (the gate fired but the entry went on at half size),
  2. de-duplicates them like scripts/gate_scorecard.py (one per symbol, gate, day, 30-minute bucket) and measures what the stock did next on the recorded 1-minute bars:
     net60 = the move 60 minutes later, as a long, minus GATE_POLICY_COST_PCT (0.15) round-trip cost,
  3. moves each gate one step at most, and writes scalp_gate_policy (state, evidence). The entry path reads it through gate_policy.state().

Rules (evaluate_gate, pure):
  HARD -> SOFT only if ALL hold: n >= GATE_POLICY_MIN_N (30), over >= GATE_POLICY_MIN_DAYS (5) days and >= GATE_POLICY_MIN_SYMBOLS (20) symbols;
       the bootstrap lower bound of mean net60 (GATE_POLICY_BOOT_N resamples, one-sided GATE_POLICY_CONFIDENCE 0.95) is > 0; and no single day drives the gain
       (the mean stays > 0 with any ONE day removed). A gate that fails is not relaxed to make it pass.
  SOFT -> HARD if n >= GATE_POLICY_MIN_N and mean net60 < -GATE_POLICY_HYST_PCT (0.10): the hysteresis band, so a gate sitting near zero does not flip back and forth.
  One step per gate per GATE_POLICY_STEP_DAYS (7) days. OFF is never changed by the job. Long and short rows are evaluated apart (the rows here are long-side gates).
The first run (no row yet) starts every gate from the state its own config gives (gate_policy.static_state) and applies the same rules, so "seeding" is nothing special:
a gate is SOFT after the first run only if it passed HARD -> SOFT as written. Run it by hand any time: POST /gate-policy/run.
Nothing here raises into the trading loop.
"""
from __future__ import annotations

import calendar
import logging
import random
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config
from orders import candidate_outcomes, gate_policy

logger = logging.getLogger("position-stocks.gate_policy_job")

IST = timedelta(hours=5, minutes=30)
Sample = Tuple[str, str, float]            # (IST day, symbol, net60 %)
_last_run_day: Optional[str] = None
_retry_after: float = 0.0                  # monotonic time before which a failed run is not tried again


def _f(name: str, default: float) -> float:
    try:
        return float(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------------- pure statistics

def gate_key(reason: Optional[str], decision: Optional[str]) -> str:
    """Same grouping as scripts/gate_scorecard.py::gate_key."""
    if decision == "ENTERED":
        return "ENTERED (baseline)"
    r = (reason or "").strip()
    if not r:
        return "SKIPPED:(no reason)"
    parts = r.split(":")
    if parts[0] == "QUALITY_GATE" and len(parts) > 1:
        return "QUALITY_GATE:" + (parts[1].strip().split(" ")[0] or "other")
    if parts[0] in ("RANGE_GATE", "OPENING_GATE", "PRICE_GUARD", "QUALITY_GATE", "BOARD_GATE"):
        return ":".join(parts[:2]) if len(parts) > 1 else parts[0]
    return parts[0][:40] if parts[0].isupper() else "OTHER:" + r.split(" ")[0][:30]


def bootstrap_lower_bound(values: List[float], n_boot: int = 2000, confidence: float = 0.95, seed: int = 285) -> Optional[float]:
    """One-sided lower bound of the mean: the (1 - confidence) quantile of `n_boot` resampled means. Deterministic (fixed seed). None for fewer than 2 values."""
    try:
        n = len(values)
        if n < 2:
            return None
        rng = random.Random(seed)
        means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(max(50, int(n_boot))))
        k = max(0, min(len(means) - 1, int((1.0 - float(confidence)) * len(means))))
        return round(means[k], 4)
    except Exception:  # noqa: BLE001
        return None


def summarize(samples: Iterable[Sample], n_boot: int = 2000, confidence: float = 0.95) -> Dict[str, Any]:
    """{n, days, symbols, mean, lb, loo_min}: sample count, distinct days / symbols, mean net60, its bootstrap lower bound, and the lowest mean with any ONE day removed
    (None when only one day is present)."""
    s = [x for x in samples if x is not None]
    vals = [float(x[2]) for x in s]
    n = len(vals)
    out: Dict[str, Any] = {"n": n, "days": len({x[0] for x in s}), "symbols": len({x[1] for x in s}), "mean": None, "lb": None, "loo_min": None}
    if not n:
        return out
    out["mean"] = round(sum(vals) / n, 4)
    out["lb"] = bootstrap_lower_bound(vals, n_boot, confidence)
    by_day: Dict[str, List[float]] = {}
    for d, _sym, v in s:
        by_day.setdefault(d, []).append(float(v))
    if len(by_day) >= 2:
        tot, cnt = sum(vals), n
        out["loo_min"] = round(min((tot - sum(v)) / (cnt - len(v)) for v in by_day.values() if cnt - len(v) > 0), 4)
    return out


def loosen_check(st: Dict[str, Any]) -> str:
    """The HARD -> SOFT evidence rule, in one place (evaluate_gate and the scorecard verdict both use it). Returns "" when every condition holds, else the first one that fails."""
    n, days, syms = int(st.get("n") or 0), int(st.get("days") or 0), int(st.get("symbols") or 0)
    lb, loo = st.get("lb"), st.get("loo_min")
    min_n, min_days, min_syms = _i("GATE_POLICY_MIN_N", 30), _i("GATE_POLICY_MIN_DAYS", 5), _i("GATE_POLICY_MIN_SYMBOLS", 20)
    if n < min_n:
        return f"needs n >= {min_n}"
    if days < min_days:
        return f"needs >= {min_days} days"
    if syms < min_syms:
        return f"needs >= {min_syms} symbols"
    if lb is None or lb <= 0:
        return "bootstrap lower bound of net60 is not > 0"
    if loo is not None and loo <= 0:
        return "one day drives the gain (mean <= 0 without it)"
    if loo is None:
        return "only one day of data"
    return ""


def verdict(st: Dict[str, Any], is_baseline: bool = False) -> str:
    """E285 batch 6: the scorecard's verdict with the SAME rules the policy job applies, so the words mean one thing everywhere.
    LOOSEN  = passes the HARD -> SOFT rule (the job would move the gate to SOFT, one step a week);  KEEP = mean net60 under -GATE_POLICY_HYST_PCT with n enough (a SOFT gate
    would go back to HARD);  NO CALL = neither, with the reason;  too few samples = n under GATE_POLICY_MIN_N."""
    if is_baseline:
        return "baseline"
    if int(st.get("n") or 0) < _i("GATE_POLICY_MIN_N", 30):
        return "too few samples"
    why = loosen_check(st)
    if not why:
        return "LOOSEN: passes the policy rule (HARD -> SOFT)"
    mean = st.get("mean")
    if mean is not None and mean < -_f("GATE_POLICY_HYST_PCT", 0.10):
        return "KEEP: saves money"
    return f"NO CALL: {why}"


def evaluate_gate(gate: str, cur_state: str, st: Dict[str, Any], today: str, last_change_day: Optional[str]) -> Dict[str, Any]:
    """Pure decision for ONE gate. Returns {state, changed, note}. `st` is summarize()'s dict. Never raises."""
    try:
        allowed = gate_policy.GATES.get(gate, ("", ()))[1]
        n, days, syms = int(st.get("n") or 0), int(st.get("days") or 0), int(st.get("symbols") or 0)
        mean, lb, loo = st.get("mean"), st.get("lb"), st.get("loo_min")
        min_n, min_days, min_syms = _i("GATE_POLICY_MIN_N", 30), _i("GATE_POLICY_MIN_DAYS", 5), _i("GATE_POLICY_MIN_SYMBOLS", 20)
        hyst = _f("GATE_POLICY_HYST_PCT", 0.10)
        step_days = _i("GATE_POLICY_STEP_DAYS", 7)
        head = f"n={n} days={days} symbols={syms} mean_net60={'-' if mean is None else format(mean, '+.2f')}% lb={'-' if lb is None else format(lb, '+.2f')}%"
        if cur_state == gate_policy.OFF:
            return {"state": cur_state, "changed": False, "note": f"OFF is set by an operator; the job leaves it. {head}"}
        if cur_state == gate_policy.HARD:
            want, why = gate_policy.SOFT, ""
            if gate_policy.SOFT not in allowed:
                return {"state": cur_state, "changed": False, "note": f"HARD (no SOFT state for this gate). {head}"}
            why = loosen_check(st)                                    # E285 batch 6: one rule, shared with the scorecard verdict
            if why:
                return {"state": cur_state, "changed": False, "note": f"stays HARD: {why}. {head}"}
            want_state = want
        else:                                                              # SOFT
            if n < min_n or mean is None or mean >= -hyst:
                return {"state": cur_state, "changed": False, "note": f"stays SOFT ({'n < %d' % min_n if n < min_n else 'net60 not under -%.2f%%' % hyst}). {head}"}
            want_state = gate_policy.HARD
        if last_change_day:
            try:
                age = (date.fromisoformat(today) - date.fromisoformat(last_change_day)).days
                if age < step_days:
                    return {"state": cur_state, "changed": False, "note": f"{want_state} earned but the last step was {age} day(s) ago (one step per {step_days} days). {head}"}
            except ValueError:
                pass
        return {"state": want_state, "changed": True, "note": f"{cur_state} -> {want_state}. {head}" + (f" loo_min={loo:+.2f}%" if loo is not None else "")}
    except Exception as e:  # noqa: BLE001
        return {"state": cur_state, "changed": False, "note": f"evaluation error {type(e).__name__}: kept {cur_state}"}


# ---------------------------------------------------------------------------------------------------- data

def collect(session: Any, models: Any, now_utc: datetime, days: int, bars_dir: str, cost_pct: float, load_bars: Any = None) -> Tuple[Dict[Tuple[str, str], List[Sample]], Dict[str, int]]:
    """Samples per (gate, side) from the candidate log + recorded bars, plus counts {rows, samples, no_bars, deduped}. `load_bars(symbol, day)` may be injected (tests)."""
    from sqlalchemy import or_
    M = models.ScalpCandidateLog
    since = now_utc.replace(tzinfo=None) - timedelta(days=days)
    likes = [M.reason.like(g + "%") for g in gate_policy.GATES]
    q = session.query(M.symbol, M.decision, M.reason, M.created_at).filter(M.created_at >= since, M.decision.in_(("SKIPPED", "SOFT")), or_(*likes)).order_by(M.id)
    rows = [{"symbol": r.symbol, "decision": r.decision, "reason": r.reason, "created_ts": calendar.timegm(r.created_at.timetuple())} for r in q.all()]
    return samples_from_rows(rows, load_bars or candidate_outcomes.file_loader(bars_dir), cost_pct)


def samples_from_rows(rows: Iterable[Dict[str, Any]], load_bars: Any, cost_pct: float) -> Tuple[Dict[Tuple[str, str], List[Sample]], Dict[str, int]]:
    """rows: dicts with symbol, decision, reason, created_ts (UTC epoch s). One sample per (symbol, gate, IST day, 30-minute bucket), as the scorecard does."""
    out: Dict[Tuple[str, str], List[Sample]] = {}
    c = {"rows": 0, "samples": 0, "no_bars": 0, "deduped": 0}
    seen = set()
    for r in rows:
        try:
            c["rows"] += 1
            gk = gate_key(r.get("reason"), r.get("decision"))
            if gk not in gate_policy.GATES:
                continue
            side = "SHORT" if str(r.get("reason") or "").upper().startswith("SHORT_") else gate_policy.LONG
            ts = float(r["created_ts"])
            sym = str(r["symbol"]).upper()
            day = datetime.fromtimestamp(ts, timezone.utc).astimezone(timezone(IST)).strftime("%Y-%m-%d")
            key = (sym, gk, day, int(ts // 1800), side)
            if key in seen:
                c["deduped"] += 1
                continue
            seen.add(key)
            lab = candidate_outcomes.label(load_bars(sym, day), ts, side)
            r60 = lab.get("r60") if lab.get("status") == "ok" else None
            if r60 is None:
                c["no_bars"] += 1
                continue
            out.setdefault((gk, side), []).append((day, sym, round(float(r60) - cost_pct, 4)))
            c["samples"] += 1
        except Exception:  # noqa: BLE001
            continue
    return out, c


def decide_all(samples: Dict[Tuple[str, str], List[Sample]], current: Dict[Tuple[str, str], Dict[str, Any]], today: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Run evaluate_gate for every adaptable gate (long side, plus any other side that has a row or samples). `current` maps (gate, side) -> {state, last_change_day, since_day}."""
    n_boot, conf = _i("GATE_POLICY_BOOT_N", 2000), _f("GATE_POLICY_CONFIDENCE", 0.95)
    keys = {(g, gate_policy.LONG) for g in gate_policy.GATES} | {k for k in samples if k[0] in gate_policy.GATES} | {k for k in current if k[0] in gate_policy.GATES}
    res: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for k in sorted(keys):
        gate, side = k
        cur = current.get(k) or {}
        cur_state = str(cur.get("state") or gate_policy.static_state(gate)).upper()
        st = summarize(samples.get(k, []), n_boot, conf)
        ev = evaluate_gate(gate, cur_state, st, today, cur.get("last_change_day"))
        res[k] = {**ev, "stats": st, "from": cur_state, "had_row": bool(cur)}
    return res


# ---------------------------------------------------------------------------------------------------- run

def _today_ist(now_utc: datetime) -> str:
    return (now_utc + IST).strftime("%Y-%m-%d")


def due(now: Optional[datetime] = None) -> bool:
    """True once per trading day after GATE_POLICY_JOB_AT_IST. Cheap, no DB."""
    try:
        if not bool(getattr(config, "GATE_POLICY_JOB_ENABLED", True)):
            return False
        from tz_utils import ist_now, is_nse_holiday, ist_today_str
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(now):
            return False
        hh, mm = (str(getattr(config, "GATE_POLICY_JOB_AT_IST", "16:30")).split(":") + ["0"])[:2]
        if (n.hour, n.minute) < (int(hh), int(mm)):
            return False
        return _last_run_day != ist_today_str(now) and time.monotonic() >= _retry_after
    except Exception:  # noqa: BLE001
        return False


def run(session_factory: Any, now_utc: Optional[datetime] = None, force: bool = False, load_bars: Any = None) -> Dict[str, Any]:
    """Evaluate and write the policy. Returns {"ok", "day", "counts", "gates": {gate|side: {...}}}. Never raises; a failure writes nothing and leaves the policy as it was."""
    global _last_run_day, _retry_after
    now_utc = now_utc or datetime.now(timezone.utc)
    today = _today_ist(now_utc)
    s = None
    try:
        import models
        s = session_factory()
        existing = {(r.gate, r.side or gate_policy.LONG): r for r in s.query(models.ScalpGatePolicy).all()}
        if not force and any(r.last_eval_day == today for r in existing.values()):
            _last_run_day = today
            return {"ok": True, "day": today, "skipped": "already evaluated today", "gates": {}}
        samples, counts = collect(s, models, now_utc, _i("GATE_POLICY_DAYS", 15), str(getattr(config, "BAR_RECORD_DIR", "/data/bars")), _f("GATE_POLICY_COST_PCT", 0.15), load_bars)
        # A row the job or an operator never MOVED only records the evidence; the gate keeps following its own config (so a later config change still works).
        current = {k: {"state": r.state, "last_change_day": r.last_change_day, "since_day": r.since_day} for k, r in existing.items() if r.last_change_day}
        decided = decide_all(samples, current, today)
        report: Dict[str, Any] = {}
        for k, d in decided.items():
            gate, side = k
            st = d["stats"]
            row = existing.get(k)
            if row is None:
                row = models.ScalpGatePolicy(gate=gate, side=side, state=d["state"], since_day=today, last_change_day=today if d["changed"] else None)
                s.add(row)
            elif d["changed"]:
                row.state, row.since_day, row.last_change_day = d["state"], today, today
            else:
                row.state = d["state"]                    # unmoved rows follow the config: keep the shown state current
            row.last_eval_day, row.n_samples, row.n_days, row.n_symbols = today, st["n"], st["days"], st["symbols"]
            row.mean_net60, row.lb_net60, row.loo_min_net60, row.note = st["mean"], st["lb"], st["loo_min"], d["note"][:1000]
            row.updated_at = now_utc.replace(tzinfo=None)
            report[f"{gate}|{side}"] = {"from": d["from"], "state": d["state"], "changed": d["changed"], "note": d["note"], **{k2: st[k2] for k2 in ("n", "days", "symbols", "mean", "lb", "loo_min")}}
            if d["changed"]:
                logger.warning("gate policy: %s %s", gate, d["note"])
        s.commit()
        _last_run_day = today
        gate_policy.set_cache({k3: v["state"] for k3, v in decided.items() if v["changed"] or k3 in current})
        logger.info("gate policy evaluated for %s: %s | %s", today, counts, "; ".join(f"{k}={v['state']}" for k, v in report.items()))
        return {"ok": True, "day": today, "counts": counts, "gates": report}
    except Exception as e:  # noqa: BLE001
        logger.warning("gate policy job failed (policy unchanged): %s", str(e)[:300])
        _retry_after = time.monotonic() + 600.0
        try:
            if s is not None:
                s.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("gate_policy_job: session rollback failed (ignored)", exc_info=True)
        return {"ok": False, "day": today, "error": f"{type(e).__name__}: {str(e)[:200]}", "gates": {}}
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:  # noqa: BLE001
            logger.debug("gate_policy_job: session close failed (ignored)", exc_info=True)


def set_state(session_factory: Any, gate: str, state: str, side: str = gate_policy.LONG, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Operator override (POST /gate-policy/set): writes one row's state, counts as the gate's step for today. OFF is only reachable this way."""
    now_utc = now_utc or datetime.now(timezone.utc)
    state = str(state or "").upper()
    if gate not in gate_policy.GATES or state not in gate_policy.GATES[gate][1] or side not in (gate_policy.LONG, "SHORT"):
        return {"ok": False, "error": f"unknown gate / state / side ({gate}, {state}, {side})"}
    s = None
    try:
        import models
        s = session_factory()
        today = _today_ist(now_utc)
        row = s.query(models.ScalpGatePolicy).filter_by(gate=gate, side=side).first()
        if row is None:
            row = models.ScalpGatePolicy(gate=gate, side=side, state=state)
            s.add(row)
        row.state, row.since_day, row.last_change_day, row.note = state, today, today, "set by an operator"
        row.updated_at = now_utc.replace(tzinfo=None)
        s.commit()
        cache = {(r.gate, r.side or gate_policy.LONG): str(r.state).upper() for r in s.query(models.ScalpGatePolicy).all() if r.last_change_day}
        gate_policy.set_cache(cache)
        return {"ok": True, "gate": gate, "side": side, "state": state}
    except Exception as e:  # noqa: BLE001
        try:
            if s is not None:
                s.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("gate_policy_job: session rollback failed (ignored)", exc_info=True)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:  # noqa: BLE001
            logger.debug("gate_policy_job: session close failed (ignored)", exc_info=True)


def tick_due() -> bool:
    """Cheap check for the trading loop: does anything need doing this tick (cache refresh or the nightly run)?"""
    return gate_policy.refresh_due() or due()


def tick(session_factory: Any) -> None:
    """Called from the trading loop in a worker thread: refresh the cache when stale, run the nightly job when due. Never raises."""
    try:
        if gate_policy.refresh_due():
            gate_policy.refresh(session_factory)
        if due():
            run(session_factory)
    except Exception:  # noqa: BLE001
        logger.debug("gate policy tick failed", exc_info=True)
