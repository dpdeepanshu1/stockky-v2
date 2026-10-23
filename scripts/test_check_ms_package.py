"""E50: tests for scripts/check_ms_package.py (stdlib + pytest; the wheel build is exercised only when setuptools and wheel are installed)."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_ms_package", ROOT / "scripts" / "check_ms_package.py")
cmp = importlib.util.module_from_spec(_spec)
sys.modules["check_ms_package"] = cmp
_spec.loader.exec_module(cmp)


def _copy_repo_bits(tmp_path):
    (tmp_path / "shared").mkdir()
    shutil.copy(ROOT / "shared" / "pyproject.toml", tmp_path / "shared" / "pyproject.toml")
    shutil.copytree(ROOT / "shared" / "market_structure", tmp_path / "shared" / "market_structure", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    return tmp_path


def test_repo_is_clean():
    assert cmp.static_problems() == []


def test_missing_pyproject_is_reported(tmp_path):
    root = _copy_repo_bits(tmp_path)
    (root / "shared" / "pyproject.toml").unlink()
    assert cmp.static_problems(root) == ["shared/pyproject.toml is missing"]


def test_third_party_import_is_reported(tmp_path):
    root = _copy_repo_bits(tmp_path)
    (root / "shared" / "market_structure" / "bad.py").write_text("import numpy as np\n", encoding="utf-8")
    probs = cmp.static_problems(root)
    assert any("bad.py" in x and "numpy" in x for x in probs)


def test_dependencies_in_pyproject_are_reported(tmp_path):
    root = _copy_repo_bits(tmp_path)
    pp = root / "shared" / "pyproject.toml"
    pp.write_text(pp.read_text().replace("dependencies = []", 'dependencies = ["numpy"]'), encoding="utf-8")
    assert cmp.static_problems(root)


def test_wrong_package_dir_is_reported(tmp_path):
    root = _copy_repo_bits(tmp_path)
    pp = root / "shared" / "pyproject.toml"
    pp.write_text(pp.read_text().replace('stockky_ms = "market_structure"', 'stockky_ms = "elsewhere"'), encoding="utf-8")
    assert cmp.static_problems(root)


def test_wheel_builds_and_imports():
    pytest.importorskip("setuptools")
    pytest.importorskip("wheel")
    assert cmp.build_and_import() == []
    assert not (ROOT / "shared" / "build").exists() and not list((ROOT / "shared").glob("*.egg-info"))
