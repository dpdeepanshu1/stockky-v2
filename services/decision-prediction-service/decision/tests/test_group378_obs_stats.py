"""group378: structure-observation skip counters (decision/structure_obs_hook.py) and the lineage kept in the training snapshot."""
from __future__ import annotations

import ast
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import structure_obs_hook as h  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 15, 11, 0, tzinfo=IST)
GOOD = {"decision": "WAIT", "close": 100.0, "structure": {"mode": "full", "features": {"quality": "ok", "asof": "2026-10-14", "ver": "1.0.0", "hash": "d0042325"}}}


def test_payload_carries_asof_and_hash_and_counts_built():
    h._reset_for_tests()
    p = h.build_payload("INFY", GOOD, NOW)
    assert p["asof_date"] == "2026-10-14" and p["params_hash"] == "d0042325" and p["engine_version"] == "1.0.0"
    s = h.stats()
    assert s["considered"] == 1 and s["built"] == 1 and s["usable_share"] == 1.0


def test_every_skip_reason_is_counted():
    h._reset_for_tests()
    assert h.build_payload("A", {"decision": "WAIT"}, NOW) is None                                   # no structure block
    assert h.build_payload("A", {"structure": GOOD["structure"]}, NOW) is None                         # no decision
    assert h.build_payload("A", {"decision": "WAIT", "structure": {"features": {"quality": "unavailable"}}}, NOW) is None
    p = h.build_payload("A", GOOD, NOW)
    assert h.should_send(p) is True and h.should_send(p) is False                                      # second is a dedupe
    s = h.stats()
    assert (s["considered"], s["skip_no_structure_block"], s["skip_no_decision"], s["skip_unusable_quality"], s["built"], s["skip_deduped"]) == (4, 1, 1, 1, 1, 1)
    assert s["usable_share"] == 0.25


def test_count_ignores_unknown_keys_and_never_raises():
    h._reset_for_tests()
    h.count("nope")
    h.count("queued", 3)
    assert h.stats()["queued"] == 3 and "nope" not in h.stats()


def test_training_snapshot_keeps_hash_and_endpoint_is_wired():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    assert '"asof", "ver", "hash")' in src                                   # _structure_snapshot keep tuple
    assert any(isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "structure_observation_stats" for n in ast.walk(tree))
    assert '@app.get("/structure/observation-stats")' in src and '"cache_served"' in src and '"queued"' in src
