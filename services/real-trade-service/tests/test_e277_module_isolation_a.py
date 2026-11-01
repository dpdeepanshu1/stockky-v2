"""E277: the first half of the module-isolation pin (see conftest.py, _e277_restore_swapped_modules). This module swaps real modules for stand-ins and
leaves them swapped, like the older tests that caused the 4,025 setup errors. tests/test_e277_module_isolation_b.py runs after it and checks they are back."""
import sys
import types

_MARK = "_e277_leaked_stand_in"


def test_leave_stand_ins_behind():
    stand_in = types.SimpleNamespace(**{_MARK: True})
    sys.modules["config"] = stand_in
    sys.modules["models"] = stand_in
    assert getattr(sys.modules["config"], _MARK) is True
