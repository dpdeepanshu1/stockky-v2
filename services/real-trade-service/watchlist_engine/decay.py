"""
watchlist_engine/decay.py — Short-Term Trading Upgrade (2026-09-02)

Central config: catalyst type → holding horizon, decay speed, and how far
price is allowed to have moved from the catalyst print before entry_engine
refuses the trade (entry-side), plus per-horizon ATR trailing schedules
for exit_engine (exit-side).

Both sides live in this one file so the horizon taxonomy is defined once.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── Entry-side profiles ──────────────────────────────────────────────────────
# decay_half_life_days: how fast the catalyst edge fades.
# entry_band_pct:       max allowed move from catalyst_price before the
#                       entry-trigger pass marks the entry "missed".
# expires_at is set to 3× half-life — long enough to catch the drift,
# short enough to stop trading a stale catalyst.

CATALYST_PROFILES: dict[str, dict] = {
    "ipo":          {"horizon_class": "short", "decay_half_life_days": 2,  "entry_band_pct": 0.04},
    "bulk_block":   {"horizon_class": "short", "decay_half_life_days": 4,  "entry_band_pct": 0.05},
    "insider":      {"horizon_class": "short", "decay_half_life_days": 5,  "entry_band_pct": 0.05},
    "results":      {"horizon_class": "mid",   "decay_half_life_days": 12, "entry_band_pct": 0.07},
    "board":        {"horizon_class": "mid",   "decay_half_life_days": 10, "entry_band_pct": 0.06},
    "volume_shock": {"horizon_class": "short", "decay_half_life_days": 2,  "entry_band_pct": 0.03},
}

DEFAULT_CATALYST_PROFILE: dict = {
    "horizon_class": "short",
    "decay_half_life_days": 3,
    "entry_band_pct": 0.04,
}


def profile_for(catalyst_type: str) -> dict:
    """Return the entry-side profile for the given catalyst type."""
    return CATALYST_PROFILES.get(catalyst_type, DEFAULT_CATALYST_PROFILE)


def expiry_from(catalyst_ts: datetime, catalyst_type: str) -> datetime:
    """
    Return the expiry timestamp for a watchlist entry.
    Uses 3× the half-life so a slow-mover (results) still gets enough window,
    but a fast-mover (ipo / volume_shock) doesn't linger for weeks.
    """
    profile = profile_for(catalyst_type)
    if catalyst_ts.tzinfo is None:
        catalyst_ts = catalyst_ts.replace(tzinfo=timezone.utc)
    return catalyst_ts + timedelta(days=profile["decay_half_life_days"] * 3)


# ── Exit-side profiles ───────────────────────────────────────────────────────
# Keyed by horizon_class (same classes used entry-side).
# trail_atr_schedule mirrors exit.py's existing TRAIL_ATR_SCHEDULE shape
# (list of (max_days, multiplier) tuples) — drop-in override, not a new format.
# breakeven_atr_trigger, max_hold_days, early_warn_days, partial_exit_fraction
# map 1-to-1 to exit.py's module-level constants.

EXIT_PROFILES: dict[str, dict] = {
    "short": {
        # ipo, bulk_block, insider, volume_shock — catalyst fades fast.
        # Tighten trail and time-stop sooner so we lock gains before edge decays.
        "trail_atr_schedule":    [(1, 2.0), (3, 1.5), (99, 1.0)],
        "breakeven_atr_trigger": 0.75,   # lock free-ride sooner
        "max_hold_days":         5,
        "early_warn_days":       3,
        "partial_exit_fraction": 0.60,
    },
    "mid": {
        # results, board — slower drift, worth more patience.
        # Give the move room to develop; let more ride in partial exits.
        "trail_atr_schedule":    [(5, 2.0), (12, 1.5), (99, 1.0)],
        "breakeven_atr_trigger": 1.0,
        "max_hold_days":         20,
        "early_warn_days":       12,
        "partial_exit_fraction": 0.50,
    },
}

# Safe default: use the shorter leash when origin is unknown (manual trades
# that have no watchlist_entry_id fall back to exit.py's own module constants,
# never this dict — this default is only for the edge case where a row exists
# but horizon_class is unrecognised).
DEFAULT_EXIT_PROFILE: dict = EXIT_PROFILES["short"]


def exit_profile_for(horizon_class: str | None) -> dict:
    """
    Return the exit-side profile for the given horizon_class.
    Returns DEFAULT_EXIT_PROFILE for None or unrecognised values.
    """
    return EXIT_PROFILES.get(horizon_class or "", DEFAULT_EXIT_PROFILE)
