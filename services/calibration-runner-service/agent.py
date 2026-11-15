"""agent.py - E553 (round 533, hardened in round 536): the nightly AI idea source. An IDEA source, never a decision maker.

What it does, once a trading evening after AGENT_RUN_AFTER_IST (and on the page's / API's "run now"):
  1. builds a small input from what auto-calibration already reads: for every auto-calibrated switch its current value, the verdict of its test and the age of that test
     (numbers and short verdict lines only: no keys, balances, account ids or trade rows);
  2. asks a free LLM (Gemini first, Groq as the fallback) which of those switches look weak and which way each should move, as JSON only;
  3. checks the answer with plain code: the switch must be in the allow-list (the auto-calibrated, NON-protected switches), the direction must be up or down (one step,
     nothing else is kept), at most AGENT_MAX_IDEAS per night, pinned switches dropped. Free text only ever reaches Telegram and the saved file, never a setting;
  4. saves agent/YYYY-MM-DD.json, counts the ideas in agent/trials.json and sends one Telegram message.

Modes (AGENT_MODE): off = nothing runs; report = steps 1-4; propose = also queue the EXISTING test that decides each idea's switch (the same job a button would start).
There is no apply mode: this module has no code path that sets a switch, places an order or touches an account. Whether anything changes is decided by the normal
auto-calibration rules (two agreeing runs to loosen, step limit, cool-down, protected switches) after the test finishes.

If both LLMs fail, nothing breaks: the run is saved with the errors, no idea message is sent for an automatic run, trading is untouched.
Round 536 (agent Rounds 4, 5 and 6):
  * health: a retired model id falls back to the backup model, then Groq; a 429 backs off ONCE (a DAILY quota 429 skips Gemini for the rest of the day); every
    fallback is written into the run and into the ONE Telegram message a night; no retry loop;
  * privacy guard: only whitelisted aggregate fields reach the prompt, and the finished prompt is checked for any known secret / key-like text / raw-row field
    names BEFORE a single call is made (a hit blocks the whole run);
  * limits: an answer over MAX_ANSWER_CHARS is rejected, a fenced / truncated / trailing-comma JSON answer is repaired, a daily call cap (agent/usage.json) is kept;
  * scoreboard: each idea is later judged by what its own test says (survived / not_confirmed / contradicted / unclear / no_result); after 50 ideas with at least
    20 tested and under 5% survived the agent switches ITSELF off (state/agent.json, survives a restart) until you press Resume; 3 answers outside the
    allow-list in 7 days sends a warning;
  * run now (POST /agent/run, the runner page button) is refused while the market-hours guard is on.
Round 541 (S1 of the Self Improve plan, E566):
  * settings on disk: mode, run-after time, max ideas and max queued tests are saved in state/agent.json (bounded; the saved value wins over the .env default, a reset
    goes back to the .env value); every change is one line in agent/audit.jsonl; switching to propose needs confirm=true (the page's second click);
  * every run carries a plain "breakdown" (how many switches are pinned / without a test result / on hold / would change, and WHY no ideas) near the top of the report,
    and the auto-calibration state (LIVE / DRY-RUN / OFF) as one line;
  * an idea check after the validator: an idea is moved to `dropped` (with the reason) when its switch has no usable test result, when it asks for a move that is
    impossible (an off switch can not go down) or when it points the OPPOSITE way to the switch's own rule. Every kept idea says whether its rule agrees or holds;
  * history(): one line per saved day (ideas, outcome from the scoreboard, breakdown); next_run(): the next automatic run time.
Stdlib only. Offline tests: tests/test_agent.py, tests/test_agent_hardening.py and tests/test_round541_self_improve.py.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import autocalib as AC

IST = timezone(timedelta(hours=5, minutes=30))
MODES = ("off", "report", "propose")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
UA = "Mozilla/5.0 (compatible; stockky-agent/1)"      # Groq's edge refuses the bare urllib agent
TRIALS_KEEP = 300
MAX_ANSWER_CHARS = 12000                              # a longer LLM answer is rejected unread
MAX_PROMPT_CHARS = 24000
MAX_SWITCH_ROWS = 60
RETRY_429_S = 20.0                                    # one back-off on a per-minute 429
RESOLVE_AFTER_D = 7                                   # an idea whose test has not produced a newer result after this many days is "no_result"
STOP_MIN_IDEAS, STOP_MIN_TESTED, STOP_BELOW_PCT = 50, 20, 5.0
OFFLIST_WARN_N, OFFLIST_WINDOW_D = 3, 7
STOP_RULE = "self-off after 50 ideas when at least 20 are tested and under 5% survived"
_BANNED_IN_PROMPT = re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|client[_-]?id|access[_-]?token|bearer|authorization|account[_-]?(id|no|number)|balance|order[_-]?id|trade[_-]?id|pnl)\b")
_KEY_SHAPES = (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), re.compile(r"gsk_[0-9A-Za-z]{16,}"), re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"))
RUN_AFTER_MIN, RUN_AFTER_MAX = 15 * 60 + 45, 23 * 60  # a SAVED run-after time stays between 15:45 (market guard over) and 23:00 IST (the window ends 23:30)
IDEAS_BOUNDS, QUEUE_BOUNDS = (1, 10), (0, 6)
AUDIT_KEEP = 200
MAX_HEAVY_QUEUE_S = 3600                              # a test that runs longer than this, or needs a download first, is never queued by the agent

SYSTEM_PROMPT = (
    "You review the automatic calibration of an Indian equity trading system. You are given, for each tunable switch, its current value, the verdict of the test that "
    "decides it and how old that test is. Pick at most {n} switches whose current setting looks weak or stale given the verdicts, and say which way each should move. "
    "You only suggest; a separate test and rule set decides every change. Use ONLY switch names that appear in the input. "
    "Answer with JSON only, no prose, exactly: "
    '{{"weaknesses": ["short sentence", ...], "ideas": [{{"switch": "NAME", "direction": "up" or "down", "why": "one short sentence"}}]}}'
)

_TOKEN_LIKE = re.compile(r"\b[A-Za-z0-9_\-]{28,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|authorization|api[_-]?key|token|secret)\b\s*[:=]?\s*\S+")


def scrub(v: Any, limit: int = 240) -> str:
    """Text that may leave this process (prompt) or reach Telegram / a file: one line, no control characters, secret-looking tokens removed, shortened."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v if v is not None else ""))
    s = _BEARER.sub("[removed]", s)
    s = _TOKEN_LIKE.sub("[removed]", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ag")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - a missing or damaged state file is "nothing yet"
        return default


# ------------------------------------------------------------------------------------------------------------------ privacy guard
ROW_FIELDS = ("switch", "title", "tests", "status", "current", "kind", "test_age_days", "verdict", "test_suggests")
TOP_FIELDS = ("date", "switches", "auto_calibration")


def whitelist(inp: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the input from the allowed fields only, so a field added to build_input later can never reach the prompt without being listed here."""
    rows = []
    for r in (inp.get("switches") or [])[:MAX_SWITCH_ROWS]:
        rows.append({k: r[k] for k in ROW_FIELDS if k in r})
    out = {"date": inp.get("date"), "switches": rows}
    ac = inp.get("auto_calibration") or {}
    out["auto_calibration"] = {k: ac.get(k) for k in ("enabled", "step_pct", "cooldown_h") if k in ac}
    return out


def privacy_problems(prompt: str, secrets: Sequence[str] = ()) -> List[str]:
    """What must never leave this process, checked on the FINISHED prompt (system + user text). Empty list = clean."""
    bad: List[str] = []
    low = prompt
    for sec in secrets:
        sec = str(sec or "")
        if len(sec) >= 6 and sec in low:
            bad.append("a known secret value is in the prompt")
            break
    for rx in _KEY_SHAPES:
        if rx.search(low):
            bad.append("key-like text is in the prompt")
            break
    m = _BANNED_IN_PROMPT.search(low)
    if m:
        bad.append(f"the field name '{m.group(0).lower()}' is in the prompt")
    if len(low) > MAX_PROMPT_CHARS:
        bad.append("the prompt is larger than the limit")
    return bad


# ------------------------------------------------------------------------------------------------------------------ the allow-list
def allowed_rules(rules: Sequence[AC.Rule]) -> Dict[str, AC.Rule]:
    """The only switches an idea may name: auto-calibrated and never protected."""
    return {r.key: r for r in rules if r.key not in AC.PROTECTED}


def queueable(job: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not job:
        return False, "unknown test"
    if job.get("heavy") and (job.get("timeout_s", 0) > MAX_HEAVY_QUEUE_S or job.get("needs")):
        return False, "runs for more than an hour or needs a download first: start it yourself"
    return True, ""


# ------------------------------------------------------------------------------------------------------------------ input
def build_input(auto: Any, rules: Dict[str, AC.Rule], now: float, ctx_out: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Aggregates only, from what auto-calibration reads anyway. Read-only: nothing is evaluated for real and no vote is counted."""
    pinned = set(auto.settings().get("pinned") or [])
    flags: Dict[str, Any] = {}
    ev_cache: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    step = float(getattr(auto, "step_pct", 0.25))
    for key, rule in rules.items():
        if key in pinned:
            continue
        row: Dict[str, Any] = {"switch": key, "title": scrub(rule.title, 80), "tests": list(rule.tests)}
        if rule.service not in flags:
            try:
                flags[rule.service] = auto.control.flags(rule.service)
            except Exception:  # noqa: BLE001
                flags[rule.service] = None
        svc = flags[rule.service]
        frow = (svc or {}).get(key) if isinstance(svc, dict) else None
        if frow is None:
            row.update(status="service_unreachable" if svc is None else "not_in_catalog")
            rows.append(row)
            continue
        cur = frow.get("effective")
        row.update(current=scrub(cur, 40), kind=scrub(frow.get("kind"), 20))
        if ctx_out is not None:
            ctx_out[key] = {"value": None if cur is None else str(cur), "kind": str(frow.get("kind") or ""), "choices": [str(c) for c in (frow.get("choices") or [])][:12]}
        evs: Dict[str, Any] = {}
        for t in rule.tests:
            if t not in ev_cache:
                try:
                    ev_cache[t] = auto.evidence.get(t)
                except Exception:  # noqa: BLE001
                    ev_cache[t] = None
            if ev_cache[t] is not None:
                evs[t] = ev_cache[t]
        if len(evs) != len(rule.tests):
            row.update(status="no_evidence")
            rows.append(row)
            continue
        row["test_age_days"] = round(max(0.0, (now - min(e.finished_at for e in evs.values())) / 86400.0), 1)
        try:
            dec = rule.fn(evs, cur, {"step_pct": step})
            row.update(status="would_change" if dec.value is not None and dec.value != cur else "hold",
                       verdict=scrub(dec.reason, 220), test_suggests=scrub(dec.value, 40) if dec.value is not None else None)
        except Exception as e:  # noqa: BLE001
            row.update(status="unreadable", verdict=scrub(type(e).__name__, 40))
        rows.append(row)
    return {"date": datetime.fromtimestamp(now, IST).strftime("%Y-%m-%d"), "switches": rows,
            "auto_calibration": {"enabled": bool(auto.settings().get("enabled")), "step_pct": step, "cooldown_h": auto.settings().get("cooldown_h")}}


# ------------------------------------------------------------------------------------------------------------------ LLM
def _post_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 45.0) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", "User-Agent": UA, **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https endpoints above
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]
    except Exception as e:  # noqa: BLE001 - timeout, DNS, TLS: "no answer"
        return 0, type(e).__name__


class LLM:
    """Gemini first (key from GEMINI_API_KEY inside this container, which compose fills from the AGENT key), then Groq; both failing returns None.
    Round 536 health rules, all written to self.health (the one Telegram message and the saved run show them):
      * a retired model (404, or "no longer available") -> the backup model once, then Groq;
      * a 429 -> wait RETRY_429_S and ask the same model once more; a DAILY-quota 429 (or a second 429) -> Gemini is skipped (and for the rest of the day when the
        quota is the daily one: self.gemini_blocked), Groq answers; no loop;
      * an answer over MAX_ANSWER_CHARS is rejected;
      * a per-run call budget (the daily cap minus what usage.json already counts): at 0 no call is made.
    Calls are spaced min_gap_s apart (free tier: 10 requests a minute)."""

    def __init__(self, gemini_key: str = "", gemini_model: str = "", gemini_backup: str = "", groq_key: str = "", groq_model: str = "",
                 min_gap_s: float = 7.0, post: Callable[..., Tuple[int, str]] = _post_json, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, retry_429_s: float = RETRY_429_S):
        self.gk, self.gm, self.gb, self.qk, self.qm = gemini_key, gemini_model, gemini_backup, groq_key, groq_model
        self.gap, self.post, self.sleep, self.clock, self.retry_s = max(0.0, float(min_gap_s)), post, sleep, clock, max(0.0, float(retry_429_s))
        self._last = -1e9
        self.gemini_blocked = False
        self.budget: Optional[int] = None
        self.usage: Dict[str, Any] = {"gemini": 0, "groq": 0, "errors": []}
        self.health: List[str] = []

    def begin(self, budget: Optional[int] = None, gemini_blocked: bool = False) -> None:
        """Start of one run: reset the per-run counters; budget = calls left today (None = no cap)."""
        self.usage = {"gemini": 0, "groq": 0, "errors": []}
        self.health = []
        self.budget, self.gemini_blocked = budget, bool(gemini_blocked)

    def _wait(self) -> None:
        left = self.gap - (self.clock() - self._last)
        if left > 0:
            self.sleep(left)
        self._last = self.clock()

    def _err(self, who: str, status: int, text: str) -> None:
        if len(self.usage["errors"]) < 6:
            self.usage["errors"].append(f"{who}: HTTP {status} {scrub(text, 120)}".strip())

    def _note(self, text: str) -> None:
        if text not in self.health and len(self.health) < 8:
            self.health.append(text)

    def _can_call(self) -> bool:
        if self.budget is not None and self.budget <= 0:
            self._note("the daily LLM call cap is reached: no more calls today")
            return False
        return True

    def _spend(self) -> None:
        if self.budget is not None:
            self.budget -= 1

    @staticmethod
    def _retired(st: int, txt: str) -> bool:
        t = (txt or "").lower()
        return st == 404 or (st in (400, 403) and ("no longer available" in t or "is not found" in t or "has been retired" in t or "deprecated" in t))

    @staticmethod
    def _daily(txt: str) -> bool:
        t = (txt or "").lower()
        return "perday" in t.replace(" ", "").replace("_", "") or "per day" in t or "daily" in t

    def _gemini(self, model: str, system: str, user: str) -> Tuple[Optional[str], int, str]:
        if not self._can_call():
            return None, -1, ""
        self._wait()
        self._spend()
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048, "responseMimeType": "application/json"}}
        st, txt = self.post(GEMINI_URL.format(model=model), {"x-goog-api-key": self.gk}, body)
        self.usage["gemini"] += 1
        if st != 200:
            self._err(f"gemini {model}", st, txt)
            return None, st, txt
        try:
            parts = json.loads(txt)["candidates"][0]["content"]["parts"]
            out = "".join(str(p.get("text", "")) for p in parts).strip()
        except Exception:  # noqa: BLE001
            self._err(f"gemini {model}", st, "unreadable answer")
            return None, st, txt
        if len(out) > MAX_ANSWER_CHARS:
            self._err(f"gemini {model}", st, f"answer too long ({len(out)} characters), rejected")
            self._note(f"Gemini answer was over {MAX_ANSWER_CHARS} characters and was rejected")
            return None, st, txt
        return (out or None), st, txt

    def _groq(self, system: str, user: str) -> Optional[str]:
        body = {"model": self.qm, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0.2, "max_tokens": 2048, "response_format": {"type": "json_object"}}
        for attempt in (0, 1):
            if not self._can_call():
                return None
            self._wait()
            self._spend()
            st, txt = self.post(GROQ_URL, {"Authorization": f"Bearer {self.qk}"}, body)
            self.usage["groq"] += 1
            if st == 400 and attempt == 0 and "response_format" in txt:
                body.pop("response_format", None)        # some models refuse JSON mode: ask again without it, the validator parses fences anyway
                continue
            if st == 429:
                self._note("Groq rate limit (429)")
            if st != 200:
                self._err(f"groq {self.qm}", st, txt)
                return None
            try:
                out = (json.loads(txt)["choices"][0]["message"]["content"] or "").strip()
            except Exception:  # noqa: BLE001
                self._err(f"groq {self.qm}", st, "unreadable answer")
                return None
            if len(out) > MAX_ANSWER_CHARS:
                self._err(f"groq {self.qm}", st, f"answer too long ({len(out)} characters), rejected")
                self._note(f"Groq answer was over {MAX_ANSWER_CHARS} characters and was rejected")
                return None
            return out or None
        return None

    def _gemini_with_health(self, model: str, system: str, user: str) -> Tuple[Optional[str], int]:
        out, st, txt = self._gemini(model, system, user)
        if out is None and st == 429:
            if self._daily(txt):
                self.gemini_blocked = True
                self._note("Gemini daily quota is used up: Gemini is skipped for the rest of the day")
                return None, st
            self._note(f"Gemini rate limit (429): waited {RETRY_429_S:g} s and asked once more")
            self.sleep(self.retry_s)
            out, st, txt = self._gemini(model, system, user)
            if out is None and st == 429:
                self._note("Gemini rate limit (429) again: switched to Groq")
                if self._daily(txt):
                    self.gemini_blocked = True
        return out, st

    def ask(self, system: str, user: str) -> Tuple[Optional[str], str]:
        if self.gk and self.gm and not self.gemini_blocked:
            out, st = self._gemini_with_health(self.gm, system, user)
            if out is None and self._retired(st, " ".join(self.usage["errors"][-1:])) and self.gb and self.gb != self.gm:
                self._note(f"model {self.gm} looks retired (HTTP {st}): tried the backup {self.gb}")
                out, st = self._gemini_with_health(self.gb, system, user)
                if out is None and self._retired(st, " ".join(self.usage["errors"][-1:])):
                    self._note(f"backup model {self.gb} is retired too (HTTP {st}): switched to Groq")
            elif out is None and self._retired(st, " ".join(self.usage["errors"][-1:])):
                self._note(f"model {self.gm} looks retired (HTTP {st}) and no backup is set: switched to Groq")
            if out:
                return out, "gemini"
            if st not in (404, 429, -1) and not self._retired(st, " ".join(self.usage["errors"][-1:])):
                self._note(f"Gemini failed (HTTP {st}): switched to Groq")
        elif self.gk and self.gm and self.gemini_blocked:
            self._note("Gemini is skipped today (daily quota used up earlier)")
        if self.qk and self.qm:
            out = self._groq(system, user)
            if out:
                if self.gk and self.gm:
                    self._note("Groq answered instead of Gemini")
                return out, "groq"
        return None, ""


# ------------------------------------------------------------------------------------------------------------------ answer -> ideas
def _close_json(prefix: str) -> Optional[str]:
    """Close the brackets of a cut-off JSON text; None when the cut is inside a string."""
    stack: List[str] = []
    ins = esc = False
    for ch in prefix:
        if ins:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                ins = False
            continue
        if ch == '"':
            ins = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if ins:
        return None
    return prefix + "".join(reversed(stack))


def parse_json(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fences stripped, text around the object ignored, trailing commas removed, a cut-off answer closed at its last complete value. Never raises."""
    if not text or len(text) > MAX_ANSWER_CHARS:
        return None
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    i = s.find("{")
    if i < 0:
        return None
    body = s[i:]
    j = body.rfind("}")
    cands = [body[:j + 1]] if j > 0 else []
    cands.append(body)
    for c in cands:
        for t in (c, re.sub(r",\s*([}\]])", r"\1", c)):
            try:
                d = json.loads(t)
                return d if isinstance(d, dict) else None
            except ValueError:
                pass
    tries = 0
    for m in reversed(list(re.finditer(r"[}\]]", body))):
        tries += 1
        if tries > 40:
            break
        closed = _close_json(re.sub(r",\s*$", "", body[:m.end()]))
        if closed is None:
            continue
        try:
            d = json.loads(re.sub(r",\s*([}\]])", r"\1", closed))
            return d if isinstance(d, dict) else None
        except ValueError:
            continue
    return None


def validate(doc: Optional[Dict[str, Any]], allowed: Dict[str, AC.Rule], pinned: Sequence[str] = (), max_ideas: int = 5) -> Dict[str, Any]:
    """Everything the LLM said is untrusted. Kept: a switch from the allow-list, direction up or down, a short reason. Dropped (and counted with the reason): the rest."""
    out: Dict[str, Any] = {"weaknesses": [], "ideas": [], "dropped": []}
    if not isinstance(doc, dict):
        out["dropped"].append({"switch": "", "reason": "answer was not valid JSON"})
        return out
    ws = doc.get("weaknesses")
    if isinstance(ws, list):
        out["weaknesses"] = [scrub(w, 160) for w in ws[:5] if isinstance(w, str) and w.strip()]
    ideas = doc.get("ideas")
    if not isinstance(ideas, list):
        out["dropped"].append({"switch": "", "reason": "no ideas list"})
        return out
    seen = set()
    for raw in ideas:
        if not isinstance(raw, dict):
            out["dropped"].append({"switch": "", "reason": "idea is not an object"})
            continue
        sw = str(raw.get("switch", "")).strip()
        d = str(raw.get("direction", "")).strip().lower()
        if sw not in allowed:
            out["dropped"].append({"switch": scrub(sw, 60), "reason": "not in the allow-list"})
        elif d not in ("up", "down"):
            out["dropped"].append({"switch": sw, "reason": "direction is not up or down"})
        elif sw in pinned:
            out["dropped"].append({"switch": sw, "reason": "pinned by you"})
        elif sw in seen:
            out["dropped"].append({"switch": sw, "reason": "duplicate"})
        elif len(out["ideas"]) >= max_ideas:
            out["dropped"].append({"switch": sw, "reason": "over the nightly cap"})
        else:
            seen.add(sw)
            out["ideas"].append({"switch": sw, "direction": d, "steps": 1, "why": scrub(raw.get("why", ""), 200)})
    return out


# ------------------------------------------------------------------------------------------------------------------ the agent
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


def direction_of(old: Any, new: Any, kind: str = "", choices: Sequence[str] = ()) -> Optional[str]:
    """Which way a value moved: up / down, or None when it did not move or cannot be compared (a bool counts true as up, an enum by its choice order)."""
    if old is None or new is None or str(old) == str(new):
        return None
    o, n = str(old).strip().lower(), str(new).strip().lower()
    if o in ("true", "false") and n in ("true", "false"):
        return "up" if n == "true" else "down"
    try:
        return "up" if float(n) > float(o) else "down"
    except ValueError:
        pass
    ch = [str(c).lower() for c in choices]
    if o in ch and n in ch:
        return "up" if ch.index(n) > ch.index(o) else "down"
    return None


# ------------------------------------------------------------------------------------------------------------------ round 541: settings, banner, breakdown, idea check
def parse_hhmm(v: Any) -> Optional[int]:
    try:
        hh, mm = [int(x) for x in str(v).strip().split(":")]
    except Exception:  # noqa: BLE001
        return None
    return hh * 60 + mm if 0 <= hh < 24 and 0 <= mm < 60 else None


def fmt_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _whole(v: Any, name: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"{name} must be a whole number")
    try:
        f = float(v)
        n = int(f)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a whole number")
    if n != f:
        raise ValueError(f"{name} must be a whole number")
    if n < lo or n > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return n


def clean_setting(key: str, v: Any) -> Any:
    """One agent setting, checked against its bounds (ValueError with a plain sentence otherwise)."""
    if key == "mode":
        m = str(v).strip().lower()
        if m not in MODES:
            raise ValueError("mode must be off, report or propose")
        return m
    if key == "run_after":
        m = parse_hhmm(v)
        if m is None or not RUN_AFTER_MIN <= m <= RUN_AFTER_MAX:
            raise ValueError(f"run_after must be HH:MM IST between {fmt_hhmm(RUN_AFTER_MIN)} and {fmt_hhmm(RUN_AFTER_MAX)}")
        return fmt_hhmm(m)
    if key == "max_ideas":
        return _whole(v, "max_ideas", *IDEAS_BOUNDS)
    if key == "max_queue":
        return _whole(v, "max_queue", *QUEUE_BOUNDS)
    raise ValueError(f"unknown setting: {key}")


def banner(settings: Dict[str, Any], allowed: Sequence[str]) -> Dict[str, Any]:
    """The one line that says whether auto-calibration can change anything: OFF / DRY-RUN / LIVE, and which allowed switches it may touch."""
    pinned = set(settings.get("pinned") or [])
    free = sorted(k for k in allowed if k not in pinned)
    if not settings.get("enabled"):
        return {"state": "off", "text": "Auto-calibration is OFF: nothing is applied.", "unpinned": free}
    if settings.get("dry_run"):
        return {"state": "dry_run", "text": f"DRY-RUN: auto-calibration only reports, nothing is applied ({len(free)} switch(es) unpinned).", "unpinned": free}
    return {"state": "live", "text": f"LIVE: auto-calibration applies changes to {len(free)} unpinned switch(es) after a finished test.", "unpinned": free}


def breakdown(rows: Sequence[Dict[str, Any]], allowed: Sequence[str], pinned: Sequence[str]) -> Dict[str, Any]:
    """Counts behind \"why no ideas\": of the switches the agent may name, how many are pinned (hidden from it), have no recent test result, are on hold or would change."""
    pin = set(pinned)
    c = {"no_evidence": 0, "hold": 0, "would_change": 0, "unreachable": 0, "unreadable": 0}
    for r in rows:
        st = r.get("status")
        if st in ("service_unreachable", "not_in_catalog"):
            c["unreachable"] += 1
        elif st in ("no_evidence", "hold", "would_change"):
            c[st] += 1
        else:
            c["unreadable"] += 1
    return {"allowed": len(allowed), "pinned": sum(1 for k in allowed if k in pin), "seen": len(rows), **c, "asked": False, "ideas": 0, "dropped": 0, "summary": ""}


def why_summary(bd: Dict[str, Any], note: str = "") -> str:
    """One plain sentence for the breakdown, written once the run is finished."""
    parts = []
    for k, txt in (("no_evidence", "without a recent test result"), ("hold", "on hold (their test says no change)"), ("would_change", "where the test would change the switch"),
                   ("unreachable", "not reachable or not listed"), ("unreadable", "unreadable")):
        if bd.get(k):
            parts.append(f"{bd[k]} {txt}")
    s = f"{bd['allowed']} switches the agent may name: {bd['pinned']} pinned (hidden from it), {bd['seen']} shown" + (" - " + ", ".join(parts) if parts else "")
    usable = bd.get("hold", 0) + bd.get("would_change", 0)
    if bd.get("ideas"):
        return s + f". {bd['ideas']} idea(s) kept" + (f", {bd['dropped']} dropped by the rules" if bd.get("dropped") else "") + "."
    if bd["seen"] == 0:
        return s + ". No ideas: every switch the agent may name is pinned."
    if usable == 0:
        return s + ". No ideas: none of the shown switches has a usable test result, so nothing was asked."
    if not bd.get("asked"):
        return s + ". No ideas: " + (note or "the model was not asked") + "."
    if bd.get("dropped"):
        return s + f". No ideas kept: {bd['dropped']} answer(s) were dropped by the rules (see `dropped`)."
    return s + ". No ideas: the model named no switch."


def _at_end(cur: Any, kind: str, choices: Sequence[str], direction: str) -> Optional[str]:
    """A move that cannot happen: an off switch cannot go down, an on switch cannot go up, the first / last choice of a list cannot go further."""
    c = str(cur).strip().lower() if cur is not None else ""
    if c in ("true", "false"):
        if direction == "down" and c == "false":
            return "already off (lowest value)"
        if direction == "up" and c == "true":
            return "already on (highest value)"
        return None
    ch = [str(x).lower() for x in choices]
    if c in ch:
        if direction == "down" and ch.index(c) == 0:
            return f"already at the lowest choice ({c})"
        if direction == "up" and ch.index(c) == len(ch) - 1:
            return f"already at the highest choice ({c})"
    return None


def check_ideas(ideas: Sequence[Dict[str, Any]], rows: Sequence[Dict[str, Any]], ctx: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Round 541 idea check, on plain data (no text matching): drop an idea whose switch has no usable test result, whose move cannot happen, or that points the
    opposite way to the switch's own rule. A kept idea is labelled `rule_agrees` (the rule would move it the same way) or `rule_holds` (the rule says no change:
    the idea has no support from its test yet) and carries the rule's own line in `rule_says`."""
    by = {str(r.get("switch")): r for r in rows}
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for i in ideas:
        sw, d = i["switch"], i["direction"]
        row = by.get(sw) or {}
        st = row.get("status")
        if st not in ("hold", "would_change"):
            dropped.append({"switch": sw, "reason": "no usable test result for this switch (" + str(st or "not shown to the model") + ")"})
            continue
        c = ctx.get(sw, {})
        end = _at_end(c.get("value"), c.get("kind", ""), c.get("choices", []), d)
        if end:
            dropped.append({"switch": sw, "reason": f"the move is impossible: {end}"})
            continue
        support = "rule_holds"
        if st == "would_change":
            rd = direction_of(c.get("value"), row.get("test_suggests"), c.get("kind", ""), c.get("choices", []))
            if rd is not None and rd != d:
                dropped.append({"switch": sw, "reason": f"opposite to the switch's own rule, which would move it {rd}"})
                continue
            support = "rule_agrees"
        kept.append({**i, "support": support, "rule_says": scrub(row.get("verdict"), 160)})
    return kept, dropped


class Agent:
    def __init__(self, data_dir: str, auto: Any, runner: Any, jobs: Dict[str, Dict[str, Any]], llm: LLM, notify: Optional[Callable[[str, str, str], Optional[str]]] = None,
                 mode: str = "report", max_ideas: int = 5, max_queue: int = 3, min_rerun_h: float = 20.0, run_after: str = "16:10",
                 now_fn: Callable[[], float] = time.time, rules: Optional[Sequence[AC.Rule]] = None,
                 max_calls_day: int = 40, secrets: Optional[Callable[[], Sequence[str]]] = None, market_open: Optional[Callable[[], bool]] = None):
        self.dir, self.auto, self.runner, self.jobs, self.llm, self.notify = data_dir, auto, runner, jobs, llm, notify
        self.mode = mode if mode in MODES else "report"
        self.max_ideas, self.max_queue, self.min_rerun_s = max(1, int(max_ideas)), max(0, int(max_queue)), max(0.0, float(min_rerun_h)) * 3600
        self.max_calls_day = max(0, int(max_calls_day))
        self.secrets = secrets or (lambda: ())
        self.market_open = market_open or (lambda: False)
        self.now = now_fn
        self.rules = allowed_rules(rules if rules is not None else AC.RULES)
        try:
            hh, mm = [int(x) for x in str(run_after).split(":")]
            self.after_min = max(0, min(23 * 60 + 59, hh * 60 + mm))
        except Exception:  # noqa: BLE001
            self.after_min = 16 * 60 + 10
        self.lock = threading.Lock()          # one run at a time
        self.slock = threading.RLock()        # read-modify-write of the small state files
        self.stop = threading.Event()
        self.state_path = os.path.join(data_dir, "state", "agent.json")
        self.trials_path = os.path.join(data_dir, "agent", "trials.json")
        self.usage_path = os.path.join(data_dir, "agent", "usage.json")
        self.audit_path = os.path.join(data_dir, "agent", "audit.jsonl")
        self.env_defaults = {"mode": self.mode, "run_after": fmt_hhmm(self.after_min), "max_ideas": self.max_ideas, "max_queue": self.max_queue}
        self.saved: Dict[str, Any] = {}
        self._load_saved()

    # -- round 541: settings kept on disk (the saved value wins over the .env default; reset goes back to the .env value)
    def current_settings(self) -> Dict[str, Any]:
        return {"mode": self.mode, "run_after": fmt_hhmm(self.after_min), "max_ideas": self.max_ideas, "max_queue": self.max_queue}

    def _apply(self, vals: Dict[str, Any]) -> None:
        if "mode" in vals:
            self.mode = vals["mode"]
        if "run_after" in vals:
            self.after_min = parse_hhmm(vals["run_after"]) or self.after_min
        if "max_ideas" in vals:
            self.max_ideas = int(vals["max_ideas"])
        if "max_queue" in vals:
            self.max_queue = int(vals["max_queue"])

    def _load_saved(self) -> None:
        raw = self._state().get("settings")
        self.saved = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    self.saved[k] = clean_setting(k, v)
                except ValueError:
                    pass        # a damaged or out-of-bounds saved value is ignored: the .env default stays
        self._apply(self.saved)

    def configure(self, changes: Dict[str, Any], by: str = "", confirm: bool = False, reset: bool = False) -> Dict[str, Any]:
        """Change settings (None = leave alone). Every value is bounded; propose needs confirm=True; a change is one audit line; reset drops the saved values first."""
        with self.slock:
            clean = {k: clean_setting(k, v) for k, v in (changes or {}).items() if v is not None}
            if clean.get("mode") == "propose" and self.mode != "propose" and not confirm:
                raise ValueError("switching to propose needs confirm=true (the second click): it lets the agent queue the existing tests behind its ideas")
            before = self.current_settings()
            saved = {} if reset else dict(self.saved)
            saved.update(clean)
            self._apply(self.env_defaults)
            self._apply(saved)
            st = self._state()
            if saved:
                st["settings"] = saved
            else:
                st.pop("settings", None)
            self._put_state(st)
            self.saved = saved
            after = self.current_settings()
            diff = {k: {"from": before[k], "to": after[k]} for k in after if before[k] != after[k]}
            if diff or reset:
                self._audit(by, diff, reset)
            return self.settings()

    def _audit(self, by: str, diff: Dict[str, Any], reset: bool) -> None:
        line = {"at": self.now(), "at_ist": datetime.fromtimestamp(self.now(), IST).strftime("%Y-%m-%d %H:%M:%S"), "by": scrub(by or "-", 60), "reset": bool(reset), "changes": diff}
        try:
            lines = []
            try:
                with open(self.audit_path, "r", encoding="utf-8") as f:
                    lines = [x for x in f.read().splitlines() if x.strip()]
            except FileNotFoundError:
                pass
            lines = (lines + [json.dumps(line, sort_keys=True)])[-AUDIT_KEEP:]
            _atomic_write(self.audit_path, "\n".join(lines) + "\n")
        except Exception:  # noqa: BLE001 - the audit file is best effort; the change itself is already saved
            pass

    def audit_tail(self, n: int = 10) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with open(self.audit_path, "r", encoding="utf-8") as f:
                for x in f.read().splitlines()[-n:]:
                    try:
                        out.append(json.loads(x))
                    except ValueError:
                        pass
        except OSError:
            pass
        return list(reversed(out))

    def settings(self) -> Dict[str, Any]:
        return {"values": self.current_settings(), "env_defaults": dict(self.env_defaults), "saved": dict(self.saved),
                "source": {k: ("saved" if k in self.saved else "env") for k in self.env_defaults},
                "bounds": {"modes": list(MODES), "run_after": [fmt_hhmm(RUN_AFTER_MIN), fmt_hhmm(RUN_AFTER_MAX)], "max_ideas": list(IDEAS_BOUNDS), "max_queue": list(QUEUE_BOUNDS)},
                "audit": self.audit_tail(10)}

    # -- small state helpers
    def _state(self) -> Dict[str, Any]:
        st = _read_json(self.state_path, {})
        return st if isinstance(st, dict) else {}

    def _put_state(self, st: Dict[str, Any]) -> None:
        _atomic_write(self.state_path, json.dumps(st, indent=1, sort_keys=True, default=str))

    def _trials(self) -> Dict[str, Any]:
        tr = _read_json(self.trials_path, {})
        if not isinstance(tr, dict):
            tr = {}
        if not isinstance(tr.get("log"), list):
            tr["log"] = []
        return tr

    def _usage(self, ts: Optional[float] = None) -> Dict[str, Any]:
        day = _day(ts if ts is not None else self.now())
        u = _read_json(self.usage_path, {})
        if not isinstance(u, dict) or u.get("date") != day:
            u = {"date": day, "gemini": 0, "groq": 0, "runs": 0, "gemini_blocked": False}
        return u

    def eff_mode(self) -> str:
        return "off" if self._state().get("stop") else self.mode

    # -- timetable
    def due(self, now: Optional[float] = None) -> bool:
        t = datetime.fromtimestamp(now if now is not None else self.now(), IST)
        mins = t.hour * 60 + t.minute
        last = self._state().get("last_auto_date")
        return self.eff_mode() != "off" and t.weekday() < 5 and self.after_min <= mins <= 23 * 60 + 30 and last != t.strftime("%Y-%m-%d")

    def next_run(self, now: Optional[float] = None) -> Dict[str, Any]:
        """When the automatic run happens next (IST): today after the run-after time if it has not run today, else the next weekday. Read only."""
        ts = now if now is not None else self.now()
        t = datetime.fromtimestamp(ts, IST)
        if self.eff_mode() == "off":
            return {"state": "off", "at_ist": None, "reason": "the agent is off (mode off or it switched itself off): no automatic run"}
        today = t.strftime("%Y-%m-%d")
        mins = t.hour * 60 + t.minute
        if t.weekday() < 5 and self._state().get("last_auto_date") != today and mins <= 23 * 60 + 30:
            if mins >= self.after_min:
                return {"state": "due_now", "at_ist": t.strftime("%Y-%m-%d %H:%M"), "reason": "the run-after time has passed and it has not run today: it starts within a minute"}
            d = t
        else:
            d = t + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
        at = d.replace(hour=self.after_min // 60, minute=self.after_min % 60, second=0, microsecond=0)
        return {"state": "scheduled", "at_ist": at.strftime("%Y-%m-%d %H:%M"), "reason": ""}

    def start(self) -> None:
        threading.Thread(target=self._loop, name="agent", daemon=True).start()

    def _loop(self) -> None:
        while not self.stop.wait(45):
            try:
                if self.due():
                    with self.slock:
                        st = self._state()
                        st["last_auto_date"] = _day(self.now())      # set first: a failed run is not retried in a loop
                        self._put_state(st)
                    self.run(trigger="schedule")
            except Exception:  # noqa: BLE001 - the agent must never take the runner down
                pass

    # -- one run
    def run(self, mode: Optional[str] = None, trigger: str = "manual", by: str = "") -> Dict[str, Any]:
        stopped = self._state().get("stop")
        if stopped:
            return {"ran": False, "reason": "the agent switched itself off (" + scrub(stopped.get("reason", "stop rule"), 120) + "): press Resume first", "stopped": stopped.get("reason")}
        mode = mode if mode in MODES else self.mode
        if mode == "off":
            return {"ran": False, "reason": "AGENT_MODE is off"}
        if trigger == "manual" and self.market_open():
            return {"ran": False, "market_open": True, "reason": "refused: the market is open (Mon-Fri 09:00-15:45 IST). Run it after 15:45; nothing was asked."}
        if not self.lock.acquire(blocking=False):
            return {"ran": False, "reason": "the agent is already running"}
        try:
            return self._run(mode, trigger, by)
        finally:
            self.lock.release()

    def resume(self) -> Dict[str, Any]:
        """Back on after a self-off. The stop rule counts again from this moment (earlier ideas stay in the log but no longer count). Sets no trading switch."""
        with self.slock:
            st = self._state()
            was = st.pop("stop", None)
            st["resume_at"] = self.now()
            self._put_state(st)
        return {"resumed": bool(was), "mode": self.mode}

    def _run(self, mode: str, trigger: str, by: str) -> Dict[str, Any]:
        ts = self.now()
        day = _day(ts)
        acs = self.auto.settings()
        rep: Dict[str, Any] = {"ran": True, "date": day, "at": ts, "mode": mode, "trigger": trigger, "by": by, "auto_calibration": banner(acs, list(self.rules))["text"],
                               "breakdown": {}, "source": "", "ideas": [], "dropped": [], "weaknesses": [], "queued": [], "skipped": [], "errors": [], "health": []}
        try:
            self._resolve(ts)
        except Exception:  # noqa: BLE001 - judging old ideas must never stop tonight's run
            pass
        ctx: Dict[str, Dict[str, Any]] = {}
        inp = whitelist(build_input(self.auto, self.rules, ts, ctx))
        usable = [r for r in inp["switches"] if r.get("status") in ("hold", "would_change")]
        rep["switches_seen"], rep["switches_with_evidence"] = len(inp["switches"]), len(usable)
        bd = breakdown(inp["switches"], list(self.rules), acs.get("pinned") or [])
        rep["breakdown"] = bd           # same dict object: filled in below, but its place near the top of the report is fixed here
        usage = self._usage(ts)
        if not usable:
            rep["note"] = "no auto-calibrated switch has a usable test result yet, so nothing was asked"
        else:
            user = json.dumps(inp, sort_keys=True, separators=(",", ":"))
            system = SYSTEM_PROMPT.format(n=self.max_ideas)
            bad = privacy_problems(system + "\n" + user, list(self.secrets()))
            left = max(0, self.max_calls_day - int(usage.get("gemini", 0)) - int(usage.get("groq", 0)))
            if bad:
                rep["note"] = "privacy guard: the prompt was not sent (" + "; ".join(bad) + ")"
                rep["health"].append("privacy guard blocked the run: " + "; ".join(bad))
            elif left <= 0:
                rep["note"] = f"daily LLM call cap reached ({self.max_calls_day}); nothing was asked"
                rep["health"].append(rep["note"])
            else:
                pinned = self.auto.settings().get("pinned") or []
                self.llm.begin(left, bool(usage.get("gemini_blocked")))
                text, src = self.llm.ask(system, user)
                rep["source"], rep["llm"] = src, {"calls": {"gemini": self.llm.usage["gemini"], "groq": self.llm.usage["groq"]}}
                rep["errors"] = list(self.llm.usage["errors"])
                rep["health"] = list(self.llm.health)
                usage["gemini"] = int(usage.get("gemini", 0)) + self.llm.usage["gemini"]
                usage["groq"] = int(usage.get("groq", 0)) + self.llm.usage["groq"]
                usage["gemini_blocked"] = bool(usage.get("gemini_blocked") or self.llm.gemini_blocked)
                usage["runs"] = int(usage.get("runs", 0)) + 1
                try:
                    _atomic_write(self.usage_path, json.dumps(usage, indent=1, sort_keys=True))
                except Exception:  # noqa: BLE001
                    pass
                if text is None:
                    rep["note"] = "no LLM answered (free quota used up or no key); nothing changed"
                else:
                    v = validate(parse_json(text), self.rules, pinned, self.max_ideas)
                    kept, checked = check_ideas(v["ideas"], inp["switches"], ctx)
                    rep["ideas"], rep["dropped"], rep["weaknesses"] = kept, v["dropped"] + checked, v["weaknesses"]
                    off = sum(1 for d in rep["dropped"] if d.get("reason") == "not in the allow-list")
                    if off:
                        w = self._offlist(ts, off)
                        if w:
                            rep["health"].append(w)
                    if mode == "propose" and rep["ideas"]:
                        rep["queued"], rep["skipped"] = self._queue(rep["ideas"], ts)
        bd.update(asked="llm" in rep, ideas=len(rep["ideas"]), dropped=len(rep["dropped"]))
        bd["summary"] = why_summary(bd, rep.get("note", ""))
        self._save(rep, day, ctx)
        stop_msg = self._check_stop(ts)
        if stop_msg:
            rep["stopped"] = stop_msg
        self._message(rep, stop_msg)
        return rep

    # -- the allow-list warning: 3 answers outside the allow-list in 7 days
    def _offlist(self, ts: float, n: int) -> Optional[str]:
        with self.slock:
            st = self._state()
            hits = [float(x) for x in st.get("offlist", []) if ts - float(x) < OFFLIST_WINDOW_D * 86400] + [ts] * n
            st["offlist"] = hits[-40:]
            msg = None
            if len(hits) >= OFFLIST_WARN_N and ts - float(st.get("offlist_warned_at", 0)) >= OFFLIST_WINDOW_D * 86400:
                st["offlist_warned_at"] = ts
                msg = f"{len(hits)} LLM answers outside the allow-list in {OFFLIST_WINDOW_D} days (all discarded): the model may be drifting"
            self._put_state(st)
            return msg

    def _queue(self, ideas: List[Dict[str, Any]], ts: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Queue the EXISTING test behind each idea (one job once, however many ideas share it). The runner's own rules still apply: market-hours guard, heavy cap,
        memory guard, one identical job at a time. Auto-calibration reads the finished result like any other."""
        queued: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        done: set = set()
        for idea in ideas:
            rule = self.rules[idea["switch"]]
            for t in rule.tests:
                if t in done:
                    continue
                done.add(t)
                ok, why = queueable(self.jobs.get(t))
                if not ok:
                    skipped.append({"test": t, "switch": idea["switch"], "reason": why})
                    continue
                ev = None
                try:
                    ev = self.auto.evidence.get(t)
                except Exception:  # noqa: BLE001
                    pass
                if ev is not None and ts - ev.finished_at < self.min_rerun_s:
                    skipped.append({"test": t, "switch": idea["switch"], "reason": f"its result is only {round((ts - ev.finished_at) / 3600, 1)} h old"})
                    continue
                if len(queued) >= self.max_queue:
                    skipped.append({"test": t, "switch": idea["switch"], "reason": "over the nightly queue cap"})
                    continue
                try:
                    r = self.runner.submit("job", t, {}, by="agent", trigger="manual")
                    st = (r.d.get("status") if hasattr(r, "d") else None) or "queued"
                    if st == "blocked":
                        skipped.append({"test": t, "switch": idea["switch"], "reason": scrub(r.d.get("note", "blocked"), 120)})
                    else:
                        queued.append({"test": t, "switch": idea["switch"], "run": getattr(r, "id", "") or r.d.get("id", "")})
                except Exception as e:  # noqa: BLE001 - already waiting, queue full, unknown job
                    skipped.append({"test": t, "switch": idea["switch"], "reason": scrub(e, 120)})
        return queued, skipped

    # -- files
    def _save(self, rep: Dict[str, Any], day: str, ctx: Dict[str, Dict[str, Any]]) -> None:
        try:
            _atomic_write(os.path.join(self.dir, "agent", f"{day}.json"), json.dumps(rep, indent=1, sort_keys=True, default=str))
            with self.slock:
                tr = self._trials()
                tr["ideas"] = int(tr.get("ideas", 0)) + len(rep["ideas"])
                tr["dropped"] = int(tr.get("dropped", 0)) + len(rep["dropped"])
                tr["runs"] = int(tr.get("runs", 0)) + 1
                qd = {q["switch"]: q["run"] for q in rep["queued"]}
                for i in rep["ideas"]:
                    c = ctx.get(i["switch"], {})
                    tr["log"].append({"date": day, "at": rep["at"], "switch": i["switch"], "direction": i["direction"], "mode": rep["mode"], "queued_run": qd.get(i["switch"]),
                                      "value": c.get("value"), "kind": c.get("kind", ""), "choices": c.get("choices", []), "support": i.get("support"), "verdict": None, "verdict_at": None})
                tr["log"] = tr["log"][-TRIALS_KEEP:]
                _atomic_write(self.trials_path, json.dumps(tr, indent=1, sort_keys=True))
                st = self._state()
                st["last"] = {k: rep.get(k) for k in ("date", "at", "mode", "trigger", "source", "note")}
                st["last"].update(ideas=len(rep["ideas"]), dropped=len(rep["dropped"]), queued=len(rep["queued"]))
                st["last_report"] = {k: rep.get(k) for k in ("date", "at", "mode", "trigger", "source", "note", "auto_calibration", "breakdown", "weaknesses", "ideas", "queued", "skipped", "health")}
                st["last_report"]["dropped"] = len(rep["dropped"])
                self._put_state(st)
        except Exception:  # noqa: BLE001 - files are best effort
            pass

    # -- the scoreboard: each idea is judged by what its own test later says
    def _resolve(self, ts: float) -> int:
        """For every idea without a verdict: once ALL tests behind its switch have a result newer than the idea, run the switch's rule on that result with the value the
        switch had WHEN THE IDEA WAS MADE (auto-calibration may have moved it since) and compare the direction. Nothing is evaluated for real and no vote is counted."""
        n = 0
        with self.slock:
            tr = self._trials()
            for e in tr["log"]:
                if e.get("verdict"):
                    continue
                rule = self.rules.get(e.get("switch"))
                if rule is None:
                    e["verdict"], e["verdict_at"] = "unclear", ts
                    n += 1
                    continue
                evs: Dict[str, Any] = {}
                for t in rule.tests:
                    try:
                        x = self.auto.evidence.get(t)
                    except Exception:  # noqa: BLE001
                        x = None
                    if x is not None and x.finished_at > float(e.get("at", 0)):
                        evs[t] = x
                if len(evs) != len(rule.tests):
                    if ts - float(e.get("at", 0)) > RESOLVE_AFTER_D * 86400:
                        e["verdict"], e["verdict_at"] = "no_result", ts
                        n += 1
                    continue
                try:
                    dec = rule.fn(evs, e.get("value"), {"step_pct": float(getattr(self.auto, "step_pct", 0.25))})
                except Exception:  # noqa: BLE001
                    e["verdict"], e["verdict_at"] = "unclear", ts
                    n += 1
                    continue
                if dec.value is None or str(dec.value) == str(e.get("value")):
                    v = "not_confirmed"
                else:
                    d = direction_of(e.get("value"), dec.value, e.get("kind", ""), e.get("choices", []))
                    v = "unclear" if d is None else ("survived" if d == e.get("direction") else "contradicted")
                e["verdict"], e["verdict_at"] = v, ts
                n += 1
            if n:
                _atomic_write(self.trials_path, json.dumps(tr, indent=1, sort_keys=True))
        return n

    def scoreboard(self) -> Dict[str, Any]:
        st = self._state()
        since = float(st.get("resume_at") or 0)
        log = [e for e in self._trials()["log"] if float(e.get("at", 0)) >= since]
        c = {k: sum(1 for e in log if e.get("verdict") == k) for k in ("survived", "not_confirmed", "contradicted", "unclear", "no_result")}
        tested = c["survived"] + c["not_confirmed"] + c["contradicted"]
        return {"ideas": len(log), **c, "tested": tested, "pending": sum(1 for e in log if not e.get("verdict")),
                "survival_pct": round(100.0 * c["survived"] / tested, 1) if tested else None, "dropped": int(self._trials().get("dropped", 0)),
                "since_resume": since > 0, "rule": STOP_RULE}

    def check_stop(self, ts: Optional[float] = None) -> Optional[str]:
        """Apply the stop rule: 50 ideas, 20 tested, under 5% survived -> the agent switches itself off (kept in state/agent.json, so a restart does not undo it)."""
        ts = self.now() if ts is None else ts
        st = self._state()
        if st.get("stop"):
            return None
        sb = self.scoreboard()
        if sb["ideas"] >= STOP_MIN_IDEAS and sb["tested"] >= STOP_MIN_TESTED and sb["survival_pct"] is not None and sb["survival_pct"] < STOP_BELOW_PCT:
            reason = f"{sb['survived']} of {sb['tested']} tested ideas survived their test ({sb['survival_pct']}%), {sb['ideas']} ideas in all"
            with self.slock:
                st = self._state()
                st["stop"] = {"at": ts, "date": _day(ts), "reason": reason}
                self._put_state(st)
            return reason
        return None

    _check_stop = check_stop

    # -- Telegram: ONE message a night (ideas and/or health), a manual run always answers
    def _message(self, rep: Dict[str, Any], stop_msg: Optional[str] = None) -> None:
        health = list(rep.get("health") or [])
        if stop_msg:
            health.append("The agent switched itself OFF: " + stop_msg + ". Work on data and costs instead; press Resume on the runner page to start counting again.")
        manual = rep["trigger"] != "schedule"
        with self.slock:
            st = self._state()
            already = st.get("message_date") == rep["date"]
        if not self.notify:
            return
        if not manual and ((already and not stop_msg) or (not rep["ideas"] and not health)):
            return
        try:
            lines = [f"AI agent ({rep['mode']}) {rep['date']} - ideas only, no setting was changed"]
            if rep.get("note"):
                lines.append(rep["note"])
            for w in rep["weaknesses"][:3]:
                lines.append(f"- {w}")
            for i in rep["ideas"]:
                lines.append(f"• {i['switch']} {i['direction']}: {i['why']}" + (" (its rule says: no change)" if i.get("support") == "rule_holds" else ""))
            if rep["queued"]:
                lines.append("Queued tests: " + ", ".join(sorted({q["test"] for q in rep["queued"]})))
            if rep["skipped"]:
                lines.append(f"Not queued: {len(rep['skipped'])} ({scrub(rep['skipped'][0]['reason'], 80)})")
            if rep["dropped"]:
                lines.append(f"Dropped {len(rep['dropped'])} answer(s) outside the rules")
            if rep.get("auto_calibration"):
                lines.append(scrub(rep["auto_calibration"], 120))
            for h in health[:4]:
                lines.append("⚠ " + scrub(h, 160))
            self.notify("Stockky AI agent", "\n".join(lines)[:1500], "normal")
            with self.slock:
                st = self._state()
                st["message_date"] = rep["date"]
                self._put_state(st)
        except Exception:  # noqa: BLE001
            pass

    # -- what the API shows (never a key)
    def status(self) -> Dict[str, Any]:
        st = self._state()
        tr = self._trials()
        mo = self.market_open()
        stopped = st.get("stop") or None
        u = self._usage()
        used = int(u.get("gemini", 0)) + int(u.get("groq", 0))
        return {"mode": self.mode, "mode_effective": "off" if stopped else self.mode, "stopped": stopped,
                "run_after_ist": f"{self.after_min // 60:02d}:{self.after_min % 60:02d}", "max_ideas": self.max_ideas, "max_queue": self.max_queue,
                "market_open": bool(mo), "run_now_allowed": (not mo) and not stopped and self.mode != "off",
                "allow_list": sorted(self.rules), "keys": {"gemini": bool(self.llm.gk), "groq": bool(self.llm.qk)},
                "models": {"gemini": self.llm.gm, "gemini_backup": self.llm.gb, "groq": self.llm.qm}, "last": st.get("last"), "last_auto_date": st.get("last_auto_date"),
                "trials": {k: tr.get(k, 0) for k in ("runs", "ideas", "dropped")}, "scoreboard": self.scoreboard(),
                "usage_today": {"date": u["date"], "gemini": int(u.get("gemini", 0)), "groq": int(u.get("groq", 0)), "runs": int(u.get("runs", 0)), "used": used,
                                "cap": self.max_calls_day, "left": max(0, self.max_calls_day - used), "gemini_blocked": bool(u.get("gemini_blocked"))},
                "last_report": st.get("last_report"), "settings": self.settings(), "next_run": self.next_run()}

    # -- round 541: one line per saved day, with each idea's outcome from the scoreboard
    def history(self, limit: int = 30) -> Dict[str, Any]:
        limit = max(1, min(120, int(limit)))
        d = os.path.join(self.dir, "agent")
        try:
            names = sorted((n for n in os.listdir(d) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n)), reverse=True)[:limit]
        except OSError:
            names = []
        verdicts = {(e.get("date"), e.get("switch")): e.get("verdict") for e in self._trials()["log"]}
        days: List[Dict[str, Any]] = []
        for n in names:
            rep = _read_json(os.path.join(d, n), None)
            if not isinstance(rep, dict):
                days.append({"date": n[:-5], "unreadable": True})
                continue
            qd = {q.get("switch"): q for q in rep.get("queued") or [] if isinstance(q, dict)}
            ideas = [{"switch": i.get("switch"), "direction": i.get("direction"), "why": i.get("why"), "support": i.get("support"),
                      "queued_test": (qd.get(i.get("switch")) or {}).get("test"), "outcome": verdicts.get((rep.get("date"), i.get("switch"))) or "pending"}
                     for i in rep.get("ideas") or [] if isinstance(i, dict)]
            days.append({"date": rep.get("date") or n[:-5], "at": rep.get("at"), "mode": rep.get("mode"), "trigger": rep.get("trigger"), "by": rep.get("by"), "source": rep.get("source"),
                         "note": rep.get("note"), "auto_calibration": rep.get("auto_calibration"), "breakdown": rep.get("breakdown"), "ideas": ideas, "dropped": rep.get("dropped") or [],
                         "weaknesses": rep.get("weaknesses") or [], "queued_n": len(rep.get("queued") or []), "skipped_n": len(rep.get("skipped") or []), "errors_n": len(rep.get("errors") or []),
                         "health": rep.get("health") or []})
        return {"days": days, "count": len(days), "note": "one file per day: a second run on the same day replaces the first"}
