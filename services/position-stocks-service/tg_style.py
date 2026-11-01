"""tg_style.py - E285 round 93: one look for every Stockky Telegram message (pure, no I/O, never raises).

render(title, text, now=None) -> {"html", "kind", "effect"}: a header (kind icon + heading), a small line with the kind, the day and the IST time, a divider, then the body
re-laid so it reads on a phone: "Key: value" lines become "▸ Key · value", "A 1 | B 2 | C 3" lines become one bullet per field, indented headline lines become quotes,
signed P&L lines get a green / red dot, and each news item is separated by a blank line. Text is HTML-escaped (a stray < or & used to make Telegram refuse the whole
message and fall back to plain text); the tags the callers already write (<b> <i> <code>) and the *bold* / _italic_ markers are kept and turned into tags.
Telegram text cannot animate, so the "animation" is the message effect Telegram offers in private chats (confetti on a target hit, thumbs-up on a buy), asked for with
effect_for(kind, chat_id); the sender retries without it if Telegram refuses. Switches (read at call time): TELEGRAM_STYLE_ENABLED, TELEGRAM_EFFECTS_ENABLED.
Identical copy in notification-scheduler-service/notification, real-trade-service and position-stocks-service (check_shared_copies)."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

IST = timezone(timedelta(hours=5, minutes=30))
DIVIDER = "━━━━━━━━━━━━━━━━━━"
GENERIC_TITLES = {"", "stockky", "stockky trade", "stockky alert", "stockky notification", "stockky notifications"}
MAX_HTML = 4000                                    # Telegram's limit is 4096; a longer render is dropped so the sender uses the plain text
KIND_ICON = {"NEWS": "📰", "ALERT": "🚨", "PROFIT": "🎯", "STOP": "🛑", "BUY": "🟢", "SELL": "🔴", "REPORT": "📊", "INFO": "ℹ️"}
KIND_LABEL = {"NEWS": "NEWS", "ALERT": "ALERT", "PROFIT": "TARGET HIT", "STOP": "STOP HIT", "BUY": "BUY", "SELL": "SELL / EXIT", "REPORT": "REPORT", "INFO": "UPDATE"}
# Telegram message effects (private chats only). Ids are Telegram's own; a refused id is retried without an effect by the sender.
EFFECTS = {"PROFIT": "5046509860389126442", "BUY": "5107584321108051014"}      # confetti, thumbs-up

_RULES = (
    ("NEWS", re.compile(r"NEWS|EVENTS?\b|HEADLINE|AFTER-HOURS|PRE-MARKET|📰|📡")),
    ("ALERT", re.compile(r"ERROR|FAILED|\bFAIL\b|UNKNOWN|CRITICAL|\bHALT|REJECT|MISMATCH|BLOCKED|EMERGENCY|KILL|🚨|⚠️|⛔")),
    ("PROFIT", re.compile(r"TARGET (?:HIT|REACHED)|TAKE[- ]PROFIT|PROFIT (?:TAKEN|BOOKED)|🎯")),
    ("STOP", re.compile(r"STOP[- ]?LOSS|STOPPED|STOP HIT|\bSL HIT|HARD STOP|🛑")),
    ("BUY", re.compile(r"\bBUY|\bBOUGHT|\bENTRY\b|\bENTERED|\bOPENED|\bLONG\b|🟢")),
    ("SELL", re.compile(r"\bSELL|\bSOLD|\bSHORT\b|SQUARE|\bEXIT|\bCLOSED|🔴")),
    ("REPORT", re.compile(r"SCAN|SUMMARY|REPORT|DIGEST|RECAP|P&L|PNL|📊")),
)
_TAG = re.compile(r"(</?(?:b|i|u|s|code|pre|blockquote)>)")
_BOLD = re.compile(r"\*([^*\n]+)\*")
_ITAL = re.compile(r"(?<![\w])_([^_\n]+?)_(?![\w])")
_KEYVAL = re.compile(r"^([A-Za-z][A-Za-z0-9 /&;%()\-]{0,30}?):\s+(\S.*)$")
_SIGNED = re.compile(r"[+\-−]\s?₹?\s?\d")
_PNL_KEY = re.compile(r"P&L|PNL|PROFIT|LOSS|NET|GAIN|RETURN", re.I)


def style_enabled() -> bool:
    return (os.environ.get("TELEGRAM_STYLE_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")


def effects_enabled() -> bool:
    return (os.environ.get("TELEGRAM_EFFECTS_ENABLED") or "true").strip().lower() in ("1", "true", "yes", "on")


def classify(title: Any, text: Any) -> str:
    """The message kind from its title and first lines (a deep body word never decides it)."""
    try:
        head = f"{title or ''}\n{str(text or '')[:220]}".upper()
        for kind, rx in _RULES:
            if rx.search(head):
                return kind
    except Exception:  # noqa: BLE001
        pass
    return "INFO"


def effect_for(kind: str, chat_id: Any) -> Optional[str]:
    """The Telegram effect id for this kind, only for a private chat (a positive chat id) and only when the switch is on."""
    try:
        if not effects_enabled() or str(chat_id).strip().startswith("-") or not str(chat_id).strip():
            return None
        return EFFECTS.get(kind)
    except Exception:  # noqa: BLE001
        return None


def escape(s: str) -> str:
    """HTML-escape, keeping the few tags callers write themselves."""
    out = []
    for part in _TAG.split(s or ""):
        if _TAG.fullmatch(part):
            out.append(part)
        else:
            out.append(part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def _inline(s: str) -> str:
    """*bold* and _italic_ markers to tags (on already-escaped text)."""
    return _ITAL.sub(r"<i>\1</i>", _BOLD.sub(r"<b>\1</b>", s))


def _strip_tags(s: str) -> str:
    return re.sub(r"</?[a-z]+>", "", s)


def _pipe_line(line: str) -> Optional[list]:
    """'Entry ₹100 | Target ₹104 | Stop ₹98' -> one bullet per field, when there are 3 or more fields."""
    segs = [x.strip() for x in line.split(" | ") if x.strip()]
    if len(segs) < 3:
        return None
    out = []
    for sg in segs:
        m = re.match(r"^(<b>[^<]+</b>|[A-Za-z][A-Za-z%&/\-]*(?: [A-Za-z]+)?)\s+(.+)$", sg)
        out.append(f"▫️ <b>{_strip_tags(m.group(1))}</b>  {m.group(2)}" if m and not sg.startswith("<b>") else f"▫️ {sg}")
    return out


def _body_line(raw: str, kind: str) -> list:
    line = _inline(escape(raw.rstrip()))
    if not line.strip():
        return [""]
    if raw.startswith("  ") and kind == "NEWS":
        return [f"<blockquote>{_strip_tags(line).strip()}</blockquote>"]
    if raw.startswith("  "):
        return [f"   {line.strip()}"]
    pipe = _pipe_line(line)
    if pipe:
        return pipe
    if " | " in line:
        line = line.replace(" | ", "  ·  ")
    m = _KEYVAL.match(_strip_tags(line)) if "<" not in line.split(":")[0] else None
    if m and not line.lstrip().startswith(("http", "▫", "▸")):
        key, val = m.group(1).strip(), line.split(":", 1)[1].strip()
        dot = ""
        if _PNL_KEY.search(key) and _SIGNED.search(val):
            dot = "🔴 " if re.search(r"(?:^|[\s(])[-−]", val) else "🟢 "
        return [f"▸ {dot}<b>{key}</b> · {val}"]
    return [line]


def render(title: Any, text: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The styled message. On any failure returns {"html": None}: the sender then keeps its old plain behaviour."""
    try:
        title_s = str(title or "").strip()
        body_raw = str(text or "").replace("\r\n", "\n").strip("\n")
        kind = classify(title_s, body_raw)
        lines = body_raw.split("\n")
        first = lines[0].strip() if lines else ""
        generic = title_s.lower() in GENERIC_TITLES or title_s.lower().startswith(tuple(g + " (" for g in GENERIC_TITLES if g))
        if generic and first and len(first) <= 110 and (_BOLD.search(first) or "<b>" in first):
            head, lines = _inline(escape(first)), lines[1:]
            head = head if head.lstrip()[:1] and not head.lstrip()[0].isalnum() and not head.lstrip().startswith("<") else f"{KIND_ICON[kind]} {head}"
            head = re.sub(r"</?b>", "", head)
        else:
            t = title_s or "Stockky"
            head = escape(t) if not t[0].isalnum() else f"{KIND_ICON[kind]} {escape(t)}"
        ts = (now or datetime.now(IST)).astimezone(IST)
        sub = f"{KIND_LABEL[kind]} · {ts.strftime('%a %d %b')} · {ts.strftime('%H:%M')} IST"
        out = [f"<b>{head}</b>", f"<code>{sub}</code>", DIVIDER, ""]
        prev_quote = False
        for raw in lines:
            for ln in _body_line(raw, kind):
                if kind == "NEWS" and prev_quote and ln and not ln.startswith("<blockquote>") and out and out[-1] != "":
                    out.append("")                                  # a blank line between two news items
                out.append(ln)
                prev_quote = ln.startswith("<blockquote>")
        while out and out[-1] == "":
            out.pop()
        html = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
        if len(html) > MAX_HTML:
            return {"html": None, "kind": kind, "effect": None}
        return {"html": html, "kind": kind, "effect": EFFECTS.get(kind)}
    except Exception:  # noqa: BLE001
        return {"html": None, "kind": "INFO", "effect": None}
