"""group322: money-path files may not gain a silent `except Exception: pass`.

Each remaining one is a dashboard progress note (pipeline_status), a db.rollback() after the real error was logged, or a
string-formatting fallback. A new one on an order / stop / sizing path would hide exactly the failure that matters, so the
count per file is pinned; raising it needs a deliberate edit here."""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAT = re.compile(r"^(\s*)except (Exception|BaseException)( as \w+)?:[^\n]*\n(\s*)pass\b[^\n]*\n", re.M)

ALLOWED = {
    "exit_engine/exit.py": 1,
    "entry_engine/entry.py": 3,
    "execution/auto_pilot.py": 1,
    "execution/reconcile.py": 0,
    "execution/dhan_client.py": 0,
    "manual_engine.py": 0,
    "risk_engine/engine.py": 0,
    "portfolio/portfolio.py": 0,
    "portfolio/broker_view.py": 0,
    "execution/equity_sync.py": 0,
    "cost_model.py": 0,
}


def test_silent_excepts_pinned():
    over = {}
    for rel, allowed in ALLOWED.items():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            n = len(_PAT.findall(f.read()))
        if n > allowed:
            over[rel] = (n, allowed)
    assert not over, f"new silent `except ...: pass` on a money path (found, allowed): {over}"
