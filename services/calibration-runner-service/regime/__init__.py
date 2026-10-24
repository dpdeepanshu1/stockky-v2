"""Market Weather runner package (Part 1 step 3). Pure computation: no trade, no switch, no network at import time.

  data.py        candles in, clean daily / intraday series out (and one small fetch helper for the scripts)
  features.py    numbers about the index and the VIX, using ONLY data up to a stated cut-off (no look-ahead)
  classifier.py  features -> one of calm / normal / choppy / stressed / panic, plus the reasons
  profiles.py    state -> the overlay entries a trading service may be asked to apply (tighten-only, clamped against the saved values)

Nothing here calls /control/overlay: that is step 4 (jobs, shadow) and step 6 (live).
"""
from __future__ import annotations

STATES = ("calm", "normal", "choppy", "stressed", "panic")   # same order and names as control/kit.py STATES
