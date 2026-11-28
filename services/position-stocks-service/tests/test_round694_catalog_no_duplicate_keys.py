"""Round 694 (E716): the Position Stocks control catalog lists every switch once (UNIVERSE_TRIM_ENABLED had been pasted twice, which scripts/check_control_catalog.py fails). Source level."""
from __future__ import annotations

import ast
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_flag_key_appears_once():
    src = open(os.path.join(HERE, "control", "catalog.py"), encoding="utf-8").read()
    ast.parse(src)
    keys = re.findall(r'^\s+Flag\(\s*"([A-Z0-9_]+)"', src, flags=re.M)
    assert keys, "no Flag(...) entries found"
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, dup
