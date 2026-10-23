"""control/env_view.py (E51): a read-only view of os.environ as module attributes. The scheduler reads PATTERN_ALERTS_* / WATCHLIST_MOVE_* from the
environment at CALL time, and control/kit.py writes os.environ when a switch is set, so a change is live at once. The kit reads / mirrors a switch through a
(module, attribute) target; this module is that target. An unset variable raises AttributeError (the page then shows the shipped default)."""
import os


def __getattr__(name: str):
    v = (os.environ.get(name) or "").strip()
    if not v:
        raise AttributeError(name)
    return v
