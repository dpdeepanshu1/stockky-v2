"""E30: reason + feeling notes for trades (orders/journal_notes.py, the /journal/notes routes, the manual-buy fields).

Needs no database and no network: the DB helpers are driven with a tiny in-memory fake session. The route wiring is checked from the source
(ast), so this file also runs where fastapi / sqlalchemy are not installed.
"""
import ast
import os
import types

from orders import journal_notes as jn

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _raises(fn, exc, contains=""):
    try:
        fn()
    except exc as e:
        assert contains in str(e), str(e)
        return
    raise AssertionError(f"{exc.__name__} not raised")


def _pos(i, status="TARGET_HIT", pnl=10.0, ws="MANUAL", msg=None):
    return types.SimpleNamespace(id=i, symbol=f"S{i}", window_source=ws, status=status, realized_pnl=pnl, error_message=msg, opened_at=None)


def _note(i, emotion, outcome, pnl, ws="MANUAL"):
    return types.SimpleNamespace(position_id=i, emotion=emotion, outcome=outcome, realized_pnl=pnl, window_source=ws)


# ---- input cleaning
def test_clean_emotion():
    assert jn.clean_emotion(None) is None
    assert jn.clean_emotion("  ") is None
    assert jn.clean_emotion(" FOMO ") == "fomo"
    _raises(lambda: jn.clean_emotion("happy"), jn.JournalInputError, "emotion must be one of")


def test_clean_reason():
    assert jn.clean_reason(None) is None and jn.clean_reason("   \n ") is None
    assert jn.clean_reason("  broke   day\nhigh ") == "broke day high"
    assert jn.clean_reason("x" * jn.MAX_REASON) == "x" * jn.MAX_REASON
    _raises(lambda: jn.clean_reason("x" * (jn.MAX_REASON + 1)), jn.JournalInputError, "too long")


# ---- stats
def test_summarize_counts_only_settled_trades_with_a_feeling():
    notes = [
        _note(1, "calm", "win", 100.0), _note(2, "calm", "loss", -40.0), _note(3, "fomo", "loss", -60.0),
        _note(4, "fomo", None, None),            # still open: not counted
        _note(5, "fomo", "pending", 5.0),        # waiting for its fill price: not counted
        _note(6, None, "win", 9.0),              # no feeling: not counted
        _note(7, "calm", "breakeven", 0.0),
    ]
    s = jn.summarize(notes)
    assert s["note_count"] == 7 and s["settled"] == 4
    by = {r["emotion"]: r for r in s["by_emotion"]}
    assert by["calm"]["trades"] == 3 and by["calm"]["wins"] == 1 and by["calm"]["win_rate"] == 0.333 and by["calm"]["pnl"] == 60.0
    assert by["fomo"]["trades"] == 1 and by["fomo"]["win_rate"] == 0.0 and by["fomo"]["risky"] is True and by["calm"]["risky"] is False
    assert by["calm"]["small_sample"] is True                  # under MIN_SAMPLE trades is always flagged
    assert [r["emotion"] for r in s["by_emotion"]] == ["calm", "fomo"]  # most trades first


def test_summarize_big_sample_is_not_flagged():
    s = jn.summarize([_note(i, "calm", "win" if i % 2 else "loss", 1.0) for i in range(jn.MIN_SAMPLE)])
    assert s["by_emotion"][0]["small_sample"] is False


def test_summarize_keys_do_not_clash_with_route_keys():
    """Regression: summarize() once returned "notes": <int>, which overwrote the route's "notes": [...] list when spread into the response."""
    s = jn.summarize([])
    for k in ("notes", "emotions", "max_reason", "manual_without_note"):
        assert k not in s, k
    assert s["by_emotion"] == [] and s["settled"] == 0 and s["note_count"] == 0


def test_untagged_manual():
    ps = [_pos(5), _pos(4, ws="5m"), _pos(3), _pos(2)]
    assert jn.untagged_manual(ps, [3]) == [5, 2]


# ---- DB helpers (fake session)
class _Col:
    def in_(self, ids):
        return set(ids)


class _Model:
    id = _Col()

    def __init__(self, position_id=None, symbol=None):
        self.position_id, self.symbol = position_id, symbol
        self.reason = self.emotion = self.window_source = self.opened_at = self.updated_at = self.status = self.outcome = self.realized_pnl = None


class _Q:
    def __init__(self, db, rows):
        self.db, self.rows = db, rows

    def filter_by(self, **kw):
        return _Q(self.db, [r for r in self.rows if all(getattr(r, k) == v for k, v in kw.items())])

    def filter(self, ids):
        return _Q(self.db, [r for r in self.rows if r.id in ids])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, notes=(), positions=()):
        self.notes, self.positions, self.commits, self.rollbacks, self.fail = list(notes), list(positions), 0, 0, False

    def query(self, model):
        if model is _Model:
            return _Q(self, self.notes)
        return _Q(self, self.positions)

    def add(self, row):
        self.notes.append(row)

    def delete(self, row):
        self.notes.remove(row)

    def commit(self):
        if self.fail:
            raise RuntimeError("db down")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_upsert_creates_updates_and_removes():
    db, p = _DB(), _pos(7, status="TARGET_HIT", pnl=12.5)
    row = jn.upsert(db, _Model, p, "breakout", "calm")
    assert row.reason == "breakout" and row.emotion == "calm" and row.symbol == "S7"
    assert (row.outcome, row.realized_pnl, row.status, row.window_source) == ("win", 12.5, "TARGET_HIT", "MANUAL")   # the snapshot is copied
    jn.upsert(db, _Model, p, "second thought", "fear")
    assert len(db.notes) == 1 and db.notes[0].emotion == "fear"                                                     # one row per position
    assert jn.upsert(db, _Model, p, None, None) is None and db.notes == []                                           # both blank = removed
    assert jn.upsert(db, _Model, p, None, None) is None                                                              # and removing nothing is fine


def test_snapshot_outcome_follows_trade_stats():
    assert jn.snapshot_fields(_pos(1, status="OPEN", pnl=None))["outcome"] is None
    assert jn.snapshot_fields(_pos(1, status="STOP_HIT", pnl=-3.0))["outcome"] == "loss"
    assert jn.snapshot_fields(_pos(1, status="EOD_SQUAREOFF", pnl=0.0, msg="EOD_SQUAREOFF_PENDING_RECONCILE"))["outcome"] == "pending"


def test_refresh_snapshots_updates_live_positions_only_and_never_raises():
    n_live, n_gone = _Model(7, "S7"), _Model(8, "S8")
    n_live.status, n_live.outcome = "OPEN", None
    n_gone.outcome, n_gone.realized_pnl = "win", 99.0
    db = _DB(notes=[n_live, n_gone], positions=[_pos(7, status="STOP_HIT", pnl=-5.0)])
    assert jn.refresh_snapshots(db, _Model, _Pos, [n_live, n_gone]) == 1
    assert (n_live.status, n_live.outcome, n_live.realized_pnl) == ("STOP_HIT", "loss", -5.0)
    assert (n_gone.outcome, n_gone.realized_pnl) == ("win", 99.0)       # the position row was cleaned up: the note keeps its own copy
    assert jn.refresh_snapshots(db, _Model, _Pos, [n_live, n_gone]) == 0  # nothing changed the second time
    db.fail = True
    n_live.outcome = "win"
    assert jn.refresh_snapshots(db, _Model, _Pos, [n_live]) == 0 and db.rollbacks == 1   # a failing commit is swallowed
    assert jn.refresh_snapshots(db, _Model, _Pos, []) == 0


class _Pos(_Model):
    pass


def test_note_dict():
    n = _Model(3, "S3")
    n.window_source, n.reason, n.emotion = "MANUAL", "r", "calm"
    d = jn.note_dict(n)
    assert d["manual"] is True and d["position_id"] == 3 and d["reason"] == "r" and d["opened_at"] is None


# ---- wiring (read from source)
def _src(name):
    with open(os.path.join(_HERE, name), encoding="utf-8") as f:
        return f.read()


def test_routes_and_model_are_wired():
    main = _src("main.py")
    tree = ast.parse(main)
    routes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("get", "put") and d.args:
                    routes[(d.func.attr, d.args[0].value)] = node
    put = routes[("put", "/journal/notes/{position_id}")]
    assert "require_admin" in ast.unparse(put.args), "saving a note must need the admin login"
    get = routes[("get", "/journal/notes")]
    assert "require_admin" not in ast.unparse(get.args)
    assert '"notes": [journal_notes.note_dict(n, iso_utc) for n in notes]' in main and "**summary" in main
    models = _src("models.py")
    assert "class ScalpTradeJournal(Base)" in models and '__tablename__ = "scalp_trade_journal"' in models
    assert "position_id = Column(Integer, nullable=False, unique=True, index=True)" in models   # one note per trade, no foreign key (retention deletes positions)
    assert "ForeignKey" not in models.split("class ScalpTradeJournal")[1]


def test_manual_buy_validates_before_ordering_and_never_fails_after_it():
    main = _src("main.py")
    i_clean = main.index("journal_notes.clean_reason(body.reason)")
    i_order = main.index("attempt_manual_entry(db, symbol, ltp, quantity=body.quantity)")
    i_save = main.index("journal_notes.upsert(db, ScalpTradeJournal, pos, _j_reason, _j_emotion)")
    assert i_clean < i_order < i_save                              # bad input is refused before any order; the note is saved after the order
    block = main[i_save - 400:i_save + 700]
    assert "except Exception" in block and "journal_saved = False" in block   # a note failure is reported, never raised


def test_journal_switch_is_wired_and_on_the_calibration_page():
    """E37: JOURNAL_NOTES_ENABLED defaults ON, is read at call time by all three entry points, and is a calibration switch."""
    cfg = _src("config.py")
    assert 'JOURNAL_NOTES_ENABLED = _get_bool("JOURNAL_NOTES_ENABLED", True)' in cfg
    main = _src("main.py")
    assert main.count("config.JOURNAL_NOTES_ENABLED") >= 3          # manual buy, PUT route, GET route
    put = main[main.index("def journal_put_note"):main.index("def journal_list_notes")]
    assert put.index("JOURNAL_NOTES_ENABLED") < put.index("journal_notes.upsert")   # refused before anything is written
    assert "status_code=409" in put[:put.index("journal_notes.upsert")]
    assert 'Flag("JOURNAL_NOTES_ENABLED"' in _src("control/catalog.py")
