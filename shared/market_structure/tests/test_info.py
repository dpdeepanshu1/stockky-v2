"""group403: engine_info() - the dict every service shows on /health so a deploy can confirm all of them run the same engine."""
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure import info
from market_structure.ms_types import MS_ENGINE_VERSION, SCHEMA_VERSION
from market_structure.params import MSParams, from_dict, params_hash


def test_engine_info_shape_and_versions():
    d = info.engine_info()
    assert d["engine_version"] == MS_ENGINE_VERSION and d["schema_version"] == SCHEMA_VERSION and d["params_hash"] is None and "code_hash" in d


def test_params_hash_is_reported_only_when_params_are_given_and_follows_overrides():
    assert info.engine_info(MSParams())["params_hash"] == params_hash(MSParams())
    assert info.engine_info(from_dict({"tick": 0.05}))["params_hash"] != info.engine_info(MSParams())["params_hash"]


def test_code_hash_follows_the_manifest_file():
    pkg = os.path.dirname(os.path.abspath(info.__file__))
    mf = os.path.join(pkg, "MANIFEST.sha256")
    existed = os.path.exists(mf)
    if existed:
        return                                   # a vendored copy: the real manifest decides; covered by sync_market_structure --check
    try:
        open(mf, "w").write("abc  x.py\n")
        a = info.code_hash()
        open(mf, "w").write("abd  x.py\n")
        assert a and info.code_hash() and a != info.code_hash()
    finally:
        os.remove(mf)
    assert info.code_hash() is None


def test_a_broken_params_object_gives_an_error_dict_not_an_exception():
    d = info.engine_info(object())
    assert "error" in d
