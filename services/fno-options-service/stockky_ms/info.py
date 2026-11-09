"""stockky_ms.info - one small dict that says which engine a service is running (group403), for /health.

engine_version / schema_version : the constants in ms_types.
code_hash : first 8 hex of the sha1 of this copy's MANIFEST.sha256 (written by scripts/sync_market_structure.py, so every in-sync copy gives the
            SAME value; a hand-edited or stale copy gives another). None when there is no manifest (the shared/ source itself).
params_hash : only when the caller passes the MSParams it actually applies (the technical service does; the consumers never override them).
Never raises; a failure gives {"error": ...} so a /health probe can not fail because of it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from .ms_types import MS_ENGINE_VERSION, SCHEMA_VERSION


def code_hash() -> Optional[str]:
    try:
        mf = Path(__file__).resolve().parent / "MANIFEST.sha256"
        return hashlib.sha1(mf.read_bytes()).hexdigest()[:8] if mf.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def engine_info(params: Any = None) -> Dict[str, Any]:
    try:
        out: Dict[str, Any] = {"engine_version": MS_ENGINE_VERSION, "schema_version": SCHEMA_VERSION, "code_hash": code_hash()}
        if params is not None:
            from .params import MSParams, params_hash
            out["params_hash"] = params_hash(params)
        else:
            out["params_hash"] = None
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}
