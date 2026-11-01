"""E272 round 5: the `setup` field on GET /positions and GET /trades/history rows (the Entry reason column).
Plain-function runner: python3 tests/test_e272_setup_row_field.py (also collected by pytest). main.py cannot be imported here (httpx / sqlalchemy), so
`_row_setup` is cut out of the source and run against the real setup_stamp module; the wiring is checked by source text."""
from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from orders import setup_stamp  # noqa: E402

_SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


def _load():
    tree = ast.parse(_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_row_setup")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["_row_setup"]


_row_setup = _load()


def _row(flow):
    return SimpleNamespace(flow_json=flow)


def test_stamped_row_returns_the_stamp_fields():
    st = setup_stamp.build("S2_PULLBACK", trigger="VWAP reclaim", confirmations=["volume"], penalties={"gap": 0.1}, size_mult=0.5,
                           now=datetime(2026, 11, 2, 9, 40), side="short")
    out = _row_setup(_row(setup_stamp.attach("{}", st)))
    assert out["id"] == "S2_PULLBACK" and out["trigger"] == "VWAP reclaim" and out["confirmations"] == ["volume"]
    assert out["penalties"] == {"gap": 0.1} and out["size_mult"] == 0.5 and out["slot"] == "0930" and out["side"] == "SHORT"


def test_legacy_stamp_and_other_keys_untouched():
    flow = setup_stamp.attach(json.dumps({"tape": {"x": 1}}), setup_stamp.build(now=datetime(2026, 11, 2, 9, 21)))
    out = _row_setup(_row(flow))
    assert out["id"] == setup_stamp.LEGACY_ID and out["slot"] == "0920"
    assert "tape" not in out                                            # only the stamp is exposed


def test_no_stamp_or_bad_json_is_none_never_raises():
    for flow in (None, "", "{}", "not json", "[]", '{"setup": 5}', '{"setup": {"trigger": "x"}}', 12345):
        assert _row_setup(_row(flow)) is None, flow
    assert _row_setup(object()) is None


def test_hostile_stamp_fields_are_bounded():
    flow = json.dumps({"setup": {"id": "X", "confirmations": [str(i) for i in range(50)], "penalties": "no", "slot": ""}})
    out = _row_setup(_row(flow))
    assert out is not None and len(out["confirmations"]) <= 10 and out["slot"] == "later"


def test_wired_into_both_routes():
    assert _SRC.count('"setup": _row_setup(r)') == 2
    pos = _SRC.index('@app.get("/positions")')
    hist = _SRC.index('@app.get("/trades/history")')
    first = _SRC.index('"setup": _row_setup(r)')
    second = _SRC.index('"setup": _row_setup(r)', first + 1)
    assert pos < first < hist < second                                  # one in /positions, one in /trades/history


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
