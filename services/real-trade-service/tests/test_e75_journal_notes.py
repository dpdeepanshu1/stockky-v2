"""E75: reason + feeling notes for Real Trade positions (journal_notes.py, the /journal/notes/{mode} routes, the model, switch and catalog).

No database, no network: the DB helpers run on a tiny in-memory fake session; the route wiring is checked from the source (ast), so this file also
runs where fastapi / sqlalchemy are not installed.
"""
import ast
import os
import types

import journal_notes as jn

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _raises(fn, exc, contains=""):
    try:
        fn()
    except exc as e:
        assert contains in str(e), str(e)
        return
    raise AssertionError(f"{exc.__name__} not raised")


def _pos(i, status="CLOSED", pnl=10.0, tab=None, imported=False, mode="REAL"):
    return types.SimpleNamespace(id=i, mode=mode, symbol=f"S{i}", source_tab=tab, broker_imported=imported, status=status,
                                 realized_pnl=pnl, opened_at=None)


def test_clean_emotion_and_reason():
    assert jn.clean_emotion(None) is None and jn.clean_emotion("  ") is None
    assert jn.clean_emotion(" FOMO ") == "fomo"
    _raises(lambda: jn.clean_emotion("angry"), jn.JournalInputError, "emotion must be one of")
    assert jn.clean_reason("  a   b \n c ") == "a b c"
    assert jn.clean_reason("") is None
    _raises(lambda: jn.clean_reason("x" * 301), jn.JournalInputError, "too long")


def test_classify_only_closed_positions():
    assert jn.classify(_pos(1, pnl=5)) == "win"
    assert jn.classify(_pos(1, pnl=-5)) == "loss"
    assert jn.classify(_pos(1, pnl=0.001)) == "breakeven"
    assert jn.classify(_pos(1, status="OPEN", pnl=5)) is None
    assert jn.classify(_pos(1, status="PARTIALLY_CLOSED", pnl=5)) is None
    assert jn.classify(_pos(1, pnl=None)) is None


def test_manual_means_no_scan_tab_and_not_imported():
    assert jn.is_manual(_pos(1)) is True
    assert jn.is_manual(_pos(1, tab="board")) is False
    assert jn.is_manual(_pos(1, imported=True)) is False
    ps = [_pos(1), _pos(2, tab="board"), _pos(3), _pos(4, imported=True)]
    assert jn.untagged_manual(ps, [3]) == [1]


def test_summarize_counts_settled_noted_trades_only():
    N = types.SimpleNamespace
    notes = [N(outcome="win", emotion="calm", realized_pnl=100.0), N(outcome="loss", emotion="calm", realized_pnl=-40.0),
             N(outcome="loss", emotion="fomo", realized_pnl=-30.0), N(outcome=None, emotion="fomo", realized_pnl=0.0),
             N(outcome="win", emotion=None, realized_pnl=5.0)]
    s = jn.summarize(notes)
    assert s["note_count"] == 5 and s["settled"] == 3
    by = {r["emotion"]: r for r in s["by_emotion"]}
    assert by["calm"]["trades"] == 2 and by["calm"]["wins"] == 1 and by["calm"]["pnl"] == 60.0
    assert by["fomo"]["risky"] is True and by["calm"]["risky"] is False
    assert all(r["small_sample"] for r in s["by_emotion"])


class _Q:
    def __init__(self, rows): self.rows = rows
    def filter_by(self, **kw): return _Q([r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])
    def first(self): return self.rows[0] if self.rows else None


class _DB:
    def __init__(self): self.rows, self.commits = [], 0
    def query(self, model): return _Q(self.rows)
    def add(self, r): self.rows.append(r)
    def delete(self, r): self.rows.remove(r)
    def commit(self): self.commits += 1


class _Model(types.SimpleNamespace):
    def __init__(self, **kw): super().__init__(**kw)


def test_upsert_creates_updates_and_removes():
    db, pos = _DB(), _pos(7, pnl=12.0)
    row = jn.upsert(db, _Model, pos, "breakout", "calm")
    assert row.position_id == 7 and row.mode == "REAL" and row.manual is True and row.outcome == "win" and row.realized_pnl == 12.0
    row2 = jn.upsert(db, _Model, pos, "changed", None)
    assert row2 is row and row.reason == "changed" and row.emotion is None and len(db.rows) == 1
    assert jn.upsert(db, _Model, pos, None, None) is None and db.rows == []


def test_note_dict_shape():
    n = types.SimpleNamespace(position_id=1, symbol="S", mode="REAL", manual=True, reason="r", emotion="calm", status="CLOSED", outcome="win",
                              realized_pnl=3.0, opened_at=None, updated_at=None)
    d = jn.note_dict(n)
    assert d["mode"] == "REAL" and d["manual"] is True and d["opened_at"] is None and d["emotion"] == "calm"


def _tree(name):
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return ast.parse(f.read()), f.name


def test_routes_are_wired_and_admin_guarded():
    tree, _ = _tree("main.py")
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name, verb, path in (("journal_put_note", "put", "/journal/notes/{mode}/{position_id}"), ("journal_list_notes", "get", "/journal/notes/{mode}")):
        f = funcs[name]
        decos = [ast.unparse(d) for d in f.decorator_list]
        assert any(d.startswith(f"app.{verb}(") and path in d for d in decos), decos
        assert "require_admin_if_real" in ast.unparse(f.args), name


def test_model_switch_and_catalog_agree():
    import config
    assert config.JOURNAL_NOTES_ENABLED is True
    tree, _ = _tree("models.py")
    cls = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "TradeJournalNote"][0]
    cols = {t.id for n in cls.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert {"position_id", "mode", "reason", "emotion", "outcome", "realized_pnl", "manual"} <= cols
    with open(os.path.join(_HERE, "control", "catalog.py"), encoding="utf-8") as f:
        assert 'Flag("JOURNAL_NOTES_ENABLED"' in f.read()
