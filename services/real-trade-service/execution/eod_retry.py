"""EOD square-off retry (E285 round 170, C1). Standard library only: the database, broker, clock and Telegram are passed in as functions.

Why: `_eod_squareoff` runs ONCE a day (the gate stores `eod_squareoff_last_run` before anything is sent). A Dhan MARKET sell that was cancelled or
rejected (28 and 29 Oct) was never sent again, so a position could still be open at the 15:30 close.

The 15:00 run stores a PLAN in the database (`new_plan`): the ids of the positions it meant to sell, i.e. everything except the overnight holds.
The retry pass (`run_pass`) only ever touches those ids, so an intended overnight hold is never sold, and a restart between 15:00 and the cut-off
loses nothing because the plan, the attempt count and the "alerted" flag all live in the stored plan. No plan for today = no retry, no alert.
"""
from __future__ import annotations

from datetime import time as _time
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

PLAN_PREFIX = "eod_retry_plan"


def plan_key(mode: str, day: str) -> str:
    return f"{PLAN_PREFIX}_{mode}_{day}"


def new_plan(ids: Iterable[int], now_ts: float) -> Dict:
    return {"ids": sorted({int(i) for i in ids}), "tries": 0, "last": float(now_ts), "alerted": False}


def retry_due(*, now_ist: _time, now_ts: float, last_ts: float, tries: int, max_tries: int, interval_s: float, until: _time) -> bool:
    """Another attempt may go out: tries left, not past the cut-off, and one interval since the last attempt."""
    if tries >= max_tries or now_ist > until:
        return False
    return (now_ts - last_ts) >= interval_s


def alert_due(*, now_ist: _time, now_ts: float, last_ts: float, tries: int, max_tries: int, interval_s: float, until: _time, alerted: bool) -> bool:
    """The 'still open' alert: no attempt is left (tries used up or cut-off passed), one interval has passed since the last attempt
    so a fill has had time to be booked, and it has not been sent today."""
    if alerted:
        return False
    if tries < max_tries and now_ist <= until:
        return False
    return (now_ts - last_ts) >= interval_s


def owed(open_positions: Iterable, ids: Iterable[int]) -> List:
    """Open positions with shares left that the 15:00 run meant to sell."""
    want = set(ids or ())
    return [p for p in open_positions if getattr(p, "id", None) in want and float(getattr(p, "qty_open", 0) or 0) > 0]


def _names(positions: Sequence) -> str:
    return ", ".join(f"{p.symbol} x{int(float(p.qty_open))}" for p in positions)


def alert_text(mode: str, positions: Sequence, tries: int) -> str:
    return (f"🚨 *EOD square-off NOT flat — {mode}*\n"
            f"Still open after {tries} retry attempt(s): {_names(positions)}.\n"
            "Close them by hand on Dhan before 15:30.")


def retry_note(mode: str, n: int, sent: int, failed: int) -> str:
    return f"🔁 *EOD retry {n} — {mode}*: sent {sent} sell order(s)" + (f", {failed} could not be sent." if failed else ".")


async def run_pass(*, mode: str, plan: Dict, load_open: Callable[[], Sequence], has_pending: Callable[[str], bool],
                   send_sell: Callable[[object, int], bool], notify: Callable[[str], Awaitable[None]],
                   now_ts: float, now_ist: _time, interval_s: float, max_tries: int, until: _time) -> Dict:
    """One pass of the retry. Returns the (possibly updated) plan; the caller stores it. Never raises for a failed send."""
    left = owed(load_open(), plan.get("ids") or ())
    if not left:
        return plan
    tries, last = int(plan.get("tries", 0)), float(plan.get("last", 0))
    if retry_due(now_ist=now_ist, now_ts=now_ts, last_ts=last, tries=tries, max_tries=max_tries, interval_s=interval_s, until=until):
        plan = dict(plan, tries=tries + 1, last=now_ts)   # counted before sending: a crash mid-send must not allow an unbounded loop
        sent = failed = waiting = 0
        for p in left:
            if has_pending(p.symbol):
                waiting += 1          # a SELL is already working at the broker: not ours to repeat
                continue
            try:
                ok = send_sell(p, int(p.qty_open))
            except Exception:
                ok = False
            sent += 1 if ok else 0
            failed += 0 if ok else 1
        plan["last_result"] = {"sent": sent, "failed": failed, "waiting": waiting}
        if sent or failed:
            await notify(retry_note(mode, plan["tries"], sent, failed))
        return plan
    if alert_due(now_ist=now_ist, now_ts=now_ts, last_ts=last, tries=tries, max_tries=max_tries, interval_s=interval_s, until=until,
                 alerted=bool(plan.get("alerted"))):
        plan = dict(plan, alerted=True)
        await notify(alert_text(mode, left, tries))
    return plan
