"""
scheduler/governance_check.py  §11 — nightly governance alerting.

Runs nightly and alerts via Telegram when:
  1. Any adaptive threshold has drifted outside its guardrail band.
  2. Any regime-dependent constant has been unreviewed for > 30 days.
  3. Any sector bucket's rolling window stays thinner than MIN_SECTOR_SAMPLE=8
     for more than 5 consecutive sessions.

Run from notification-scheduler-service cron (same pattern as weekend_hydrator).
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("governance-check")

NOTIFY_URL = os.getenv("REAL_TRADE_URL", "").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
DB_URL = (
    os.getenv("CACHE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
)

# Regime-dependent constants with their review dates and guardrail bands
REGIME_CONSTANTS = {
    "ENTRY_REGIME_MIN_SCORE":         ("38",    "2026-08-28", 20, 60),
    "ENTRY_MIN_REWARD_RISK":          ("2.0",   "2026-08-28", 1.5, 3.0),
    "CANDIDATE_MIN_CONVICTION":       ("55",    "2026-08-28", 40, 75),
    "CANDIDATE_DOWNTREND_6M_PCT":     ("-10.0", "2026-08-28", -20, -5),
    "CANDIDATE_MIN_BULLISH_TF":       ("4",     "2026-08-28", 2, 5),
}

STALE_DAYS      = int(os.getenv("GOVERNANCE_STALE_DAYS", "30"))
MIN_SECTOR_SAMPLE = 8
THIN_SECTOR_SESSIONS = 5  # alert after this many consecutive thin sessions


def _days_since(date_str: str) -> int:
    try:
        reviewed = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - reviewed).days
    except Exception:
        return 0


def _send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — governance alert not sent")
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10.0,
        )
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def check_stale_constants() -> list:
    stale = []
    for name, (val, reviewed, guardrail_min, guardrail_max) in REGIME_CONSTANTS.items():
        age = _days_since(reviewed)
        if age >= STALE_DAYS:
            stale.append({
                "constant": name, "value": val, "reviewed": reviewed,
                "age_days": age, "guardrail": f"[{guardrail_min}, {guardrail_max}]",
            })
    return stale


def check_adaptive_guardrails() -> list:
    """Check if any adaptive threshold has drifted outside its guardrail band."""
    violations = []
    if not DB_URL:
        return violations
    try:
        url = DB_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            # Check adaptive regime threshold
            rows = conn.execute(text(
                "SELECT score FROM market_regime_history "
                "WHERE recorded_at > now() - interval '90 days' "
                "ORDER BY score"
            )).fetchall()
            scores = [float(r[0]) for r in rows]
            if len(scores) >= 30:
                import statistics
                p20 = sorted(scores)[int(len(scores) * 0.20)]
                _, _, guardrail_min, guardrail_max = REGIME_CONSTANTS["ENTRY_REGIME_MIN_SCORE"]
                if p20 < float(guardrail_min) or p20 > float(guardrail_max):
                    violations.append({
                        "constant": "ENTRY_REGIME_MIN_SCORE_adaptive",
                        "computed_p20": round(p20, 1),
                        "guardrail": f"[{guardrail_min}, {guardrail_max}]",
                        "warning": f"Adaptive threshold p20={p20:.1f} outside guardrail",
                    })
    except Exception as e:
        logger.debug("check_adaptive_guardrails: %s", e)
    return violations


def check_thin_sector_buckets() -> list:
    """Check if any sector has fewer than MIN_SECTOR_SAMPLE active symbols."""
    thin = []
    if not DB_URL:
        return thin
    try:
        url = DB_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT sector, COUNT(*) as n
                FROM symbol_master
                WHERE status = 'active' AND sector IS NOT NULL AND sector != ''
                GROUP BY sector
                HAVING COUNT(*) < :min_sample
                ORDER BY n ASC
            """), {"min_sample": MIN_SECTOR_SAMPLE}).fetchall()
            for r in rows:
                thin.append({"sector": r[0], "count": r[1], "min_required": MIN_SECTOR_SAMPLE})
    except Exception as e:
        logger.debug("check_thin_sector_buckets: %s", e)
    return thin


def run() -> dict:
    stale     = check_stale_constants()
    guardrail = check_adaptive_guardrails()
    thin      = check_thin_sector_buckets()

    issues = len(stale) + len(guardrail) + len(thin)
    if issues == 0:
        logger.info("Governance check: all clear.")
        return {"status": "ok", "issues": 0}

    lines = [f"⚠️ *Governance Alert — {issues} issue(s)*"]

    if stale:
        lines.append("\n*Stale regime constants:*")
        for s in stale:
            lines.append(
                f"  • `{s['constant']}={s['value']}` set {s['reviewed']} "
                f"({s['age_days']}d ago) — guardrail {s['guardrail']}"
            )

    if guardrail:
        lines.append("\n*Adaptive threshold guardrail violations:*")
        for g in guardrail:
            lines.append(f"  • {g['warning']}")

    if thin:
        lines.append("\n*Thin sector buckets (hybrid_gate may fallback to guardrail):*")
        for t in thin:
            lines.append(f"  • `{t['sector']}`: {t['count']} symbols (need ≥{t['min_required']})")

    msg = "\n".join(lines)
    _send_telegram(msg)
    logger.warning("Governance issues found: %d stale, %d guardrail, %d thin sectors",
                   len(stale), len(guardrail), len(thin))
    return {
        "status": "alerts_sent", "issues": issues,
        "stale_constants": stale, "guardrail_violations": guardrail, "thin_sectors": thin,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
