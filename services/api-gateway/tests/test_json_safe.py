"""json_safe.sanitize — NaN/Inf-safe conversion used for every FastAPI JSON response."""
import builtins
import json
import math

import numpy as np
import pytest

import json_safe


class TestScalars:
    @pytest.mark.parametrize("v", [None, 0, 1, -3, "text", "", True, False])
    def test_passthrough(self, v):
        assert json_safe.sanitize(v) is v or json_safe.sanitize(v) == v

    @pytest.mark.parametrize("v", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_python_float_becomes_none(self, v):
        assert json_safe.sanitize(v) is None

    @pytest.mark.parametrize("v", [0.0, -1.5, 1e308, 3.14])
    def test_finite_float_unchanged(self, v):
        assert json_safe.sanitize(v) == v


class TestContainers:
    def test_dict_recursion(self):
        assert json_safe.sanitize({"a": float("nan"), "b": {"c": float("inf"), "d": 2.0}}) == \
            {"a": None, "b": {"c": None, "d": 2.0}}

    def test_list_and_tuple_become_list(self):
        assert json_safe.sanitize([1.0, float("nan")]) == [1.0, None]
        assert json_safe.sanitize((1.0, float("-inf"))) == [1.0, None]

    def test_deeply_nested(self):
        out = json_safe.sanitize({"x": [{"y": (float("nan"), 5)}]})
        assert out == {"x": [{"y": [None, 5]}]}

    def test_result_is_strict_json_serialisable(self):
        payload = {"a": float("nan"), "b": [float("inf"), np.float64("nan"), np.int64(4)]}
        json.dumps(json_safe.sanitize(payload), allow_nan=False)  # must not raise


class TestNumpy:
    def test_numpy_float64_finite_stays_json_serialisable(self):
        # np.float64 subclasses float -> plain-float branch returns it unchanged (json.dumps accepts it)
        out = json_safe.sanitize(np.float64(2.5))
        assert out == 2.5 and isinstance(out, float)
        assert json.dumps(out) == "2.5"

    def test_numpy_float32_finite_is_converted_to_python_float(self):
        out = json_safe.sanitize(np.float32(2.5))
        assert out == 2.5 and type(out) is float

    def test_numpy_float32_finite(self):
        assert json_safe.sanitize(np.float32(1.5)) == 1.5

    @pytest.mark.parametrize("v", [np.float32("nan"), np.float32("inf"), np.float32("-inf")])
    def test_numpy_non_finite_float32_becomes_none(self, v):
        # np.float32 is NOT a subclass of Python float, so this exercises the numpy branch
        assert json_safe.sanitize(v) is None

    def test_numpy_integer_becomes_int(self):
        out = json_safe.sanitize(np.int64(7))
        assert out == 7 and type(out) is int

    def test_numpy_array_becomes_sanitized_list(self):
        assert json_safe.sanitize(np.array([1.0, np.nan, np.inf])) == [1.0, None, None]

    def test_numpy_2d_array(self):
        assert json_safe.sanitize(np.array([[1.0, np.nan], [2.0, 3.0]])) == [[1.0, None], [2.0, 3.0]]

    def test_numpy_float64_is_python_float_subclass(self):
        # np.float64 subclasses float, so it takes the plain-float branch
        assert json_safe.sanitize(np.float64("nan")) is None


class TestNumpyBool:
    @pytest.mark.parametrize("v,expected", [(np.bool_(True), True), (np.bool_(False), False)])
    def test_numpy_bool_becomes_python_bool(self, v, expected):
        out = json_safe.sanitize(v)
        assert out is expected and type(out) is bool

    def test_numpy_bool_nested_is_strict_json_serialisable(self):
        payload = {"ok": np.bool_(True), "rows": [{"flag": np.bool_(False)}], "t": (np.bool_(True),)}
        out = json_safe.sanitize(payload)
        assert out == {"ok": True, "rows": [{"flag": False}], "t": [True]}
        json.dumps(out, allow_nan=False)

    def test_numpy_bool_array_still_works(self):
        assert json_safe.sanitize(np.array([True, False])) == [True, False]

    def test_numpy_bool_from_comparison_of_numpy_values(self):
        # The realistic source: `np.float64(3) > np.float64(2)` is an np.bool_.
        out = json_safe.sanitize({"up": np.float64(3) > np.float64(2)})
        assert out == {"up": True} and type(out["up"]) is bool


class TestSets:
    def test_set_becomes_sorted_list(self):
        assert json_safe.sanitize({3, 1, 2}) == [1, 2, 3]
        assert json_safe.sanitize({"b", "a"}) == ["a", "b"]

    def test_frozenset_becomes_sorted_list(self):
        assert json_safe.sanitize(frozenset({"y", "x"})) == ["x", "y"]

    def test_empty_set(self):
        assert json_safe.sanitize(set()) == []

    def test_unorderable_members_fall_back_without_raising(self):
        out = json_safe.sanitize({1, "a", None})
        assert sorted(map(repr, out)) == sorted(map(repr, [1, "a", None]))
        assert isinstance(out, list)

    def test_members_are_sanitized_recursively(self):
        out = json_safe.sanitize({float("nan"), 1.0})
        assert sorted(out, key=lambda x: (x is None, x)) == [1.0, None]
        out = json_safe.sanitize({np.int64(5), np.int64(2)})
        assert out == [2, 5] and all(type(x) is int for x in out)

    def test_set_nested_in_dict_and_list_is_strict_json_serialisable(self):
        out = json_safe.sanitize({"tags": {"b", "a"}, "rows": [{"ids": frozenset({2, 1})}]})
        assert out == {"tags": ["a", "b"], "rows": [{"ids": [1, 2]}]}
        json.dumps(out, allow_nan=False)


class TestFallbacks:
    def test_unknown_object_returned_unchanged(self):
        o = object()
        assert json_safe.sanitize(o) is o

    def test_numpy_import_failure_returns_object_unchanged(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "numpy":
                raise ImportError("no numpy")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        o = object()
        assert json_safe.sanitize(o) is o
        assert json_safe.sanitize({"k": float("nan")}) == {"k": None}  # pure-python path still works
