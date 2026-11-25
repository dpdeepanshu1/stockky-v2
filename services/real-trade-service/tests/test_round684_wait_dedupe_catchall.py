"""Round 684: the before_flush catch-all applies the dedupe rule to WAIT rows that bypassed divert_pending_waits."""
import wait_dedupe as wd


class TradeDecision:                      # name matters: the hook only looks at TradeDecision objects
    def __init__(self, action="WAIT", decision_type="ENTRY", symbol="ABC", reasoning="structure blocked", id=None):
        self.action, self.decision_type, self.symbol, self.id = action, decision_type, symbol, id
        self.mode, self.gate_tag, self.reasoning, self.risk_verdict, self.risk_verdict_reason = "REAL", "structure", reasoning, None, None


class _Session:
    def __init__(self, *rows):
        self.new, self.expunged = list(rows), []

    def expunge(self, o):
        self.expunged.append(o)
        self.new.remove(o)


def _on(monkeypatch, minutes="15"):
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", minutes)
    monkeypatch.delenv("WAIT_DEDUPE_CATCHALL", raising=False)
    wd.reset_for_tests()


def test_repeat_is_dropped_first_is_kept(monkeypatch):
    _on(monkeypatch)
    a, b = TradeDecision(), TradeDecision()
    s = _Session(a, b)
    wd._before_flush(s)
    assert s.new == [a] and s.expunged == [b]
    st = wd.status()
    assert st["unchecked"] == 2 and st["caught_dropped"] == 1


def test_checked_row_is_not_touched(monkeypatch):
    _on(monkeypatch)
    a = TradeDecision()
    wd.is_repeat(a)
    s = _Session(a)
    wd._before_flush(s)
    assert s.new == [a] and wd.status()["unchecked"] == 0


def test_enter_exit_and_rows_with_id_ignored(monkeypatch):
    _on(monkeypatch)
    rows = [TradeDecision(action="ENTER"), TradeDecision(decision_type="EXIT"), TradeDecision(id=7)]
    s = _Session(*rows)
    wd._before_flush(s)
    assert s.new == rows and wd.status()["unchecked"] == 0


def test_kill_switch_and_window_zero(monkeypatch):
    _on(monkeypatch)
    monkeypatch.setenv("WAIT_DEDUPE_CATCHALL", "off")
    s = _Session(TradeDecision(), TradeDecision())
    wd._before_flush(s)
    assert len(s.new) == 2 and wd.status()["unchecked"] == 0
    monkeypatch.delenv("WAIT_DEDUPE_CATCHALL")
    monkeypatch.setenv("WAIT_DEDUPE_MINUTES", "0")
    wd._before_flush(s)
    assert len(s.new) == 2


def test_never_raises_on_broken_session(monkeypatch):
    _on(monkeypatch)
    wd._before_flush(object())
    wd._before_flush(None)


def test_db_pool_recycle_default_and_floor(monkeypatch):
    import ast, pathlib
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("db.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_pool_recycle")
    ns = {}
    exec(compile(ast.Module([node], []), "db", "exec"), ns)
    f = ns["_pool_recycle"]
    monkeypatch.delenv("RT_DB_POOL_RECYCLE", raising=False)
    assert f() == 1800
    monkeypatch.setenv("RT_DB_POOL_RECYCLE", "10")
    assert f() == 60
    monkeypatch.setenv("RT_DB_POOL_RECYCLE", "abc")
    assert f() == 1800
    monkeypatch.setenv("RT_DB_POOL_RECYCLE", "900")
    assert f() == 900
