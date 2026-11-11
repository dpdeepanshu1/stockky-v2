"""E425: Hot Picks price / score repair rewrites item_json only. updated_at is the scan time (freshness, stored_at, the repair windows),
so a repair must not reset it."""
from __future__ import annotations

import ast
import os

_STORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hotpicks_store.py")


def _update_sql_strings():
    src = open(_STORE, encoding="utf-8").read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            text = ast.get_source_segment(src, node) or ""
            if "UPDATE {hp.TABLE_NAME}" in text:
                out.append(text)
    return out


def test_both_repair_updates_set_item_json_only():
    stmts = _update_sql_strings()
    assert len(stmts) >= 2
    for s in stmts:
        assert "SET item_json = :item_json" in s and "updated_at" not in s and "now_func" not in s


def test_the_store_no_longer_calls_now_func():
    assert ".now_func(" not in open(_STORE, encoding="utf-8").read()
