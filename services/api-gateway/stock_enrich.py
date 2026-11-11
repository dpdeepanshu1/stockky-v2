"""E387: support / resistance placeholders in the single-stock decision route, and how real technical levels replace them.

get_stock_decision fills `support` / `resistance` with close -3% / +3% when the decision reply has none, BEFORE it re-fetches technicals for a
failed technical pillar. The enrichment then only wrote a technical value into a key that was still None, so the real support / resistance never
replaced the placeholder - in exactly the case the re-fetch exists for. The placeholders are now remembered and may be replaced. Pure functions;
never raise."""
from __future__ import annotations

from typing import Any, Dict, Set

LEVEL_KEYS = ("support", "resistance")


def fill_placeholder_levels(result: Dict[str, Any]) -> Set[str]:
    """Fill missing support / resistance from close (-3% / +3%). Returns the keys that were filled (the placeholders)."""
    filled: Set[str] = set()
    try:
        c = float(result["close"])
    except (KeyError, TypeError, ValueError):
        return filled
    if result.get("support") is None:
        result["support"] = round(c * 0.97, 2)
        filled.add("support")
    if result.get("resistance") is None:
        result["resistance"] = round(c * 1.03, 2)
        filled.add("resistance")
    return filled


def apply_tech_fields(result: Dict[str, Any], td: Dict[str, Any], placeholders: Set[str]) -> None:
    """Copy the technical reply's fields into the decision result: only into empty keys, except a placeholder level, which a real one replaces."""
    for k in ("support", "resistance", "trend_strength", "volume_surge", "rsi", "close"):
        v = td.get(k)
        if v is None:
            continue
        if result.get(k) is None or (k in placeholders and k in LEVEL_KEYS):
            result[k] = v
            placeholders.discard(k)


def tech_refetch_needed(result: Dict[str, Any], pillar_failed: bool) -> bool:
    """E389: should the route re-fetch technicals? Yes when decide reported the pillar failed, or when it returned NO technical score at all.
    The old trigger was `technical_score is None and data_insufficient`, but the route had just set data_insufficient False for every reply with a
    close, so a missing score alone never re-fetched. A neutral 50 is a score, not a trigger (unchanged)."""
    try:
        return bool(pillar_failed) or result.get("technical_score") is None
    except Exception:  # noqa: BLE001
        return bool(pillar_failed)
