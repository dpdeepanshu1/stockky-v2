"""
adaptive_thresholds.py — Self-adjusting regime-dependent trading thresholds.

WHY THIS EXISTS (per the improvement plan):
═══════════════════════════════════════════
The Aug-2026 market-intelligence patch baked specific numbers into config.py
(ENTRY_REGIME_MIN_SCORE=38, ENTRY_MIN_REWARD_RISK=2.0, CANDIDATE_MIN_CONVICTION=55,
CANDIDATE_DOWNTREND_6M_PCT=-10.0). These were CORRECT for Aug-2026 but are
FROZEN SNAPSHOTS — if Nifty recovers to 26,000 and FIIs turn net buyers,
those numbers are too tight and will block valid trades indefinitely.

THIS MODULE FIXES THAT by:
  1. Recording every market_score reading into MarketRegimeHistory (DB table).
  2. Computing regime-gate threshold as the 20th percentile of the trailing
     90 days of scores — automatically loose in bull markets, tight in bear.
  3. Tagging every regime-dependent constant with a _LAST_REVIEWED date and
     warning when any is stale (>30 days).
  4. Including threshold age in every WAIT/BLOCK reasoning string so you
     see exactly how old a judgment is every time it fires.

STRUCTURAL vs REGIME-DEPENDENT split (per the plan):
  STRUCTURAL (never touch): RISK_MIN_STOCK_PRICE, RISK_MAX_POSITION_CONCENTRATION_PCT
  REGIME-DEPENDENT (this module manages): ENTRY_REGIME_MIN_SCORE,
    ENTRY_MIN_REWARD_RISK, CANDIDATE_MIN_CONVICTION, CANDIDATE_DOWNTREND_6M_PCT,
    CANDIDATE_MIN_BULLISH_TF, CANDIDATE_OVEREXTENDED_52W_TOP_PCT

FALLBACK GUARANTEE: every function here falls back to the static config.py
constant when DB is unavailable or history is < 30 days. This module NEVER
blocks a trade that the static config would allow — it can only tighten
dynamically, and only once 30 days of history exists.
"""
from __future__ import annotations

import json
import logging
import os
from env_util import env_float_safe, env_int_safe  # E503: a typo / blank setting must not stop the import
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

import config
from models import MarketRegimeHistory

logger = logging.getLogger("real-trade-adaptive")

_REGIME_CONSTANTS = {
    "ENTRY_REGIME_MIN_SCORE":             (config.ENTRY_REGIME_MIN_SCORE,             "2026-10-04"),
    "ENTRY_MIN_REWARD_RISK":              (config.ENTRY_MIN_REWARD_RISK,              "2026-10-04"),
    "CANDIDATE_MIN_CONVICTION":           (config.CANDIDATE_MIN_CONVICTION,           "2026-10-04"),
    "CANDIDATE_DOWNTREND_6M_PCT":         (config.CANDIDATE_DOWNTREND_6M_PCT,         "2026-10-04"),
    "CANDIDATE_MIN_BULLISH_TF":           (config.CANDIDATE_MIN_BULLISH_TF,           "2026-10-04"),
    "CANDIDATE_OVEREXTENDED_52W_TOP_PCT": (config.CANDIDATE_OVEREXTENDED_52W_TOP_PCT, "2026-10-04"),
}

ADAPTIVE_HISTORY_DAYS     = env_int_safe("ADAPTIVE_HISTORY_DAYS", 90, lo=0)
ADAPTIVE_MIN_HISTORY_DAYS = env_int_safe("ADAPTIVE_MIN_HISTORY_DAYS", 30, lo=0)
ADAPTIVE_PERCENTILE       = env_float_safe("ADAPTIVE_PERCENTILE", 20.0, lo=0, hi=100)
STALE_THRESHOLD_DAYS      = env_int_safe("ADAPTIVE_STALE_THRESHOLD_DAYS", 30, lo=0)


# ── Score recording ───────────────────────────────────────────────────────────

def record_market_score(db: Session, score: int) -> None:
    """Persist a market_score reading. Called every time entry_engine
    fetches from /market/indices. Best-effort — never raises."""
    try:
        db.add(MarketRegimeHistory(score=float(score)))
        db.commit()
    except Exception as e:
        logger.debug("record_market_score failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    # 2026-09-12 fix (audit finding): _prune_old_scores was defined but never
    # called anywhere, so market_regime_history grew unboundedly (~125
    # rows/day at the ~120s regime-cache TTL across market hours). Same
    # self-prune-on-every-write pattern adaptive_market_params.py's own
    # record_metric() already uses — call it here too, right after the
    # insert. Best-effort/non-fatal, same as the insert above.
    _prune_old_scores(db)


def _prune_old_scores(db: Session) -> None:
    """Remove scores older than ADAPTIVE_HISTORY_DAYS to keep the table small."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_HISTORY_DAYS + 5)
        db.execute(
            text("DELETE FROM market_regime_history WHERE recorded_at < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
    except Exception as e:
        logger.debug("prune_old_scores failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass


# ── Adaptive regime gate ──────────────────────────────────────────────────────

def adaptive_regime_threshold(db: Session) -> tuple[int, str]:
    """Round 206: the history-based threshold (see _history_regime_threshold) shifted by the multi-horizon market
    picture from regime_adaptive.py (yesterday .. 1 year, volatility, recent results). No fresh snapshot or
    REGIME_ADAPTIVE_MODE=off -> shift 0 -> exactly the old value and source string."""
    threshold, source = _history_regime_threshold(db)
    try:
        import regime_adaptive
        d = regime_adaptive.score_delta()
        if d:
            return max(20, min(60, int(threshold) + d)), f"{source}{d:+d}tone"
    except Exception as e:  # noqa: BLE001
        logger.debug("regime_adaptive shift skipped: %s", e)
    return threshold, source


def _history_regime_threshold(db: Session) -> tuple[int, str]:
    """
    Returns (threshold, source) where source is one of:
      'adaptive_Nd_p20' — computed from N days of history at 20th percentile
      'static'          — fell back to config.ENTRY_REGIME_MIN_SCORE

    The adaptive threshold is the 20th percentile of trailing 90 days of
    market_score readings:
      - In a bull market (scores consistently 60-80), p20 ≈ 55 → loosens gate
      - In a correction (scores 25-45 like Aug-2026), p20 ≈ 28 → tightens gate
        (but not as tight as the manually-frozen 38 would be once the market recovers)
      - Falls back to static value if < ADAPTIVE_MIN_HISTORY_DAYS of data

    This eliminates the "frozen snapshot" problem: the gate self-calibrates
    to the actual recent market regime without requiring a manual code change.
    """
    static_val = config.ENTRY_REGIME_MIN_SCORE
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_HISTORY_DAYS)
        rows = (
            db.query(MarketRegimeHistory.score, MarketRegimeHistory.recorded_at)
            .filter(MarketRegimeHistory.recorded_at >= cutoff)
            .order_by(MarketRegimeHistory.recorded_at.asc())
            .all()
        )
        scores = [float(r.score) for r in rows]
        # BUG FIX (2026-09-01): the "30 days of history" safety gate was
        # comparing len(scores) — i.e. the raw READING count — against
        # ADAPTIVE_MIN_HISTORY_DAYS. record_market_score() is called once
        # per regime-cache refresh (~every 120s the cache is cold, per
        # entry_engine's _REGIME_TTL_S / auto-pilot's ~180s cycle interval),
        # not once per calendar day, so 30 readings accumulates in roughly
        # an hour of a single trading session — the adaptive percentile then
        # activates on a couple hours of intraday data instead of the 30
        # distinct days the module's own docstring/FALLBACK GUARANTEE
        # promises. Count distinct calendar days actually covered instead.
        distinct_days = len({r.recorded_at.date() for r in rows})
        if distinct_days < ADAPTIVE_MIN_HISTORY_DAYS:
            return static_val, "static"

        sorted_scores = sorted(scores)
        n = len(sorted_scores)
        # 20th percentile index
        idx = max(0, int(n * ADAPTIVE_PERCENTILE / 100.0) - 1)
        p20 = sorted_scores[idx]
        threshold = int(round(p20))
        # Never go below 20 (protect against an extreme crash misreading)
        threshold = max(20, threshold)
        source = f"adaptive_{n}d_p{int(ADAPTIVE_PERCENTILE)}"
        logger.debug(
            "adaptive_regime_threshold: %d readings → p%d = %d (static fallback=%d)",
            n, int(ADAPTIVE_PERCENTILE), threshold, static_val,
        )
        return threshold, source
    except Exception as e:
        logger.debug("adaptive_regime_threshold error (falling back to static): %s", e)
        return static_val, "static"


# ── Staleness checking ────────────────────────────────────────────────────────

def _reviewed(name: str, hard_coded: str) -> str:
    """Round 512: the later of the hard-coded review date and the last successful market-research run (market_research.py)."""
    try:
        import market_research
        return market_research.last_reviewed(name, hard_coded)
    except Exception:  # noqa: BLE001 - research is optional; the hard-coded date still applies
        return hard_coded


def _days_since(date_str: str) -> int:
    try:
        reviewed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - reviewed).days
    except Exception:
        return 0


def check_threshold_staleness() -> list[dict]:
    """
    Returns a list of stale regime constants — any constant whose
    _LAST_REVIEWED date is older than STALE_THRESHOLD_DAYS.
    Called at startup and by the dashboard's /adaptive/status endpoint.
    """
    stale = []
    try:
        import regime_adaptive as _ra
    except Exception:  # noqa: BLE001
        _ra = None
    for name, (value, reviewed_date) in _REGIME_CONSTANTS.items():
        if _ra is not None and (_ra.is_managed(name) or name in _ra.RETIRED):
            continue        # round 206: this constant is re-set from the market every 15 minutes; a review date means nothing
        reviewed_date = _reviewed(name, reviewed_date)      # round 512: a market-research run resets the clock
        age_days = _days_since(reviewed_date)
        if age_days >= STALE_THRESHOLD_DAYS:
            stale.append({
                "constant":      name,
                "current_value": value,
                "last_reviewed": reviewed_date,
                "age_days":      age_days,
                "warning": (
                    f"{name}={value} was set {age_days}d ago (>{STALE_THRESHOLD_DAYS}d). "
                    "Re-run market research and update if the regime has changed."
                ),
            })
    return stale


# Telegram noise control (group 95, item 19): the stale-constant notice used to go out on EVERY boot
# (every deploy, every restart). The warning is about constants that change on a scale of weeks, so
# it is now sent when the stale set changes, or at most once per STALE_NOTICE_INTERVAL_HOURS
# (default 168 = weekly). The log lines are unchanged and still appear on every boot. State lives in
# a small file (default /tmp, like boot_forensics): it survives a container restart but not a
# container re-create, so a fresh deploy sends one message.
_STALE_NOTICE_PATH = os.getenv("STALE_NOTICE_STATE_PATH", "/tmp/stockky_stale_notice.json")
try:
    _STALE_NOTICE_INTERVAL_S = max(0.0, float((os.getenv("STALE_NOTICE_INTERVAL_HOURS") or "").strip() or 168)) * 3600.0
except ValueError:
    _STALE_NOTICE_INTERVAL_S = 168 * 3600.0


def _stale_signature(stale: list[dict]) -> str:
    return ",".join(sorted(f"{i['constant']}={i['current_value']}@{i['last_reviewed']}" for i in stale))


def _stale_notice_due(stale: list[dict], now: float | None = None) -> bool:
    """True when the stale-constants Telegram notice should be sent now. Never raises; any problem
    reading the state file means "send" (a duplicate message beats a missed warning)."""
    try:
        now = time.time() if now is None else now
        with open(_STALE_NOTICE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if st.get("sig") != _stale_signature(stale):
            return True
        age = now - float(st.get("ts"))
        return age < 0 or age >= _STALE_NOTICE_INTERVAL_S
    except Exception:
        return True


def _stale_notice_record(stale: list[dict], now: float | None = None) -> None:
    try:
        with open(_STALE_NOTICE_PATH, "w", encoding="utf-8") as f:
            json.dump({"sig": _stale_signature(stale), "ts": time.time() if now is None else now}, f)
    except Exception:
        pass    # an unwritable state file only means the next boot may notify again


def _wait_for_adaptive_snapshot(max_wait_s: float, poll_s: float = 5.0) -> None:
    """Block until regime_adaptive has a fresh snapshot, REGIME_ADAPTIVE_MODE is off, or `max_wait_s` has passed."""
    try:
        import regime_adaptive as _ra
        waited = 0.0
        while _ra.mode() == "live" and _ra._fresh_snapshot() is None and waited < max_wait_s:
            time.sleep(poll_s)
            waited += poll_s
    except Exception:  # noqa: BLE001 - no regime_adaptive module: just run the check
        pass


def startup_staleness_warning(defer_until_snapshot: bool = False) -> None:
    """Log (and optionally Telegram-notify) stale constants at service boot.
    Called from main.py startup event — never blocks boot.

    Round 512: main.py passes defer_until_snapshot=True. At boot regime_adaptive has no snapshot yet (its first refresh is 45 s later), so
    is_managed() was False for all five constants and every restart sent "Stale trading thresholds detected" for constants that adapt
    themselves. With the flag the check runs on a daemon thread after the first snapshot exists (waits at most ADAPTIVE_STARTUP_GRACE_S,
    default 240 s; REGIME_ADAPTIVE_MODE=off needs no wait). Without the flag (tests, other callers) it runs at once, as before."""
    if defer_until_snapshot:
        try:
            import regime_adaptive as _ra
            if _ra.mode() == "live" and _ra._fresh_snapshot() is None:
                import threading

                def _later() -> None:
                    try:
                        _wait_for_adaptive_snapshot(env_float_safe("ADAPTIVE_STARTUP_GRACE_S", 240.0, lo=0))
                        startup_staleness_warning()
                    except Exception as e:  # noqa: BLE001
                        logger.debug("deferred staleness check failed (non-fatal): %s", e)

                threading.Thread(target=_later, name="stale-check-after-snapshot", daemon=True).start()
                return
        except Exception:  # noqa: BLE001 - cannot tell: fall through to the immediate check
            pass
    stale = check_threshold_staleness()
    if not stale:
        logger.info("adaptive_thresholds: all regime constants reviewed within %dd", STALE_THRESHOLD_DAYS)
        return
    for item in stale:
        logger.warning(
            "STALE REGIME CONSTANT: %s (set %s, %dd ago) — "
            "re-run market research if the market regime has changed significantly.",
            item["constant"], item["last_reviewed"], item["age_days"],
        )
    if not _stale_notice_due(stale):
        logger.info("adaptive_thresholds: stale-constant Telegram notice already sent recently - not repeating on boot")
        return
    # Best-effort Telegram notify (non-blocking)
    try:
        from notifier import notify_sync
        lines = ["⚠️ *Stale trading thresholds detected*"]
        for item in stale:
            lines.append(
                f"  • `{item['constant']}={item['current_value']}` "
                f"(set {item['last_reviewed']}, {item['age_days']}d ago)"
            )
        lines.append(
            "Run Market research (Real Money Calibration page, button 'Run market research now'; it also runs every "
            "Saturday 09:00 IST) to review them and reset this warning. "
            "Adaptive gate will self-adjust regardless — this warning is for the "
            "non-adaptive constants."
        )
        notify_sync("\n".join(lines))
        _stale_notice_record(stale)
    except Exception as _tg_err:  # Telegram failure never affects startup (and is not recorded, so the next boot retries)
        logger.warning("stale-threshold notice not sent (will retry next boot): %s", _tg_err)


# ── Threshold age annotation for reasoning strings ───────────────────────────

def threshold_age_note(constant_name: str, effective=None) -> str:
    """
    Returns a short string like "(gate=38, set 2026-08-28, 12d ago)" to
    embed in WAIT/BLOCK reasoning so the dashboard shows exactly how old
    the judgment is every time a threshold fires.
    Called by entry_engine.py when logging regime-gate WAIT decisions.

    group230: `effective` is the gate the caller actually applied. The constants table holds the STATIC value
    (25), while the live gate is usually the adaptive one (36), so the log read "gate=36 (adaptive) (gate=25,
    set ...)" and looked like two gates. When `effective` is given and differs from the table value, the note
    names the table value as the static default instead of printing a second "gate=".
    """
    entry = _REGIME_CONSTANTS.get(constant_name)
    if entry is None:
        return ""
    value, reviewed_date = entry
    reviewed_date = _reviewed(constant_name, reviewed_date)
    age_days = _days_since(reviewed_date)
    if effective is not None and effective != value:
        return f"(static default {value}, set {reviewed_date}, {age_days}d ago)"
    return f"(gate={value}, set {reviewed_date}, {age_days}d ago)"


# ── Full adaptive status snapshot ─────────────────────────────────────────────

def adaptive_status(db: Session) -> dict:
    """Full status dict for GET /adaptive/status dashboard endpoint."""
    threshold, source = adaptive_regime_threshold(db)
    stale = check_threshold_staleness()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_HISTORY_DAYS)
        recorded_ats = [
            r[0] for r in
            db.query(MarketRegimeHistory.recorded_at)
            .filter(MarketRegimeHistory.recorded_at >= cutoff)
            .all()
        ]
        count = len(recorded_ats)
        # BUG FIX (2026-09-01): same readings-vs-days mixup as
        # adaptive_regime_threshold() — activation/advice must be gated on
        # distinct days observed, not raw reading rows (see that function's
        # comment for why those diverge sharply intraday).
        distinct_days = len({dt.date() for dt in recorded_ats})
        latest_score_row = (
            db.query(MarketRegimeHistory)
            .order_by(MarketRegimeHistory.recorded_at.desc())
            .first()
        )
        latest_score = float(latest_score_row.score) if latest_score_row else None
    except Exception:
        count = 0
        distinct_days = 0
        latest_score = None

    return {
        "adaptive_regime_threshold":  threshold,
        "threshold_source":           source,
        "history_days_used":          ADAPTIVE_HISTORY_DAYS,
        "history_readings_available": count,
        "history_days_available":     distinct_days,
        "min_history_needed":         ADAPTIVE_MIN_HISTORY_DAYS,
        "adaptive_active":            distinct_days >= ADAPTIVE_MIN_HISTORY_DAYS,
        "percentile_used":            ADAPTIVE_PERCENTILE,
        "static_fallback":            config.ENTRY_REGIME_MIN_SCORE,
        "latest_market_score":        latest_score,
        "stale_constants":            stale,
        "regime_adaptive":            _regime_adaptive_status(),
        "regime_constants": {
            name: {"value": val, "last_reviewed": _reviewed(name, reviewed), "age_days": _days_since(_reviewed(name, reviewed))}
            for name, (val, reviewed) in _REGIME_CONSTANTS.items()
        },
        "advice": (
            "Adaptive gate is active — threshold auto-adjusts to market regime."
            if distinct_days >= ADAPTIVE_MIN_HISTORY_DAYS
            else f"Adaptive gate needs {ADAPTIVE_MIN_HISTORY_DAYS - distinct_days} more distinct "
                 f"trading day(s) of history to activate ({count} readings across "
                 f"{distinct_days} day(s) so far). "
                 f"Using static fallback {config.ENTRY_REGIME_MIN_SCORE} until then."
        ),
    }


def _regime_adaptive_status() -> dict:
    try:
        import regime_adaptive
        return regime_adaptive.status()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
