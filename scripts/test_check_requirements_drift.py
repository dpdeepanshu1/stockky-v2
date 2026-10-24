"""Tests for scripts/check_requirements_drift.py (F33 part 2)."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("crd", Path(__file__).with_name("check_requirements_drift.py"))
crd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(crd)


def _tree(tmp_path, parent_txt, sub_txt, parent="analysis-intelligence-service", sub="technical", fname="requirements.txt"):
    d = tmp_path / "services" / parent
    (d / sub).mkdir(parents=True)
    (d / "requirements.txt").write_text(parent_txt)
    (d / sub / fname).write_text(sub_txt)
    return tmp_path


def test_parse_ignores_comments_options_and_extras():
    p = Path(__file__).parent / "_tmp_req.txt"
    p.write_text("# c\n-r other.txt\nuvicorn[standard]>=0.29  # x\nPy_JWT == 2.8.0\n\n")
    try:
        assert crd.parse(p) == {"uvicorn": ">=0.29", "py-jwt": "==2.8.0"}
    finally:
        p.unlink()


def test_same_pins_have_no_difference(tmp_path):
    root = _tree(tmp_path, "httpx==0.27.2\n", "httpx==0.27.2\n")
    assert crd.differences(root) == []


def test_different_pin_and_missing_package_are_found(tmp_path):
    root = _tree(tmp_path, "httpx==0.27.2\n", "httpx==0.25.2\nshap>=0.44\n")
    kinds = {(d[3], d[4]) for d in crd.differences(root)}
    assert kinds == {("httpx", "spec"), ("shap", "missing")}


def test_new_difference_fails_and_stale_baseline_fails(tmp_path):
    root = _tree(tmp_path, "httpx==0.27.2\n", "httpx==0.25.2\n", sub="brand-new")
    new, stale = crd.check(root)
    assert any("brand-new" in n for n in new)
    assert stale  # the real baseline entries do not exist in this fake tree


def test_the_repo_matches_its_baseline():
    new, stale = crd.check()
    assert new == [] and stale == []


def _svc(tmp_path, name, txt):
    d = tmp_path / "services" / name
    d.mkdir(parents=True)
    (d / "requirements.txt").write_text(txt)


def test_cross_difference_found_and_identical_pins_ignored(tmp_path):
    _svc(tmp_path, "a", "httpx==0.27.0\nsqlalchemy==2.0.31\n")
    _svc(tmp_path, "b", "httpx==0.25.2\nsqlalchemy==2.0.31\n")
    assert crd.cross_differences(tmp_path) == {"httpx": {"a": "==0.27.0", "b": "==0.25.2"}}


def test_cross_check_new_changed_and_fixed(tmp_path):
    import json
    _svc(tmp_path, "a", "httpx==0.27.0\n")
    _svc(tmp_path, "b", "httpx==0.25.2\n")
    bp = tmp_path / "base.json"
    bp.write_text(json.dumps(crd.cross_differences(tmp_path)))
    assert crd.cross_check(tmp_path, bp) == []
    (tmp_path / "services" / "b" / "requirements.txt").write_text("httpx==0.26.0\n")
    assert any(m.startswith("CHANGED") for m in crd.cross_check(tmp_path, bp))
    (tmp_path / "services" / "b" / "requirements.txt").write_text("httpx==0.27.0\n")
    assert any(m.startswith("FIXED") for m in crd.cross_check(tmp_path, bp))
    (tmp_path / "services" / "b" / "requirements.txt").write_text("httpx==0.25.2\nrequests==2.0\n")
    (tmp_path / "services" / "a" / "requirements.txt").write_text("httpx==0.27.0\nrequests==2.1\n")
    assert any(m.startswith("NEW") for m in crd.cross_check(tmp_path, bp))
