"""notify.py - group 447: a Telegram message when an AUTOMATIC run (the timetable) finishes or is blocked.

Goes through the repo's own notification service (POST <NOTIFICATION_URL>/notification/notify, channel "telegram"), the same path the trading services use,
so it needs no token here. Plain text only (job titles, not ids: underscores break Markdown). Never raises: a failed message only gets a note on the run
(`notified`) and never changes the run's status. Manual runs are not announced unless RUNNER_NOTIFY_MANUAL=true.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
ICON = {"success": "✅", "attention": "⚠️", "failed": "❌", "timeout": "⏱", "cancelled": "🚫", "blocked": "⛔"}
WORD = {"success": "OK", "attention": "LOOK", "failed": "FAILED", "timeout": "TIMEOUT", "cancelled": "CANCELLED", "blocked": "BLOCKED"}
MAX_CHARS = 1500


def _hhmm(ts: Optional[float]) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _dur(a: Optional[float], b: Optional[float]) -> str:
    if not a or not b or b < a:
        return ""
    s = int(round(b - a))
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else (f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s")


def _advice_lines(run_dir: str) -> List[str]:
    """The Service ON / OFF verdicts, when this run produced them (service_advice.json next to the output)."""
    try:
        doc = json.load(open(os.path.join(run_dir, "service_advice.json"), encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no advice in this run
        return []
    out: List[str] = []
    for svc, r in (doc.get("services") or {}).items():
        why = (r.get("why") or [""])[0]
        out.append(f"{svc}: {r.get('verdict')}" + (f" - {why[:150]}" if why else ""))
    return (["Service advice (recommendation only):"] + out) if out else []


def build_message(run: Dict[str, Any], run_dir: str) -> Dict[str, str]:
    st = str(run.get("status") or "failed")
    auto = run.get("trigger") == "schedule"
    title = f"{ICON.get(st, '•')} {run.get('title') or run.get('target')}: {WORD.get(st, st.upper())}"
    lines = [("Automatic run (timetable)" if auto else f"Manual run by {run.get('by') or '-'}") + f" - finished {_hhmm(run.get('finished_at'))} IST"
             + (f", took {_dur(run.get('started_at'), run.get('finished_at'))}" if run.get("started_at") else "")]
    if st == "blocked":
        lines += [str(run.get("note") or "")[:300], "It did NOT run."]
    jobs = run.get("jobs") or []
    if jobs:
        lines.append("")
        lines += [f"{WORD.get(j.get('status'), str(j.get('status')).upper()):<9}{j.get('title')}" for j in jobs[:14]]
        if len(jobs) > 14:
            lines.append(f"... and {len(jobs) - 14} more")
    advice = _advice_lines(run_dir)
    hl = [h for h in (run.get("headline") or []) if h][:4]
    if advice:
        lines += [""] + advice
    elif hl:
        lines += ["", "Headlines:"] + [f"- {h[:140]}" for h in hl]
    lines += ["", "Full text: Real Money Calibration > Runs & downloads."]
    text = "\n".join(lines)
    return {"title": "Stockky calibration", "head": title, "message": (title + "\n" + text)[:MAX_CHARS]}


def send(base_url: str, title: str, message: str, urgency: str = "normal", timeout: float = 10.0) -> str:
    """'sent' | 'not delivered: <why>' | 'error: <why>'. The notification service is mounted at /notification on its port."""
    base = (base_url or "").rstrip("/")
    url = base + "/notify" if base.endswith("/notification") else base + "/notification/notify"
    body = json.dumps({"title": title, "message": message, "channel": "telegram", "urgency": urgency}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed internal URL from the environment
            doc = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return f"error: HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return f"error: {type(e).__name__}"
    return "sent" if doc.get("delivered") else "not delivered: " + str(doc.get("note") or "no channel enabled")[:120]


def make_notifier(base_url: str, enabled: bool = True, manual: bool = False, redact: Optional[Callable[[str], str]] = None,
                  sender: Callable[..., str] = send) -> Callable[[Dict[str, Any], str], Optional[str]]:
    """Returns notify(run_dict, run_dir) -> result text, or None when this run is not announced."""
    def notify(run: Dict[str, Any], run_dir: str) -> Optional[str]:
        if not enabled:
            return None
        if run.get("trigger") != "schedule" and not manual:
            return None
        m = build_message(run, run_dir)
        text = redact(m["message"]) if redact else m["message"]
        bad = str(run.get("status")) in ("failed", "timeout", "blocked", "attention")
        return sender(base_url, m["title"], text, "high" if bad else "normal")
    return notify
