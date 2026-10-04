"""group101 items 11 + 28: the stored `stockky:searched_symbols` list heals itself on read."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import main as m  # noqa: E402


class _Store:
    def __init__(self, value):
        self.value, self.writes = value, []

    def get(self, key):
        assert key == m.SEARCHED_KEY
        return self.value

    def set(self, key, val, ttl=None):
        assert key == m.SEARCHED_KEY
        self.writes.append(list(val))
        self.value = list(val)


def _patch(monkeypatch, value):
    st = _Store(value)
    monkeypatch.setattr(m, "_redis_get", st.get)
    monkeypatch.setattr(m, "_redis_set", st.set)
    return st


def test_clean_list_is_returned_untouched_and_not_rewritten(monkeypatch):
    st = _patch(monkeypatch, ["TCS", "INFY"])
    assert m._load_searched() == ["TCS", "INFY"]
    assert st.writes == []


def test_delisted_symbols_are_dropped_and_written_back(monkeypatch):
    st = _patch(monkeypatch, ["TCS", "AAKASH", "ANNAPURNA", "INFY"])
    assert m._load_searched() == ["TCS", "INFY"]
    assert st.writes == [["TCS", "INFY"]]
    assert m._load_searched() == ["TCS", "INFY"]      # healed: second read writes nothing
    assert len(st.writes) == 1


def test_padded_lowercase_dotted_duplicates_and_junk(monkeypatch):
    st = _patch(monkeypatch, [" tcs ", "TCS.NS", "infy.bo", None, 7, "", "  ", "HDFC BANK", "-"])
    assert m._load_searched() == ["TCS", "INFY"]
    assert st.writes == [["TCS", "INFY"]]


def test_typo_recorded_before_group93_is_replaced_by_the_real_symbol(monkeypatch):
    st = _patch(monkeypatch, ["HEROMOTORS", "TCS"])
    out = m._load_searched()
    assert "HEROMOTORS" not in out and "HEROMOTOCO" in out and "TCS" in out
    assert st.writes and "HEROMOTORS" not in st.writes[-1]


def test_non_equity_instruments_are_dropped(monkeypatch):
    _patch(monkeypatch, ["TCS", "RELIANCE30OCT26FUT", "INFY"])
    assert m._load_searched() == ["TCS", "INFY"]


def test_non_list_or_missing_value_gives_empty(monkeypatch):
    st = _patch(monkeypatch, {"oops": 1})
    assert m._load_searched() == []
    assert st.writes == []
    _patch(monkeypatch, None)
    assert m._load_searched() == []


def test_write_back_is_capped_at_200(monkeypatch):
    many = [f"SYM{i}" for i in range(250)] + ["AAKASH"]
    st = _patch(monkeypatch, many)
    out = m._load_searched()
    assert "AAKASH" not in out
    assert len(st.writes[-1]) == 200 and st.writes[-1][-1] == "SYM249"


def test_write_back_failure_is_not_fatal(monkeypatch):
    _patch(monkeypatch, ["TCS", "AAKASH"])

    def boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(m, "_redis_set", boom)
    assert m._load_searched() == ["TCS"]
