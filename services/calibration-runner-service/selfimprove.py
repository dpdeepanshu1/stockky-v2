"""selfimprove.py - round 541 (E566, S1 of the Self Improve plan): the READ-ONLY "loop" view behind the Self Improve System tab.

It answers one question in plain data: what is the nightly chain (scheduled tests -> daily_close -> agent -> queued tests -> auto-calibration) doing today, and when
does each part run next? It reads the timetable, the runner's recent runs, the agent's saved state and auto-calibration's last evaluation. It sets nothing, starts
nothing and calls no other service. A step whose state cannot be told is `unknown`, never a guess.

Round 544: the agent block also carries `proof` (did the agent run today, and the 17:30 IST warning state) and `recent_runs` stays the runner's job list.
Step states: done | waiting | skipped | failed | not_due | unknown.
Stdlib only. Offline tests: tests/test_round541_self_improve.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import agent as AG

IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
RUN_KEEP = 10


def _hm(t: Any) -> Optional[int]:
    return AG.parse_hhmm(t)


def next_fire(entry: Dict[str, Any], now: datetime) -> Optional[datetime]:
    """The next time (IST, after `now`) a timetable slot fires; None when it is off or has no valid time."""
    if not entry.get("enabled"):
        return None
    m = _hm(entry.get("time"))
    days = entry.get("days") or []
    if m is None or not days:
        return None
    for add in range(0, 36):
        d = (now + timedelta(days=add)).replace(hour=m // 60, minute=m % 60, second=0, microsecond=0)
        if d <= now or d.weekday() not in days:
            continue
        if entry.get("first_week_only") and d.day > 7:
            continue
        return d
    return None


def slot_view(entry: Dict[str, Any], last_fired: Dict[str, str], now: datetime) -> Dict[str, Any]:
    """One timetable slot: whether it is due today, has fired, still waits, or was missed (the timetable never catches a missed slot up)."""
    today = now.strftime("%Y-%m-%d")
    m = _hm(entry.get("time"))
    due_today = bool(entry.get("enabled")) and now.weekday() in (entry.get("days") or []) and not (entry.get("first_week_only") and now.day > 7)
    fired = last_fired.get(entry.get("id")) == today
    if not entry.get("enabled"):
        today_state = "off"
    elif not due_today:
        today_state = "not_today"
    elif fired:
        today_state = "fired"
    elif m is not None and now.hour * 60 + now.minute < m:
        today_state = "waiting"
    else:
        today_state = "not_fired"          # the time passed and the slot did not fire (container down at that minute, or the timetable was off)
    nxt = next_fire(entry, now)
    return {"id": entry.get("id"), "pack": entry.get("pack"), "time": entry.get("time"), "days": [DAYS[d] for d in (entry.get("days") or []) if 0 <= d <= 6], "enabled": bool(entry.get("enabled")),
            "today": today_state, "next_ist": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None}


def _run_state(status: str) -> str:
    return {"success": "done", "attention": "done", "failed": "failed", "running": "waiting", "queued": "waiting", "blocked": "skipped", "cancelled": "skipped"}.get(status, "unknown")


def _step(sid: str, title: str, state: str, detail: str) -> Dict[str, Any]:
    return {"id": sid, "title": title, "state": state, "detail": detail}


def _iso_day(v: Any) -> str:
    try:
        return datetime.fromtimestamp(float(v), IST).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _run_today(runs: List[Dict[str, Any]], target: str, today: str) -> Optional[Dict[str, Any]]:
    for r in runs:                      # runs are newest first
        if r.get("target") == target and str(r.get("queued_at") or "").startswith(today):
            return r
    return None


def _chain(agent: Any, auto_status: Dict[str, Any], sched: Dict[str, Any], runs: List[Dict[str, Any]], now: datetime, now_ts: float) -> List[Dict[str, Any]]:
    today = now.strftime("%Y-%m-%d")
    weekday = now.weekday() < 5
    steps: List[Dict[str, Any]] = []
    # 1. the evening test pack in front of the agent
    ent = next((e for e in sched.get("entries", []) if e.get("pack") == "daily_close"), None)
    if ent is None:
        steps.append(_step("daily_close", "Evening tests (daily_close)", "unknown", "no daily_close slot in the timetable"))
    else:
        sv = slot_view(ent, sched.get("last_fired") or {}, now)
        run = _run_today(runs, "daily_close", today)
        if not sched.get("enabled"):
            steps.append(_step("daily_close", "Evening tests (daily_close)", "skipped", "the timetable is switched off"))
        elif sv["today"] in ("off", "not_today"):
            steps.append(_step("daily_close", "Evening tests (daily_close)", "not_due" if sv["today"] == "not_today" else "skipped", "no slot today" if sv["today"] == "not_today" else "the slot is off"))
        elif sv["today"] == "waiting":
            steps.append(_step("daily_close", "Evening tests (daily_close)", "waiting", f"due {ent.get('time')} IST"))
        elif run is not None:
            steps.append(_step("daily_close", "Evening tests (daily_close)", _run_state(str(run.get("status"))), f"{run.get('status')} ({run.get('trigger')})"))
        elif sv["today"] == "fired":
            steps.append(_step("daily_close", "Evening tests (daily_close)", "unknown", "the slot fired but its run is not in the last runs list"))
        else:
            steps.append(_step("daily_close", "Evening tests (daily_close)", "skipped", f"the {ent.get('time')} slot did not fire (a missed slot is not caught up)"))
    # 2. the agent
    st = agent._state()
    last = st.get("last") or {}
    nxt = agent.next_run(now_ts)
    eff = agent.eff_mode()
    ran_today = str(last.get("date") or "") == today
    if eff == "off":
        steps.append(_step("agent", "AI agent", "skipped", "the agent is off (mode off, or it switched itself off)"))
    elif ran_today:
        bad = (not last.get("source")) and bool(last.get("note")) and "no LLM answered" in str(last.get("note"))
        steps.append(_step("agent", "AI agent", "failed" if bad else "done", (str(last.get("note")) if bad else f"ran ({last.get('trigger')}), {last.get('ideas', 0)} idea(s), {last.get('dropped', 0)} dropped")))
    elif not weekday:
        steps.append(_step("agent", "AI agent", "not_due", "no automatic run on a weekend"))
    elif nxt.get("state") == "due_now":
        steps.append(_step("agent", "AI agent", "waiting", "due now: it starts within a minute"))
    elif nxt.get("state") == "scheduled" and str(nxt.get("at_ist") or "").startswith(today):
        steps.append(_step("agent", "AI agent", "waiting", f"runs after {nxt['at_ist'][-5:]} IST"))
    else:
        steps.append(_step("agent", "AI agent", "skipped", "today's window has passed and it did not run"))
    # 3. the tests the agent queued
    rep = st.get("last_report") or {}
    if not ran_today:
        steps.append(_step("queued_tests", "Tests queued by the agent", "not_due", "waits for the agent"))
    elif str(rep.get("mode")) != "propose":
        steps.append(_step("queued_tests", "Tests queued by the agent", "not_due", "report mode: the agent queues nothing"))
    else:
        q = rep.get("queued") or []
        by_id = {r.get("id"): r for r in runs}
        if not q:
            sk = rep.get("skipped") or []
            steps.append(_step("queued_tests", "Tests queued by the agent", "skipped", (str(sk[0].get("reason")) if sk and isinstance(sk[0], dict) else "no test was queued")))
        else:
            sts = [_run_state(str((by_id.get(x.get("run")) or {}).get("status", "?"))) for x in q]
            state = "failed" if "failed" in sts else "waiting" if "waiting" in sts else "unknown" if "unknown" in sts else "skipped" if "skipped" in sts and "done" not in sts else "done"
            steps.append(_step("queued_tests", "Tests queued by the agent", state, f"{len(q)} queued: " + ", ".join(sorted({str(x.get('test')) for x in q}))))
    # 4. auto-calibration
    s = auto_status.get("settings") or {}
    la = auto_status.get("last") or {}
    if not s.get("enabled"):
        steps.append(_step("autocalib", "Auto-calibration", "skipped", "auto-calibration is switched off"))
    elif _iso_day(la.get("at")) == today:
        steps.append(_step("autocalib", "Auto-calibration", "done", f"{la.get('trigger')}: {la.get('changed', 0)} changed, {la.get('waiting', 0)} waiting" + (" (dry-run: reported only)" if la.get("dry_run") else "")))
    else:
        steps.append(_step("autocalib", "Auto-calibration", "waiting", "evaluates after a finished test; nothing evaluated yet today"))
    return steps


def loop_view(agent: Any, auto: Any, sched_state: Dict[str, Any], runs: List[Dict[str, Any]], now_ts: Optional[float] = None) -> Dict[str, Any]:
    """The whole nightly loop in one read: banner, timetable, agent, auto-calibration, today's chain and the latest runs. Never raises for a missing part."""
    ts = float(now_ts if now_ts is not None else agent.now())
    now = datetime.fromtimestamp(ts, IST)
    acs = auto.settings()
    ast = auto.status(with_coverage=False)
    entries = [e for e in sched_state.get("entries", []) if isinstance(e, dict)]
    last_fired = sched_state.get("last_fired") or {}
    st = agent.status()
    return {
        "now_ist": now.strftime("%Y-%m-%d %H:%M"), "weekday": DAYS[now.weekday()],
        "banner": AG.banner(acs, list(agent.rules)),
        "agent": {"mode": st["mode"], "mode_effective": st["mode_effective"], "stopped": st["stopped"], "run_after_ist": st["run_after_ist"], "next_run": st["next_run"],
                  "last": st.get("last"), "last_auto_date": st.get("last_auto_date"), "run_now_allowed": st["run_now_allowed"], "market_open": st["market_open"],
                  "keys": st["keys"], "usage_today": st["usage_today"], "scoreboard": st["scoreboard"], "settings": st["settings"], "proof": agent.proof(ts)},
        "schedule": {"enabled": bool(sched_state.get("enabled")), "slots": [slot_view(e, last_fired, now) for e in entries], "last_skipped": sched_state.get("last_skipped")},
        "autocalib": {"settings": acs, "last": ast.get("last"), "pending": ast.get("pending") or {}, "recent_changes": (ast.get("history") or [])[:5]},
        "chain": _chain(agent, ast, sched_state, runs, now, ts),
        "recent_runs": [{k: r.get(k) for k in ("id", "kind", "target", "status", "trigger", "queued_at", "finished_at")} for r in runs[:RUN_KEEP]],
    }
