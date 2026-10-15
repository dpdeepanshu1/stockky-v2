"""group401: sector name -> Yahoo sector-index symbol, for the sector-relative returns in training/structure_obs.py.

Pure, stdlib only, never raises. The sector labels in this repo come from several sources (Yahoo style "Technology" / "Financial Services",
Indian style "IT" / "PHARMA" / "Banks"), so the match is on whole words of the lowercased label. A label that does not match anything gives None
and the sector return stays unknown (None, never 0 and never the Nifty return). The index symbols below are Yahoo's usual NSE sector tickers and
could NOT be checked offline. Broad Yahoo sectors with no clean NSE index ("Consumer Cyclical", "Basic Materials", "Industrials") are left unmapped on purpose, because a loose proxy would give a misleading relative return: a ticker Yahoo does not serve simply yields no bars, so the sector columns stay empty. Override or extend with
STRUCTURE_OBS_SECTOR_INDEX_JSON, e.g. {"banks": "^NSEBANK", "telecom": "^CNXMEDIA"}.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional, Tuple

# (words that must appear in the label, symbol) - first match wins, so the specific entries come first.
_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("psu", "bank"), "^CNXPSUBANK"),
    (("bank",), "^NSEBANK"), (("banks",), "^NSEBANK"), (("banking",), "^NSEBANK"),
    (("financial",), "^CNXFIN"), (("finance",), "^CNXFIN"), (("nbfc",), "^CNXFIN"), (("insurance",), "^CNXFIN"),
    (("it",), "^CNXIT"), (("information", "technology"), "^CNXIT"), (("technology",), "^CNXIT"), (("software",), "^CNXIT"),
    (("auto",), "^CNXAUTO"), (("automobile",), "^CNXAUTO"), (("automobiles",), "^CNXAUTO"),
    (("pharma",), "^CNXPHARMA"), (("pharmaceuticals",), "^CNXPHARMA"), (("healthcare",), "^CNXPHARMA"), (("health",), "^CNXPHARMA"),
    (("fmcg",), "^CNXFMCG"), (("consumer", "defensive"), "^CNXFMCG"),
    (("metal",), "^CNXMETAL"), (("metals",), "^CNXMETAL"),
    (("energy",), "^CNXENERGY"), (("oil",), "^CNXENERGY"), (("power",), "^CNXENERGY"), (("utilities",), "^CNXENERGY"),
    (("realty",), "^CNXREALTY"), (("real", "estate"), "^CNXREALTY"),
    (("infra",), "^CNXINFRA"), (("infrastructure",), "^CNXINFRA"),
    (("media",), "^CNXMEDIA"), (("communication", "services"), "^CNXMEDIA"),
)


def _words(label: str) -> set:
    return set(re.findall(r"[a-z]+", label.lower()))


def _overrides() -> Dict[str, str]:
    try:
        raw = (os.getenv("STRUCTURE_OBS_SECTOR_INDEX_JSON") or "").strip()
        d = json.loads(raw) if raw else {}
        return {str(k).strip().lower(): str(v).strip() for k, v in d.items() if str(k).strip() and str(v).strip()} if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def clean_label(sector: object) -> Optional[str]:
    """The stored sector label: stripped text of at most 50 characters, or None for blank / non-text."""
    try:
        if not isinstance(sector, str):
            return None
        s = " ".join(sector.split())[:50]
        return s or None
    except Exception:  # noqa: BLE001
        return None


def index_for(sector: object) -> Optional[str]:
    """Yahoo index symbol for a sector label, or None when unknown. Never raises."""
    try:
        label = clean_label(sector)
        if not label:
            return None
        ov = _overrides()
        if label.lower() in ov:
            return ov[label.lower()]
        w = _words(label)
        for need, sym in _RULES:
            if all(n in w for n in need):
                return sym
        return None
    except Exception:  # noqa: BLE001
        return None
