"""
adaptive_market_params.py — 2026-09-11 addition.

User request: "calibrate more our above parameter for real trade service
for buying... set an adaptive threshold limit for all parameter not
static." adaptive_thresholds.py already solved exactly this problem for
ONE metric (ENTRY_REGIME_MIN_SCORE, from market_score history). This
module generalizes that same pattern — record a daily reading, compute a
trailing-window percentile once enough history exists, fall back to a
static constant until then — to any measured quantity, via a new
AdaptiveMetricSnapshot table (see models.py) instead of one bespoke table
per metric.

Currently drives:
  - CANDIDATE_MAX_ATR_PCT        (adaptive_max_atr_pct)
  - VOLUME_SHOCK_FUND/TECH_ABS_FLOOR  (adaptive_quality_floor)
  - CANDIDATE_MIN_MARKET_CAP_CR  (adaptive_min_market_cap_cr)
  - RSI oversold/overbought (technical service)  (adaptive_rsi_bounds)
  - "extended/chase-risk" return cutoffs (technical service)
    (adaptive_extension_thresholds)

LIVE, NOT A ONE-TIME SNAPSHOT: every value here is read fresh from the DB
on every call — nothing is cached/frozen in memory beyond one candidate-
refresh cycle (candidate_engine.candidates.py caches the current cycle's
values in a few module globals purely so ~10 nested check functions don't
each need a `db` argument threaded through them; that cache is
overwritten by _refresh_cycle_adaptive_params() at the START of every
single cycle, market hours only, for as long as the service keeps
running — see cycle_runner.py / execution/auto_pilot.py). The rolling
window itself (ADAPTIVE_HISTORY_DAYS, 90 by default) means each new
day's reading pushes the oldest day out automatically: this is NOT "seed
it with 30 days once and it's done" — record_metric() below both writes
today's reading AND prunes anything past the window on every single
call, so the distribution driving these thresholds is always exactly
"the last ~90 days as of right now," continuously, for as long as the
live pipeline keeps recording. If that pipeline ever stops (crash,
market holiday, service down), adaptive_params_status()'s
"data_freshness" section below will show it — these thresholds do NOT
silently keep using old data forever without that being visible.

CALIBRATION NOTE — read before changing the constants in config.py: this
sandbox has no access to NSE/Dhan historical market-data APIs (only pypi/
npm/github egress permitted here), so the INITIAL static fallback values
this module tilts around are grounded in verified CURRENT published
market data (see config.py's MIN_MARKET_CAP_CR_STATIC comment for the
specific figures — Nifty level/YTD return, India VIX vs its 52-week
range, NSE500 breadth) rather than a from-scratch backtest run in this
environment. That is intentionally a STARTING point. The whole reason
this module exists is that a hand-picked "backtested" number goes stale
the moment the regime changes — adaptive_thresholds.py's own docstring
already flagged this exact failure mode for the Aug-2026 constants
(correct in Aug, wrong by Sep because nobody re-ran the analysis). Every
function below instead self-recalibrates from the service's OWN live
operation once enough history accumulates, so it never goes stale the
same way.

FALLBACK GUARANTEE (same contract as adaptive_thresholds.py): every
function degrades to the static config.py constant when the DB is
unavailable, history is thin (<ADAPTIVE_MIN_HISTORY_DAYS distinct
calendar days), or anything raises. Never blocks the calling pipeline.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

import config
from models import AdaptiveMetricSnapshot, MarketRegimeHistory

logger = logging.getLogger("real-trade-adaptive-params")

# Reuse the same warm-up/window knobs adaptive_thresholds.py already
# exposes as env vars, rather than defining a second set with different
# names for what is conceptually the same "how much history before we
# trust it" decision.
from adaptive_thresholds import (  # noqa: E402
    ADAPTIVE_HISTORY_DAYS,
    ADAPTIVE_MIN_HISTORY_DAYS,
)


# ── Generic building block ─────────────────────────────────────────────────

def record_metric(db: Session, name: str, value: float) -> None:
    """Persist one metric reading, then prune anything older than the
    adaptive window (+5-day buffer) for that same metric so the table
    never grows unbounded — this must be self-contained rather than a
    separate call site some caller has to remember to invoke: this
    codebase's own adaptive_thresholds.py defines an equivalent
    _prune_old_scores() for MarketRegimeHistory that is, as of this
    writing, never actually called anywhere — exactly the kind of gap
    that quietly turns "adaptive" into "whatever got recorded once."
    Best-effort — never raises, mirrors
    adaptive_thresholds.record_market_score."""
    try:
        db.add(AdaptiveMetricSnapshot(metric_name=name, value=float(value)))
        db.commit()
    except Exception as e:
        logger.debug("record_metric(%s) failed (non-fatal): %s", name, e)
        try:
            db.rollback()
        except Exception:
            pass
        return
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ADAPTIVE_HISTORY_DAYS + 5)
        db.query(AdaptiveMetricSnapshot).filter(
            AdaptiveMetricSnapshot.metric_name == name,
            AdaptiveMetricSnapshot.recorded_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        logger.debug("record_metric(%s) prune failed (non-fatal): %s", name, e)
        try:
            db.rollback()
        except Exception:
            pass


def _readings_and_distinct_days(db: Session, name: str, history_days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
    rows = (
        db.query(AdaptiveMetricSnapshot.value, AdaptiveMetricSnapshot.recorded_at)
        .filter(
            AdaptiveMetricSnapshot.metric_name == name,
            AdaptiveMetricSnapshot.recorded_at >= cutoff,
        )
        .all()
    )
    values = [float(r.value) for r in rows]
    distinct_days = len({r.recorded_at.date() for r in rows})
    return values, distinct_days


def adaptive_percentile_value(
    db: Session,
    metric_name: str,
    static_default: float,
    percentile: float,
    guardrail_min: float,
    guardrail_max: float,
    history_days: int = ADAPTIVE_HISTORY_DAYS,
    min_history_days: int = ADAPTIVE_MIN_HISTORY_DAYS,
) -> tuple[float, str]:
    """
    value = the `percentile`th percentile of the trailing `history_days`
    readings of `metric_name`, clamped to [guardrail_min, guardrail_max].
    Falls back to static_default until min_history_days distinct calendar
    days of data exist. Returns (value, source) — same contract as
    adaptive_thresholds.adaptive_regime_threshold.
    """
    try:
        values, distinct_days = _readings_and_distinct_days(db, metric_name, history_days)
        if distinct_days < min_history_days:
            return static_default, "static"
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        idx = max(0, min(n - 1, int(n * percentile / 100.0) - 1))
        raw = sorted_vals[idx]
        clamped = max(guardrail_min, min(guardrail_max, raw))
        source = f"adaptive_{n}r_{distinct_days}d_p{int(percentile)}"
        return round(clamped, 3), source
    except Exception as e:
        logger.debug(
            "adaptive_percentile_value(%s) error, falling back to static: %s",
            metric_name, e,
        )
        return static_default, "static"


# ── Specific parameters ──────────────────────────────────────────────────

def adaptive_max_atr_pct(db: Session) -> tuple[float, str]:
    """
    CANDIDATE_MAX_ATR_PCT, adaptively. Uses the 80th percentile of the
    scan universe's OWN observed pre-shock ATR% (recorded once per cycle
    by candidate_engine.candidates._refresh_volume_shock_candidates) over
    the trailing window, instead of a fixed 7.0%.
    Rationale: in a genuinely more volatile regime, average ATR% across
    NSE names rises across the board — a frozen 7% cap then starts
    rejecting setups that are simply normal for that regime. In a calmer
    regime it should tighten back down rather than keep admitting 7%-ATR
    names just because it always has. Guardrails (4%-12%) keep it from
    ever loosening to the point of not meaning anything, or tightening so
    far that ordinary midcap volatility gets rejected outright.
    """
    return adaptive_percentile_value(
        db, "universe_atr_pct",
        static_default=config.CANDIDATE_MAX_ATR_PCT,
        percentile=80.0,
        guardrail_min=4.0,
        guardrail_max=12.0,
    )


def _current_market_score(db: Session) -> tuple[float, str]:
    """
    A genuine "how is the market doing right now" reading, distinct from
    adaptive_thresholds.adaptive_regime_threshold — that function returns
    the 20th-percentile ENTRY GATE BAR computed from score history (i.e.
    "how good must the market be to allow entries"), not the current score
    itself; reusing it here as a stand-in for "current regime strength"
    would be a category error (the gate bar tracks recent history in the
    same direction as the current score, but is not that reading).
    Mean of the last 3 recorded market_score readings — a few readings
    smooths single-cycle noise (record_market_score is called on every
    regime-cache refresh, roughly every 2-3 minutes, so 3 readings is
    recent, not stale) without needing its own separate history/warm-up
    contract. Falls back to config.ENTRY_REGIME_MIN_SCORE (the same
    constant adaptive_thresholds.py itself falls back to) when nothing has
    been recorded yet, with source "static" — note this makes the "no
    data yet" fallback deliberately CAUTIOUS rather than neutral (25 is
    the entry gate's own minimum-acceptable score, well below the 50
    midpoint used below), matching this module's general asymmetry
    preference for erring toward tighter floors, not looser ones, when
    genuinely uncertain.
    """
    try:
        rows = (
            db.query(MarketRegimeHistory.score)
            .order_by(MarketRegimeHistory.recorded_at.desc())
            .limit(3)
            .all()
        )
        if not rows:
            return float(config.ENTRY_REGIME_MIN_SCORE), "static"
        scores = [float(r.score) for r in rows]
        return sum(scores) / len(scores), f"latest_{len(scores)}r_avg"
    except Exception as e:
        logger.debug("_current_market_score error, falling back to static: %s", e)
        return float(config.ENTRY_REGIME_MIN_SCORE), "static"


def _regime_tilt(db: Session, weak_bonus: float, strong_penalty: float) -> tuple[float, str]:
    """
    Shared regime-read step for the two functions below — both want "tilt
    up when the regime is weak, tilt down when it's strong," just with
    different magnitudes, so the read (and its source label) is computed
    once here rather than duplicated. market_score in practice ranges
    roughly 0-100 (see api-gateway's /market/indices) — 50 is treated as
    the neutral midpoint (an even/flat session), not 40: unlike
    adaptive_regime_threshold's percentile-of-history bar, this reads the
    raw score itself, whose own natural midpoint is 50.
    """
    score, source = _current_market_score(db)
    delta = 50 - score  # positive when regime is weak
    if delta >= 0:
        tilt = min(weak_bonus, delta * (weak_bonus / 30.0))
    else:
        tilt = max(-strong_penalty, delta * (strong_penalty / 30.0))
    return tilt, source


def adaptive_quality_floor(db: Session, base_static: float) -> tuple[float, str]:
    """
    fund/tech absolute floor (VOLUME_SHOCK_FUND_ABS_FLOOR /
    VOLUME_SHOCK_TECH_ABS_FLOOR), regime-tilted: raise the bar when the
    broader market regime is weak (fewer names deserve the benefit of the
    doubt), relax it when it's strong. +8 max tilt in a weak regime, -5 max
    in a strong one — deliberately asymmetric: a false-negative (missing a
    good setup in a weak market) is cheaper than a false-positive (buying
    a bad one), so the floor should raise faster than it relaxes.
    """
    tilt, source = _regime_tilt(db, weak_bonus=8.0, strong_penalty=5.0)
    value = max(20.0, min(55.0, base_static + tilt))
    return round(value, 1), f"regime_tilt({source})"


def adaptive_min_market_cap_cr(db: Session) -> tuple[float, str]:
    """
    Market-cap floor (config.MIN_MARKET_CAP_CR_STATIC base), regime-tilted
    the same direction as adaptive_quality_floor — tighter (favor larger,
    more liquid names) when the regime is weak, looser (let genuine
    midcap/smallcap setups through — the outperformance this file's Aug-28
    header already documented) when it's strong. Never below
    config.MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR regardless of regime — that
    one is a structural liquidity floor, not a tactical dial.
    """
    tilt, source = _regime_tilt(db, weak_bonus=2500.0, strong_penalty=1000.0)
    value = max(
        config.MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR,
        config.MIN_MARKET_CAP_CR_STATIC + tilt,
    )
    return round(value, 0), f"regime_tilt({source})"


def adaptive_rsi_bounds(db: Session) -> tuple[float, float, str]:
    """
    2026-09-11 addition. RSI oversold/overbought bounds, shifted with the
    live regime instead of frozen at 30/70 — a well-documented real
    technical-analysis adjustment (Constance Brown's "RSI range shift"):
    in an uptrend RSI tends to oscillate in a HIGHER band (commonly
    ~40-80, rarely dropping below 40) because pullbacks stay shallow; in
    a downtrend it oscillates in a LOWER band (~20-60, rarely exceeding
    60) because rallies stay shallow. A permanently frozen 30/70 band is
    really only "correct" for a genuinely range-bound/neutral market —
    which is precisely the case adaptive_thresholds.py's own docstring
    already flagged as the failure mode of hand-picked static constants.
    Guardrails keep the shift from ever fully inverting the classic
    bounds: oversold stays within 15-40, overbought within 60-85.
    """
    score, source = _current_market_score(db)
    delta = score - 50.0  # positive = bullish tilt, negative = bearish
    shift = max(-15.0, min(15.0, delta * (15.0 / 50.0)))
    oversold = max(15.0, min(40.0, 30.0 + shift))
    overbought = max(60.0, min(85.0, 70.0 + shift))
    return round(oversold, 1), round(overbought, 1), f"regime_tilt({source})"


def adaptive_extension_thresholds(db: Session) -> tuple[float, float, str]:
    """
    2026-09-11 addition. "Extended / chase-risk" return cutoffs — frozen
    at >18% over ~1 month and >5% over ~3 sessions in the technical
    service — scaled by the SAME observed universe ATR% distribution
    adaptive_max_atr_pct already tracks (no separate history table
    needed). Rationale: in a higher-volatility regime a bigger single-
    name move is unremarkable and shouldn't trip a "chase risk" flag
    every time; in a calm regime a smaller move is genuinely more
    unusual. Scales both cutoffs by the ratio of the currently-adaptive
    ATR cap to its own static baseline, clamped to a sane 0.6x-1.6x range
    so a single noisy reading can't wildly distort the cutoff.
    """
    atr_val, atr_src = adaptive_max_atr_pct(db)
    ratio = atr_val / config.CANDIDATE_MAX_ATR_PCT if config.CANDIDATE_MAX_ATR_PCT else 1.0
    ratio = max(0.6, min(1.6, ratio))
    extended_1m = max(0.10, min(0.30, 0.18 * ratio))
    extended_short = max(0.03, min(0.09, 0.05 * ratio))
    return round(extended_1m, 3), round(extended_short, 3), f"atr_scaled({atr_src})"


def adaptive_signal_weights(db: Session) -> tuple[float, float, str]:
    """
    2026-09-11 gap-closure addition ("EMA/MACD/ADX/Bollinger Band logic
    itself is still static"). Rather than inventing new absolute
    thresholds for indicators that are inherently structural (EMA stack
    alignment is a yes/no condition, not a number with a "floor"), this
    scales HOW MUCH each signal family counts toward technical_score,
    based on a genuine measured "is the market trending or range-bound
    right now" reading — average ADX across the quality-gate's own
    scanned batch (recorded once per cycle as "universe_adx" by
    candidate_engine.candidates, the same self-recording pattern as
    universe_atr_pct).
    This is real, standard technical-analysis practice: trend-following
    signals (EMA stack alignment, MACD crossover) are more reliable in a
    trending tape; mean-reversion signals (RSI extremes, Bollinger Band
    edge-touches) are more reliable in a range-bound one. A fixed +15/+12/
    +8 point scheme applies full trend-following weight even in a dead-
    flat market and full mean-reversion weight even in a strong trend —
    exactly the "static regardless of what the market is actually doing"
    problem this whole module exists to fix.
    Returns (trend_weight, meanrev_weight, source) — multipliers applied
    to the EMA-stack/MACD score deltas and the RSI/Bollinger-Band score
    deltas respectively in technical/main.py. 1.0 = unchanged from today's
    fixed scheme (this is also the correct static fallback with no
    history). ADX below the classic Wilder "weak trend" line (20) is
    read as range-bound; above the classic "strong trend" line (25) as
    trending — those two constants themselves are left untouched (see
    this module's docstring for why: they're well-established, and
    "trendiness" itself, unlike a return-percentage cutoff, doesn't
    obviously scale with the CANDIDATE_MAX_ATR_PCT-style ATR distribution
    the way extension thresholds do).
    """
    try:
        values, distinct_days = _readings_and_distinct_days(db, "universe_adx", ADAPTIVE_HISTORY_DAYS)
        if distinct_days < ADAPTIVE_MIN_HISTORY_DAYS or not values:
            return 1.0, 1.0, "static"
        avg_adx = sum(values) / len(values)
        if avg_adx >= 25.0:
            # Trending: up to +30% trend-following weight, down to -20%
            # mean-reversion weight, scaling further as ADX rises past 25.
            strength = min(1.0, (avg_adx - 25.0) / 20.0)  # saturates ~ADX 45
            trend_w = 1.0 + 0.3 * strength
            meanrev_w = 1.0 - 0.2 * strength
        elif avg_adx <= 20.0:
            # Range-bound: reverse tilt, up to +30% mean-reversion weight.
            strength = min(1.0, (20.0 - avg_adx) / 15.0)  # saturates ~ADX 5
            trend_w = 1.0 - 0.2 * strength
            meanrev_w = 1.0 + 0.3 * strength
        else:
            trend_w, meanrev_w = 1.0, 1.0
        source = f"adaptive_{len(values)}r_{distinct_days}d_avgadx{avg_adx:.0f}"
        return round(trend_w, 3), round(meanrev_w, 3), source
    except Exception as e:
        logger.debug("adaptive_signal_weights error, falling back to static: %s", e)
        return 1.0, 1.0, "static"


def market_cap_tier(market_cap_cr: float | None) -> str:
    """Approximate tier label for display/logging — see config.py's
    MARKET_CAP_*_CR comment for why these are approximate, not SEBI's
    exact rank-based bands."""
    if market_cap_cr is None:
        return "unknown"
    if market_cap_cr >= config.MARKET_CAP_LARGE_CR:
        return "large"
    if market_cap_cr >= config.MARKET_CAP_MID_CR:
        return "mid"
    if market_cap_cr >= config.MARKET_CAP_SMALL_CR:
        return "small"
    return "micro"


# ── Full status snapshot (mirrors adaptive_thresholds.adaptive_status) ────

def _latest_reading_age_hours(db: Session, metric_name: str) -> float | None:
    """Hours since the most recent reading of `metric_name` — the
    freshness signal referenced in this module's docstring. None if
    nothing has ever been recorded."""
    try:
        row = (
            db.query(AdaptiveMetricSnapshot.recorded_at)
            .filter(AdaptiveMetricSnapshot.metric_name == metric_name)
            .order_by(AdaptiveMetricSnapshot.recorded_at.desc())
            .first()
        )
        if not row:
            return None
        last = row.recorded_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - last).total_seconds() / 3600.0, 1)
    except Exception:
        return None


def adaptive_params_status(db: Session) -> dict:
    """Status dict for a dashboard/diagnostics endpoint — same shape
    spirit as adaptive_thresholds.adaptive_status(), plus a
    data_freshness section: this is the answer to "how do I know this
    isn't just frozen on whatever data it started with" — if the live
    cycle stops recording, last_universe_atr_pct_reading_hours_ago (and
    the equivalent for market_score, surfaced by /adaptive/status
    already) will climb and stale=true will flip, rather than the
    adaptive values just silently going quiet while still LOOKING active.
    """
    atr_val, atr_src = adaptive_max_atr_pct(db)
    fund_val, fund_src = adaptive_quality_floor(db, config.VOLUME_SHOCK_FUND_ABS_FLOOR)
    tech_val, tech_src = adaptive_quality_floor(db, config.VOLUME_SHOCK_TECH_ABS_FLOOR)
    mcap_val, mcap_src = adaptive_min_market_cap_cr(db)
    rsi_os, rsi_ob, rsi_src = adaptive_rsi_bounds(db)
    ext_1m, ext_short, ext_src = adaptive_extension_thresholds(db)
    trend_w, meanrev_w, weight_src = adaptive_signal_weights(db)

    def _history_note(metric_name: str) -> dict:
        values, distinct_days = _readings_and_distinct_days(db, metric_name, ADAPTIVE_HISTORY_DAYS)
        return {
            "readings": len(values),
            "distinct_days": distinct_days,
            "min_days_needed": ADAPTIVE_MIN_HISTORY_DAYS,
            "active": distinct_days >= ADAPTIVE_MIN_HISTORY_DAYS,
        }

    atr_age_hrs = _latest_reading_age_hours(db, "universe_atr_pct")
    # >48h stale during a week with trading days is a real gap (a single
    # weekend is ~60h but this is checked live during market hours, when
    # the most recent prior trading session's reading is always <48h old
    # unless something actually broke) — not a hard alarm threshold, just
    # a visible signal rather than silence.
    stale = atr_age_hrs is not None and atr_age_hrs > 48.0

    return {
        "candidate_max_atr_pct": {
            "value": atr_val, "source": atr_src,
            "static_fallback": config.CANDIDATE_MAX_ATR_PCT,
            "history": _history_note("universe_atr_pct"),
        },
        "volume_shock_fund_abs_floor": {
            "value": fund_val, "source": fund_src,
            "static_fallback": config.VOLUME_SHOCK_FUND_ABS_FLOOR,
        },
        "volume_shock_tech_abs_floor": {
            "value": tech_val, "source": tech_src,
            "static_fallback": config.VOLUME_SHOCK_TECH_ABS_FLOOR,
        },
        "min_market_cap_cr": {
            "value": mcap_val, "source": mcap_src,
            "static_fallback": config.MIN_MARKET_CAP_CR_STATIC,
            "absolute_floor": config.MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR,
        },
        "rsi_bounds": {
            "oversold": rsi_os, "overbought": rsi_ob, "source": rsi_src,
            "static_fallback": {"oversold": 30.0, "overbought": 70.0},
        },
        "extension_thresholds": {
            "extended_1m_pct": ext_1m, "extended_short_pct": ext_short, "source": ext_src,
            "static_fallback": {"extended_1m_pct": 0.18, "extended_short_pct": 0.05},
        },
        "signal_weights": {
            "trend_weight": trend_w, "meanrev_weight": meanrev_w, "source": weight_src,
            "static_fallback": {"trend_weight": 1.0, "meanrev_weight": 1.0},
            "history": _history_note("universe_adx"),
        },
        "data_freshness": {
            "last_universe_atr_pct_reading_hours_ago": atr_age_hrs,
            "stale": stale,
            "note": (
                "If this is null, the live cycle has never recorded this metric yet "
                "(fresh deploy — expected). If it's a large, growing number during "
                "market hours, the candidate-refresh cycle has stopped running and "
                "these values are NOT updating from live data anymore — check "
                "cycle_runner.py / execution/auto_pilot.py, don't just trust the "
                "'value' fields above at face value."
            ),
        },
        "advice": (
            "These self-calibrate from the service's own recorded history once "
            "30 distinct trading days of readings exist per metric; until then "
            "each one uses its static config.py fallback, tilted by the same "
            "regime read as the entry gate (see adaptive_thresholds.py)."
        ),
    }
