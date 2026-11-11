"""E441 (round 422): NaN / inf never reach Hot Picks storage or its JSON response. Stand-alone: needs only hotpicks_store."""
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hotpicks_store as hs  # noqa: E402

NAN, INF = float("nan"), float("inf")


def test_num_treats_non_finite_as_no_value():
    for v in (NAN, INF, -INF, Decimal("NaN"), "nan", "inf"):
        assert hs._num(v) is None
    assert hs._num("3.5") == 3.5 and hs._num(0) == 0.0


def test_scrub_replaces_nested_non_finite_and_leaves_the_rest():
    src = {"a": NAN, "b": [1, INF, {"c": -INF, "d": "x"}], "e": (2.5, NAN), "f": None, "g": True}
    out = hs._scrub_non_finite(src)
    assert out == {"a": None, "b": [1, None, {"c": None, "d": "x"}], "e": [2.5, None], "f": None, "g": True}
    assert src["a"] != src["a"]                      # the input is not mutated
    json.dumps(out, allow_nan=False)                 # strict JSON accepts it


def test_dump_item_json_has_no_nan_token():
    raw = hs._dump_item_json({"score": NAN, "price": 10, "x": [INF]})
    assert "NaN" not in raw and "Infinity" not in raw and json.loads(raw) == {"score": None, "price": 10, "x": [None]}


def test_row_to_item_scrubs_a_nan_stored_before_the_fix():
    class HP:
        @staticmethod
        def coerce_bool(v):
            return bool(v)
    stored = '{"symbol": "A", "score": NaN, "news_score": 5}'      # what json.dumps wrote before E441
    item = hs._row_to_item({"item_json": stored, "symbol": "A", "section": "news_driven", "score": Decimal("NaN")}, HP)
    json.dumps(item, allow_nan=False, default=str)
    assert item["score"] is None and item["news_score"] == 5


def test_payload_rows_write_strict_json_and_a_null_typed_score():
    import types
    fake = types.SimpleNamespace(ROW_KEYS=("symbol", "section", "item_json"))
    hs._schema = lambda: fake
    rows = hs._payload_rows({"generated_at": "t", "news_driven": [{"symbol": "a", "score": NAN, "news_score": INF}]})
    assert rows[0]["score"] is None and rows[0]["news_score"] is None
    assert "NaN" not in rows[0]["item_json"] and "Infinity" not in rows[0]["item_json"]
