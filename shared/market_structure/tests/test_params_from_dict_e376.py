"""E376: STRUCTURE_PARAMS_JSON overrides are coerced safely. A quoted "false" must not turn a flag on, and NaN / Infinity (json.loads accepts both)
must not become a threshold, because every comparison with NaN is False and the rule would silently stop working."""
import json

from market_structure.params import MSParams, from_dict, params_hash


def test_string_booleans():
    assert from_dict({"bos_close_required": "false"}).bos_close_required is False
    assert from_dict({"bos_close_required": "0"}).bos_close_required is False
    assert from_dict({"bos_close_required": "No"}).bos_close_required is False
    assert from_dict({"bos_close_required": "true"}).bos_close_required is True
    assert from_dict({"bos_close_required": False}).bos_close_required is False
    assert from_dict({"bos_close_required": "maybe"}).bos_close_required is True      # unreadable -> default kept (True)


def test_non_finite_numbers_are_ignored():
    d = MSParams()
    for bad in ('{"bos_min_atr": NaN}', '{"bos_min_atr": Infinity}', '{"bos_min_atr": -Infinity}', '{"bos_min_atr": "nan"}', '{"swing_left": NaN}'):
        p = from_dict(json.loads(bad))
        assert p.bos_min_atr == d.bos_min_atr and p.swing_left == d.swing_left, bad


def test_normal_overrides_and_defaults_unchanged():
    assert from_dict(None) == MSParams() and params_hash(from_dict({})) == params_hash(MSParams())
    p = from_dict({"bos_min_atr": 0.2, "swing_left": "3", "unknown": 1, "swing_right": True})
    assert p.bos_min_atr == 0.2 and p.swing_left == 3 and p.swing_right == MSParams().swing_right
