"""journal_notes.py (real-trade-service) - E75, the Real Trade twin of position-stocks orders/journal_notes.py (E30): a reason and a feeling for each trade, and what they say about results.

WHY: the journal showed P&L by day and win rate by scan window, but nothing about WHY a trade was taken or how the trader felt. The usual
leaks (FOMO entries, revenge trades after a loss) only show up when that is written down next to the result.

  reason   free text, up to 300 characters, trimmed (what the setup was)
  emotion  one of EMOTIONS (calm, confident, hopeful, unsure, impatient, fomo, fear, greed, revenge) or blank

Stats are descriptive only: trades, wins, win rate and P&L per feeling over SETTLED trades (a CLOSED position, see classify), and a "small sample" flag under
MIN_SAMPLE trades. Nothing here gates, scores, sizes or blocks a trade. Pure functions + two small DB helpers; the DB helpers never raise out of
the snapshot refresh.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("real-trade-journal-notes")

EMOTIONS: Tuple[str, ...] = ("calm", "confident", "hopeful", "unsure", "impatient", "fomo", "fear", "greed", "revenge")
RISKY_EMOTIONS = frozenset({"fomo", "fear", "greed", "revenge", "impatient"})
MAX_REASON = 300
MIN_SAMPLE = 30


class JournalInputError(ValueError):
    """A reason / emotion the journal will not store (the message is shown to the user)."""


def clean_emotion(v: Any) -> Optional[str]:
    """None for blank; the lower-case emotion when valid; JournalInputError otherwise."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s not in EMOTIONS:
        raise JournalInputError(f"emotion must be one of: {', '.join(EMOTIONS)}")
    return s


def clean_reason(v: Any) -> Optional[str]:
    """None for blank; the text with runs of whitespace collapsed; JournalInputError when longer than MAX_REASON."""
    if v is None:
        return None
    s = " ".join(str(v).split())
    if not s:
        return None
    if len(s) > MAX_REASON:
        raise JournalInputError(f"reason is too long ({len(s)} characters, the limit is {MAX_REASON})")
    return s


def summarize(notes: Iterable[Any]) -> Dict[str, Any]:
    """{"by_emotion": [...], "note_count": n, "settled": n}. One entry per emotion that has a settled trade; sorted by trades then name.
    `risky` marks the emotions that usually hurt (fomo / fear / greed / revenge / impatient) - a label, not a verdict."""
    agg: Dict[str, Dict[str, Any]] = {}
    total = settled = 0
    for n in notes:
        total += 1
        out = getattr(n, "outcome", None)
        emo = getattr(n, "emotion", None)
        if out not in ("win", "loss", "breakeven") or not emo:
            continue
        settled += 1
        a = agg.setdefault(emo, {"emotion": emo, "trades": 0, "wins": 0, "pnl": 0.0})
        a["trades"] += 1
        a["wins"] += 1 if out == "win" else 0
        a["pnl"] += float(getattr(n, "realized_pnl", 0.0) or 0.0)
    rows = []
    for a in agg.values():
        rows.append({"emotion": a["emotion"], "trades": a["trades"], "wins": a["wins"], "win_rate": round(a["wins"] / a["trades"], 3),
                     "pnl": round(a["pnl"], 2), "small_sample": a["trades"] < MIN_SAMPLE, "risky": a["emotion"] in RISKY_EMOTIONS})
    rows.sort(key=lambda r: (-r["trades"], r["emotion"]))
    return {"by_emotion": rows, "note_count": total, "settled": settled, "min_sample": MIN_SAMPLE}


_EPS = 0.005


def classify(pos: Any) -> Optional[str]:
    """win | loss | breakeven for a CLOSED position, None while it is still open (net of nothing: realized_pnl as stored)."""
    if getattr(pos, "status", None) != "CLOSED":
        return None
    pnl = getattr(pos, "realized_pnl", None)
    if pnl is None:
        return None
    return "win" if pnl > _EPS else ("loss" if pnl < -_EPS else "breakeven")


def is_manual(pos: Any) -> bool:
    """A position the engine did not open from a scan: no source_tab and not an imported broker holding."""
    return getattr(pos, "source_tab", None) is None and not getattr(pos, "broker_imported", False)


def untagged_manual(positions: Iterable[Any], noted_ids: Iterable[int]) -> List[int]:
    """Ids of MANUAL positions that have no note yet (newest first as given)."""
    have = set(noted_ids)
    return [p.id for p in positions if is_manual(p) and p.id not in have]


# ---------------------------------------------------------------- DB helpers (callers pass a SQLAlchemy session)
def snapshot_fields(pos: Any) -> Dict[str, Any]:
    """The trade snapshot kept on the note (survives the retention cleanup of the position row)."""
    return {"symbol": pos.symbol, "manual": bool(is_manual(pos)), "opened_at": getattr(pos, "opened_at", None),
            "status": getattr(pos, "status", None), "outcome": classify(pos),
            "realized_pnl": getattr(pos, "realized_pnl", None)}


def upsert(db: Any, model: Any, pos: Any, reason: Optional[str], emotion: Optional[str]) -> Optional[Any]:
    """Create / update the note for `pos`. Both fields blank removes the note and returns None. This commits."""
    row = db.query(model).filter_by(position_id=pos.id).first()
    if reason is None and emotion is None:
        if row is not None:
            db.delete(row)
            db.commit()
        return None
    if row is None:
        row = model(position_id=pos.id, mode=getattr(pos, "mode", None), symbol=pos.symbol)
        db.add(row)
    row.reason, row.emotion = reason, emotion
    for k, v in snapshot_fields(pos).items():
        setattr(row, k, v)
    db.commit()
    return row


def refresh_snapshots(db: Any, model: Any, position_model: Any, notes: List[Any]) -> int:
    """Copy status / outcome / pnl from positions that still exist onto their notes. Returns how many notes changed. Never raises."""
    try:
        ids = [n.position_id for n in notes]
        if not ids:
            return 0
        live = {p.id: p for p in db.query(position_model).filter(position_model.id.in_(ids)).all()}
        changed = 0
        for n in notes:
            p = live.get(n.position_id)
            if p is None:
                continue
            snap = snapshot_fields(p)
            if any(getattr(n, k) != v for k, v in snap.items()):
                for k, v in snap.items():
                    setattr(n, k, v)
                changed += 1
        if changed:
            db.commit()
        return changed
    except Exception as e:  # noqa: BLE001
        logger.warning("journal notes: outcome refresh failed: %s", e)
        try:
            db.rollback()
        except Exception as re_:  # noqa: BLE001
            logger.debug("journal notes: rollback after a failed refresh also failed: %s", re_)
        return 0


def note_dict(n: Any, iso=lambda d: d.isoformat() if d else None) -> Dict[str, Any]:
    return {"position_id": n.position_id, "symbol": n.symbol, "mode": n.mode, "manual": bool(n.manual), "reason": n.reason, "emotion": n.emotion,
            "status": n.status, "outcome": n.outcome, "realized_pnl": n.realized_pnl, "opened_at": iso(n.opened_at), "updated_at": iso(n.updated_at)}
