"""Group 349: pass the technical service's `market_structure` block / `structure_summary` through the gateway safely.

Rules: never raise; an unknown schema_version or a non-dict block is dropped (the caller gets the safe empty default, quality
'unavailable'); the flat `ms_*` summary is rebuilt from the block when the technical service did not send one. STRUCTURE_PASSTHROUGH=0
turns the whole thing off (existing payloads are then byte-for-byte what they were)."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

SUPPORTED_SCHEMA = 1

try:
    from stockky_ms.summary import SUMMARY_KEYS, flatten_summary
except Exception:  # noqa: BLE001 - package not vendored
    SUMMARY_KEYS = ()

    def flatten_summary(block):  # type: ignore
        return {"ms_quality": "unavailable"}


def enabled() -> bool:
    return (os.getenv("STRUCTURE_PASSTHROUGH") or "1").strip().lower() not in ("0", "false", "no", "off")


def safe_block(src: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The block when it is a dict with a supported schema_version, else None."""
    if not isinstance(src, dict):
        return None
    b = src.get("market_structure")
    if not isinstance(b, dict) or b.get("schema_version") != SUPPORTED_SCHEMA:
        return None
    return b


def safe_summary(src: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Flat ms_* summary: from the block when valid, else {ms_quality: 'unavailable', ...all None}."""
    try:
        b = safe_block(src)
        return flatten_summary(b)
    except Exception:  # noqa: BLE001
        return {"ms_quality": "unavailable"}


def merge_into(target: Dict[str, Any], src: Optional[Dict[str, Any]], with_block: bool = True) -> bool:
    """Copy block + summary from a technical response into `target` (a result / feed row). Returns True when a valid block was copied.
    Never overwrites an existing block with a worse one."""
    if not enabled() or not isinstance(target, dict):
        return False
    try:
        b = safe_block(src)
        if b is None:
            return False
        if with_block:
            target["market_structure"] = b
        target["structure_summary"] = flatten_summary(b)
        return True
    except Exception:  # noqa: BLE001
        return False


def feed_keys(src: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Only the non-None ms_* keys, for seeding a feed row without clobbering real values with nulls."""
    if not enabled():
        return {}
    return {k: v for k, v in safe_summary(src).items() if v is not None and k in SUMMARY_KEYS}
