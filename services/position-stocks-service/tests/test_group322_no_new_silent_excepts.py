"""group322: position-stocks order paths may not gain a silent `except Exception: pass` (see the real-trade twin test)."""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAT = re.compile(r"^(\s*)except (Exception|BaseException)( as \w+)?:[^\n]*\n(\s*)pass\b[^\n]*\n", re.M)
# orders/reconcile.py: one db.rollback() after the error is logged; orders/charges_ledger.py: two rollbacks after logger.exception
ALLOWED = {"orders/reconcile.py": 1, "orders/charges_ledger.py": 2}


def test_silent_excepts_pinned():
    over = {}
    for path in glob.glob(os.path.join(ROOT, "orders", "*.py")) + [os.path.join(ROOT, "execution", "dhan_client.py")]:
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            n = len(_PAT.findall(f.read()))
        if n > ALLOWED.get(rel, 0):
            over[rel] = (n, ALLOWED.get(rel, 0))
    assert not over, f"new silent `except ...: pass` on an order path (found, allowed): {over}"
