"""E366: plain_short's release queue and orphan watch list are saved in scalp_pending_claims and reloaded after a restart (behaviour: scripts/test_plain_short.py, E366 checks)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS = (ROOT / "orders" / "plain_short.py").read_text(encoding="utf-8")
CS = (ROOT / "orders" / "claim_store.py").read_text(encoding="utf-8")
MODELS = (ROOT / "models.py").read_text(encoding="utf-8")


def test_model_is_a_new_table_with_a_string_key():
    assert 'class ScalpPendingClaim(Base)' in MODELS and '__tablename__ = "scalp_pending_claims"' in MODELS
    assert 'key = Column(String(64), primary_key=True)' in MODELS


def test_store_functions_never_raise_and_roll_back():
    for fn in ("def save(", "def forget(", "def load_all("):
        body = CS[CS.index(fn):]
        assert "except Exception" in body.split("\ndef ")[0] and "_rollback(db)" in body.split("\ndef ")[0]


def test_guard_reloads_first_and_every_exit_of_an_orphan_forgets_it():
    g = PS[PS.index("def run_plain_short_guard"):]
    assert g.index("load_persisted(db)") < g.index("retry_pending_releases(db)")
    assert "_orphans.pop(" not in PS.replace("o = _orphans.pop(oid, None)", "")


def test_short_live_passes_its_session_so_the_items_are_saved():
    sl = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")
    assert sl.count("db=db)") >= 4 and "watch_orphan(" in sl
