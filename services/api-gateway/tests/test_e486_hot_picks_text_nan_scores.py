"""E486: Hot Picks refresh (`stockky_hot_stocks`) failed outright on a text score and let a NaN score into the reply.

The score of a row comes from the last-scan seed (`combined_score or score`, stored under stockky:last_decision:<SYM>). E472 found text / NaN scores in
scan rows. `_rank` (nested in `stockky_hot_stocks`) did `-(x.get("score") or 0)`: a text score raised TypeError AFTER all the per-symbol news / event /
price work (nothing cached, the tab shows an error), and a NaN score ordered on NaN. `_build_hot_conviction_extra` copied a NaN `combined_score` /
`*_score` into the card row, where FastAPI cannot JSON-encode it. Now: `_hot_rank_items` (module level, reads scores with `_scan_num`), the seed score
is read with `_scan_num`, and a non-finite float is skipped in `_build_hot_conviction_extra`.

No fastapi needed (functions lifted from main.py by AST). Runs with pytest or `python3 tests/test_e486_...py`."""
from __future__ import annotations

import ast
import math
import os

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)
NAN, INF = float("nan"), float("inf")


def _exec(names, **extra):
    body = [n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in body} == set(names), set(names) - {n.name for n in body}
    ns = dict(math=math, **extra)
    exec(compile(ast.Module(body=body, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def _item(sym, score, **kw):
    d = {"symbol": sym, "decision": "PREPARE TO BUY", "score": score, "signal_strength": "high", "from_scan": True, "headline_count": 3}
    d.update(kw)
    return d


def _rank():
    return _exec({"_hot_float", "_scan_num", "_hot_rank_items"})["_hot_rank_items"]


def test_a_text_score_no_longer_raises_and_ranks_by_its_number():
    out = _rank()([_item("A", 60), _item("B", "72"), _item("C", 65.5)])
    assert [x["symbol"] for x in out] == ["B", "C", "A"]


def test_junk_and_nan_scores_rank_as_zero():
    out = _rank()([_item("A", "N/A"), _item("B", NAN), _item("C", None), _item("D", INF), _item("E", 1)])
    assert out[0]["symbol"] == "E" and {x["symbol"] for x in out} == {"A", "B", "C", "D", "E"}


def test_the_order_of_normal_rows_is_unchanged():
    items = [
        _item("LOW", 50, from_scan=False, signal_strength="medium"),
        _item("MID", 70, decision="BUY NOW"),
        _item("TOP", 80, decision="BUY NOW"),
        _item("HC", 70, decision="BUY NOW", headline_count=9),
        _item("DNB", 90, decision="DO NOT BUY"),
    ]
    assert [x["symbol"] for x in _rank()(items)] == ["TOP", "HC", "MID", "DNB", "LOW"]


def test_a_non_dict_entry_does_not_break_the_sort():
    assert len(_rank()([_item("A", 1), None])) == 2


def test_conviction_extra_skips_a_nan_or_inf_score():
    f = _exec({"_build_hot_conviction_extra"}, _estimate_holding_period=lambda *a: "Short")["_build_hot_conviction_extra"]
    out = f({"combined_score": 70.0}, {"combined_score": NAN, "fundamental_score": INF, "news_score": 55.5, "target": 120})
    assert out["combined_score"] == 70.0            # the NaN from the fuller record does not replace the seed's finite value
    assert "fundamental_score" not in out and out["news_score"] == 55.5 and out["target"] == 120
    assert "combined_score" not in f(None, {"combined_score": NAN})
    assert f(None, {"combined_score": 61, "technical_score": 0.0}) == {"combined_score": 61, "technical_score": 0.0}   # finite values (0.0 too) unchanged


def test_hot_stocks_uses_the_shared_ranker_and_reads_the_seed_score_safely():
    fn = next(n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "stockky_hot_stocks")
    src = ast.get_source_segment(_SRC, fn)
    assert "_rank = _hot_rank_items" in src and "def _rank(" not in src
    assert '_scan_num((decision_data or {}).get("score"))' in src


if __name__ == "__main__":
    import traceback
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except Exception:
                failed += 1
                print("FAIL", _n)
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
