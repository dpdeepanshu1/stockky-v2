"""E272 round 6: the after-hours scan as a reader over the typed event feed (analysis-intelligence-service GET {EVENT_URL}/events/active).

The scan used to score headlines by keywords in this service and throw a negative headline away (score 0). The feed already holds one typed record per
symbol / type / headline (type, polarity, strength, published time, source), the same one Position Stocks reads, so this module turns those records into
`NextDayWatchlistEntry` hits and keeps a negative event as a VETO row instead of dropping it. No database column is added: the event type, polarity and
strength travel in `catalyst_source` (String 64) as `<source>|ev=<type>|p=<+1|-1>|s=<strength>`, which `encode_tag` / `decode_tag` read and write.

Rules (all pure, never raise):
  * unknown polarity (0) never becomes a hit; polarity +1 scores `strength at announce time x 100`; polarity -1 becomes a veto (score 0, tag p=-1).
  * strength is taken AT the announce time (age 0), not at read time: the feed halves strength every hour, so an event from 16:00 would read ~0 at 08:45
    although it is exactly what the next session should trade.
  * two independent sources for the same symbol / type / polarity lift the strength (x1.15, cap 1.0); a single positive source is capped at SINGLE_SOURCE_CAP so one
    weak report cannot reach full size (a single negative source is not capped: the safe direction).
  * a veto beats a positive for the same symbol (negative news wins).
  * the feed down, ok:false or an unreadable answer returns None: the caller falls back to the old keyword scan, so a feed outage changes nothing.
`cadence_seconds()` is the new polling cadence (15:30-20:00 every 30 min, 20:00-08:00 every 2 h, 08:00-09:00 every 10 min).
"""
from __future__ import annotations

import logging
from datetime import time as _time
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Must equal TYPE_WEIGHT in analysis-intelligence-service/event/event_feed.py (a test compares the two files).
TYPE_WEIGHT = {"regulatory": 0.9, "results": 0.85, "deal": 0.7, "order": 0.65, "rating": 0.45, "management": 0.4, "other": 0.2}
# the old catalyst_type values the rest of the code and the Telegram icons know
LEGACY_CATALYST = {"results": "results", "deal": "bulk_block", "management": "board"}
SINGLE_SOURCE_CAP = 0.70
MULTI_SOURCE_LIFT = 1.15
TAG_MAX = 64


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def encode_tag(source: str, etype: str, polarity: int, strength: float) -> str:
    """`<source>|ev=<type>|p=<+1|-1>|s=<0.85>`, at most 64 characters (the source name is cut first)."""
    tail = f"|ev={str(etype or 'other')[:12]}|p={'+1' if int(polarity) >= 0 else '-1'}|s={round(_f(strength), 2)}"
    return (str(source or "feed")[: max(1, TAG_MAX - len(tail))] + tail)[:TAG_MAX]


def decode_tag(catalyst_source: Any) -> Dict[str, Any]:
    """{source, type, polarity, strength} from a stored catalyst_source; type / polarity / strength are None for a legacy (untagged) row."""
    out: Dict[str, Any] = {"source": None, "type": None, "polarity": None, "strength": None}
    try:
        parts = str(catalyst_source or "").split("|")
        out["source"] = parts[0] or None
        for p in parts[1:]:
            k, _, v = p.partition("=")
            if k == "ev" and v:
                out["type"] = v
            elif k == "p" and v in ("+1", "-1", "1"):
                out["polarity"] = -1 if v == "-1" else 1
            elif k == "s":
                out["strength"] = round(_f(v, 0.0), 3)
    except Exception:  # noqa: BLE001
        pass
    return out


def is_veto(catalyst_source: Any) -> bool:
    return decode_tag(catalyst_source).get("polarity") == -1


def _family(source: str) -> str:
    """Moneycontrol, Moneycontrol-business and Moneycontrol-google are one publisher: they do not count as two independent sources."""
    return str(source or "").split("-")[0].strip().lower()


def hits_from_events(events: Iterable[Dict[str, Any]], known_symbols: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """{symbol: hit} from feed records. hit = {score, headline, catalyst_type, source, event_type, polarity, strength}; a veto has score 0.0 and polarity -1.
    `known_symbols` (optional) drops a symbol outside the NSE list. Never raises."""
    try:
        known = {str(s).upper() for s in known_symbols} if known_symbols else None
        groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        for e in events or []:
            if not isinstance(e, dict):
                continue
            sym = str(e.get("symbol") or "").strip().upper()
            pol = int(_f(e.get("polarity")))
            if not sym or pol == 0 or (known is not None and sym not in known):
                continue
            etype = str(e.get("type") or "other")
            groups.setdefault((sym, etype, 1 if pol > 0 else -1), []).append(e)
        best: Dict[str, Dict[str, Any]] = {}
        for (sym, etype, pol), evs in groups.items():
            base = TYPE_WEIGHT.get(etype, 0.2)
            families = {_family(x.get("source")) for x in evs if x.get("source")}
            if len(families) >= 2:
                strength = min(1.0, base * MULTI_SOURCE_LIFT)
            else:
                strength = base if pol < 0 else min(base, SINGLE_SOURCE_CAP)   # the single-source cap is for positives; one negative report still vetoes at full weight
            lead = max(evs, key=lambda x: _f(x.get("published_ts"), 0.0))
            src = str(lead.get("source") or "feed")
            hit = {"score": 0.0 if pol < 0 else round(strength * 100.0, 1), "headline": str(lead.get("headline") or "")[:300],
                   "catalyst_type": LEGACY_CATALYST.get(etype, "news"), "source": encode_tag(src, etype, pol, strength),
                   "event_type": etype, "polarity": pol, "strength": round(strength, 3), "published": str(lead.get("published") or "")}
            cur = best.get(sym)
            if cur is None or (pol < 0 and cur["polarity"] > 0) or (pol == cur["polarity"] and hit["strength"] > cur["strength"]):
                best[sym] = hit            # a veto beats a positive; between equals the stronger wins
        return best
    except Exception as e:  # noqa: BLE001
        logger.warning("event_reader: hits_from_events failed (%s)", e)
        return {}


def merge_into(best: Dict[str, Dict[str, Any]], feed_hits: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Put the typed hits over the keyword hits in `best` (same dict, returned). A veto replaces whatever the keywords found; a typed positive keeps the
    higher of the two scores but always carries the typed tag. Never raises."""
    try:
        for sym, hit in (feed_hits or {}).items():
            old = best.get(sym)
            if old is None or hit.get("polarity", 0) < 0:
                best[sym] = hit
                continue
            if old.get("polarity") == -1:
                continue                   # an existing veto stands
            merged = dict(hit)
            merged["score"] = max(_f(hit.get("score")), _f(old.get("score")))
            best[sym] = merged
    except Exception as e:  # noqa: BLE001
        logger.warning("event_reader: merge_into failed (%s)", e)
    return best


def fetch_active(base_url: str, timeout_s: float = 4.0) -> Optional[Dict[str, Any]]:
    """GET {base_url}/events/active; the parsed body only when it says ok:true with a list of events, else None (feed down, no healthy source, bad body)."""
    try:
        import httpx
        with httpx.Client(timeout=timeout_s) as c:
            r = c.get(f"{str(base_url).rstrip('/')}/events/active", params={"limit": 500})
        if r.status_code != 200:
            return None
        d = r.json()
        if isinstance(d, dict) and d.get("ok") is True and isinstance(d.get("events"), list):
            return d
    except Exception as e:  # noqa: BLE001
        logger.info("event_reader: event feed not reachable (%s)", type(e).__name__)
    return None


def cadence_seconds(now_t: _time, start: _time, evening_end: _time, ramp_start: _time, ramp_end: _time,
                    evening_s: float = 1800.0, night_s: float = 7200.0, ramp_s: float = 600.0) -> float:
    """The new poll interval at clock time `now_t` (IST): evening 15:30-20:00 `evening_s`, night 20:00-08:00 `night_s`, pre-open ramp 08:00-09:00 `ramp_s`;
    86400 inside the session (nothing to scan; the caller caps the sleep at the next boundary). Never below 30 s."""
    try:
        if ramp_start <= now_t < ramp_end:
            v = ramp_s
        elif ramp_end <= now_t < start:
            v = 86400.0
        elif start <= now_t < evening_end:
            v = evening_s
        else:
            v = night_s
        return max(30.0, float(v))
    except Exception:  # noqa: BLE001
        return 1800.0
