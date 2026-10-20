"""notify.py - group 447: a Telegram message when an AUTOMATIC run (the timetable) finishes or is blocked.

Goes through the repo's own notification service (POST <NOTIFICATION_URL>/notification/notify, channel "telegram"), the same path the trading services use,
so it needs no token here. Plain text only (job titles, not ids: underscores break Markdown). Never raises: a failed message only gets a note on the run
(`notified`) and never changes the run's status. Manual runs are not announced unless RUNNER_NOTIFY_MANUAL=true.

group 466: a message the notification service could not take (it was restarting / starved of CPU right after the 16:05 close: "Telegram error: URLError")
is kept in an Outbox folder on the data volume and sent again every RUNNER_OUTBOX_RETRY_S seconds until it goes through, is refused for good, or is older
than RUNNER_OUTBOX_MAX_AGE_S. A failed run's message now also says WHICH step failed.
"""
from __future__ import annotations

import json
import os
import threading
import time
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
    elif st in ("failed", "timeout") and run.get("note"):
        lines.append(str(run.get("note"))[:300])
    jobs = run.get("jobs") or []
    if jobs:
        lines.append("")
        lines += [f"{WORD.get(j.get('status'), str(j.get('status')).upper()):<9}{j.get('title')}" for j in jobs[:14]]
        if len(jobs) > 14:
            lines.append(f"... and {len(jobs) - 14} more")
    bad_steps = [x for x in (run.get("steps") or []) if str(x.get("status")) in ("failed", "timeout", "cancelled")]
    if bad_steps:                                                       # group 466: say WHAT failed, not just "failed"
        lines += ["", "What failed:"]
        for x in bad_steps[:6]:
            det = str(x.get("detail") or "").strip()
            lines.append(f"- {str(x.get('title') or x.get('job') or 'step')[:80]}" + (f": {det[:120]}" if det else ""))
        if len(bad_steps) > 6:
            lines.append(f"... and {len(bad_steps) - 6} more")
    advice = _advice_lines(run_dir)
    hl = [h for h in (run.get("headline") or []) if h][:4]
    if advice:
        lines += [""] + advice
    elif hl:
        lines += ["", "Headlines:"] + [f"- {h[:140]}" for h in hl]
    lines += ["", "Full text: Real Money Calibration > Runs & downloads."]
    text = "\n".join(lines)
    return {"title": "Stockky calibration", "head": title, "message": (title + "\n" + text)[:MAX_CHARS]}


def _why(e: BaseException) -> str:
    """'URLError: [Errno 111] Connection refused' instead of a bare 'URLError' (the page shows this text, so it has to say what to fix)."""
    inner = getattr(e, "reason", None) or e
    detail = str(inner).strip()[:100]
    return f"{type(e).__name__}: {detail}" if detail and detail != type(e).__name__ else type(e).__name__


def send(base_url: str, title: str, message: str, urgency: str = "normal", timeout: float = 10.0, tries: int = 3, pause: float = 4.0) -> str:
    """'sent' | 'not delivered: <why>' | 'error: <why>'. The notification service is mounted at /notification on its port.

    group 458: a run that ends while the notification service is restarting / not up yet, or a one-off network hiccup, used to lose the message
    after ONE 10 s try ("Telegram error: URLError"). Connection failures, timeouts and HTTP 502 / 503 / 504 are now retried (`tries` attempts,
    `pause` seconds apart; worst case about 40 s, and only when Telegram is unreachable, before the next queued job starts). Other HTTP errors are final."""
    base = (base_url or "").rstrip("/")
    url = base + "/notify" if base.endswith("/notification") else base + "/notification/notify"
    body = json.dumps({"title": title, "message": message, "channel": "telegram", "urgency": urgency}).encode()
    last = "error: not tried"
    for attempt in range(max(1, int(tries))):
        if attempt:
            time.sleep(max(0.0, pause))
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed internal URL from the environment
                doc = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            last = f"error: HTTP {e.code} from {base or '(no URL)'}"
            if e.code in (502, 503, 504):
                continue
            return last
        except Exception as e:  # noqa: BLE001 - URLError (refused / DNS / timeout), socket timeouts, bad JSON
            last = f"error: {_why(e)} reaching {base or '(NOTIFICATION_URL is empty)'}"
            continue
        return "sent" if doc.get("delivered") else "not delivered: " + str(doc.get("note") or "no channel enabled")[:120]
    return last + (f" (after {tries} tries; check that notification-scheduler-service is running and NOTIFICATION_URL is right)" if tries > 1 else "")


def retryable(result: Any) -> bool:
    """True when a send result is worth trying again later: a connection / timeout / 5xx error. A 4xx or 'not delivered: ...' (no channel) is final."""
    r = str(result or "")
    return r.startswith("error:") and not r.startswith("error: HTTP 4")


_AGAIN = " - will be sent again when the notification service is reachable"
_SEQ_LOCK = threading.Lock()
_SEQ = 0


def _next_seq() -> int:
    """Process-wide counter: two Outbox objects on one folder (or two adds in the same millisecond) must never pick the same file name."""
    global _SEQ
    with _SEQ_LOCK:
        _SEQ += 1
        return _SEQ


class Outbox:
    """group 466: undelivered messages kept on disk (one small JSON file each) so a restart of this service or of the notification service loses nothing."""

    def __init__(self, directory: str, max_age_s: float = 24 * 3600, max_items: int = 50):
        self.dir = str(directory)
        self.max_age_s = float(max_age_s)
        self.max_items = max(1, int(max_items))
        self._lock = threading.Lock()
        self._n = 0

    def _files(self) -> List[str]:
        try:
            return sorted(os.path.join(self.dir, f) for f in os.listdir(self.dir) if f.startswith("ob_") and f.endswith(".json"))
        except OSError:
            return []

    def pending(self) -> int:
        return len(self._files())

    def add(self, title: str, message: str, urgency: str = "normal") -> bool:
        with self._lock:
            try:
                os.makedirs(self.dir, exist_ok=True)
                self._n = _next_seq()
                name = os.path.join(self.dir, f"ob_{int(time.time() * 1000):013d}_{os.getpid()}_{self._n:09d}.json")
                tmp = name + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"title": title, "message": message, "urgency": urgency, "ts": time.time()}, fh)
                os.replace(tmp, name)                                     # atomic: never a half-written message
                for old in self._files()[:-self.max_items]:               # keep the newest max_items
                    try:
                        os.remove(old)
                    except OSError:
                        pass
                return True
            except OSError:
                return False

    def flush(self, base_url: str, sender: Callable[..., str] = send) -> Dict[str, int]:
        """Try every kept message, oldest first. Stops at the first retryable failure (the service is still down: no point hammering it)."""
        sent = dropped = 0
        with self._lock:
            files = self._files()
            left = 0
            for k, f in enumerate(files):
                try:
                    with open(f, encoding="utf-8") as fh:
                        doc = json.load(fh)
                    title, msg, urg, ts = str(doc["title"]), str(doc["message"]), str(doc.get("urgency") or "normal"), float(doc["ts"])
                except Exception:  # noqa: BLE001 - unreadable file: nothing to send
                    dropped += 1
                    self._rm(f)
                    continue
                if time.time() - ts > self.max_age_s:
                    dropped += 1
                    self._rm(f)
                    continue
                try:
                    res = sender(base_url, title, "(delayed) " + msg, urg, tries=1) if sender is send else sender(base_url, title, "(delayed) " + msg, urg)
                except Exception as e:  # noqa: BLE001
                    res = f"error: {type(e).__name__}"
                if res == "sent":
                    sent += 1
                    self._rm(f)
                elif retryable(res):
                    left = len(files) - k
                    break
                else:
                    dropped += 1
                    self._rm(f)
        return {"sent": sent, "dropped": dropped, "left": left}

    @staticmethod
    def _rm(f: str) -> None:
        try:
            os.remove(f)
        except OSError:
            pass


def start_outbox_flusher(outbox: Outbox, base_url: str, every_s: float = 60.0, sender: Callable[..., str] = send) -> threading.Thread:
    """Daemon thread: send what is waiting, sleep, repeat. Never raises."""
    def loop() -> None:
        while True:
            try:
                if outbox.pending():
                    outbox.flush(base_url, sender)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(max(0.5, float(every_s)))
    t = threading.Thread(target=loop, name="outbox-flusher", daemon=True)
    t.start()
    return t


def make_notifier(base_url: str, enabled: bool = True, manual: bool = False, redact: Optional[Callable[[str], str]] = None,
                  sender: Callable[..., str] = send, outbox: Optional[Outbox] = None) -> Callable[[Dict[str, Any], str], Optional[str]]:
    """Returns notify(run_dict, run_dir) -> result text, or None when this run is not announced. With an outbox an unreachable send is kept and retried."""
    def notify(run: Dict[str, Any], run_dir: str) -> Optional[str]:
        if not enabled:
            return None
        if run.get("trigger") != "schedule" and not manual:
            return None
        m = build_message(run, run_dir)
        text = redact(m["message"]) if redact else m["message"]
        bad = str(run.get("status")) in ("failed", "timeout", "blocked", "attention")
        urg = "high" if bad else "normal"
        res = sender(base_url, m["title"], text, urg)
        if outbox is not None and retryable(res) and outbox.add(m["title"], text, urg):
            res = str(res) + _AGAIN
        return res
    return notify


def build_skip_message(slot: Dict[str, Any], pack_title: str, reason: str) -> Dict[str, str]:
    """A timetable slot that fired but could not be submitted (already queued, queue full, unknown pack)."""
    head = f"⛔ {pack_title or slot.get('pack')}: SKIPPED"
    text = "\n".join([head, f"Automatic run (timetable) - slot {slot.get('id')} at {slot.get('time')} IST did NOT start.", str(reason)[:300],
                      "It will not be retried today.", "", "Full text: Real Money Calibration > Runs & downloads."])
    return {"title": "Stockky calibration", "message": text[:MAX_CHARS]}


def make_skip_notifier(base_url: str, enabled: bool = True, titles: Optional[Callable[[str], str]] = None, redact: Optional[Callable[[str], str]] = None,
                       sender: Callable[..., str] = send, outbox: Optional[Outbox] = None) -> Callable[[Dict[str, Any], str], Optional[str]]:
    """Returns on_skip(slot, reason) -> result text, or None when notifications are off. Never raises."""
    def on_skip(slot: Dict[str, Any], reason: str) -> Optional[str]:
        if not enabled:
            return None
        try:
            m = build_skip_message(slot, titles(slot.get("pack")) if titles else "", reason)
            text = redact(m["message"]) if redact else m["message"]
            res = sender(base_url, m["title"], text, "high")
            if outbox is not None and retryable(res) and outbox.add(m["title"], text, "high"):
                res = str(res) + _AGAIN
            return res
        except Exception as e:  # noqa: BLE001
            return f"error: {type(e).__name__}"
    return on_skip
