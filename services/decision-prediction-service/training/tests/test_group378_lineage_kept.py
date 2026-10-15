"""group378: a later decision of the day without lineage must not blank asof_date / params_hash already stored on a pending observation row."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import structure_obs as so  # noqa: E402
from test_group374_structure_obs import OBS, M, _DB  # noqa: E402


def test_null_lineage_does_not_overwrite_stored_lineage():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "params_hash": "d0042325"}])
    assert db.rows[0].asof_date == "2026-09-30" and db.rows[0].params_hash == "d0042325"
    so.upsert_batch(db, M, [{**OBS, "asof_date": None, "params_hash": None, "final_decision": "WAIT"}])
    row = db.rows[0]
    assert row.final_decision == "WAIT"                      # the newest decision still wins
    assert row.params_hash == "d0042325" and str(row.asof_date)[:10] == "2026-09-30"   # lineage kept


def test_new_lineage_still_replaces_old_lineage():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "params_hash": "old00000"}])
    so.upsert_batch(db, M, [{**OBS, "asof_date": "2026-10-01", "params_hash": "new11111"}])
    assert db.rows[0].params_hash == "new11111" and str(db.rows[0].asof_date)[:10] == "2026-10-01"
