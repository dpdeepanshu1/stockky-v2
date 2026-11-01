"""E277: the second half of the module-isolation pin. Runs after test_e277_module_isolation_a.py (alphabetical), which leaves stand-ins in sys.modules."""
import sys

_MARK = "_e277_leaked_stand_in"


def test_stand_ins_from_the_previous_module_are_gone():
    for name in ("config", "models"):
        assert not getattr(sys.modules.get(name), _MARK, False), f"{name}: a stand-in from the previous test module is still in sys.modules"


def test_config_is_the_real_module_again():
    import config
    assert hasattr(config, "__file__")
