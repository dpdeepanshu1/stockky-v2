"""
tests/test_env_url_blank_fallback.py — blank-safe URL settings (decision-prediction-service).

`os.getenv(name, default)` only falls back to the default when the variable is UNSET. A blank Render
dashboard variable or a `NAME=` line in an env_file came through as "" / "   ", so the service called
"/path" instead of its real upstream. The settings below now go through `_env_url`, which treats
blank / whitespace-only values as unset and trims padded ones.

The checks are generic: every module-level setting built with `_env_url` is discovered from the module's
own AST, then the module is imported fresh under different environments —
  * blank ("", "   ", tab/newline) must give exactly the same values as UNSET,
  * a padded value must be trimmed (and, for rstrip=True settings, lose its trailing slash).
No raw `os.getenv(<service URL>, <default>)` may be left behind (regression guard).
Covers decision/, prediction/ and training/ (service root = two levels up).
Run from services/decision-prediction-service/decision:  python3 -m pytest tests/test_env_url_blank_fallback.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODULES = ['decision/main.py', 'prediction/main.py', 'training/app.py', 'training/trades.py']
_URL_NAMES = {
    "DECISION_PREDICTION_URL", "ANALYSIS_INTELLIGENCE_URL", "ANALYSIS_URL", "NOTIFICATION_SCHEDULER_URL",
    "DECISION_URL", "NOTIFICATION_URL", "NEWS_URL", "MARKET_DATA_URL", "TECHNICAL_URL", "FUNDAMENTAL_URL",
    "SCHEDULER_URL", "EVENT_URL", "PREDICTION_URL", "MARKET_SENTIMENT_URL", "TRAINING_URL",
    "TRAINING_SERVICE_URL", "MARKET_INDICES_URL", "QSTASH_URL", "EVENT_TRACKER_URL",
    "NOTIFICATION_SERVICE_URL", "SERVICE_URL",
}
_counter = itertools.count()


def _path(rel):
    return os.path.join(_ROOT, *rel.split("/"))


def _tree(rel):
    with open(_path(rel), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _env_url_calls(node):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_env_url"]


def _rstrip_of(call):
    for kw in call.keywords:
        if kw.arg == "rstrip" and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return True


def _sites(rel):
    """[(attr, env_name, rstrip)] for module-level `NAME = _env_url("ENV", ...)` assignments, plus every
    env name used by any `_env_url` call in the module (nested ones included)."""
    tree = _tree(rel)
    sites = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            calls = _env_url_calls(node.value)
            if calls:
                first = calls[0]                                  # ast.walk is breadth-first: outermost call
                sites.append((node.targets[0].id, first.args[0].value, _rstrip_of(first)))
    names = sorted({c.args[0].value for c in _env_url_calls(tree)})
    return sites, names


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh import state per test: nothing a loaded module imports (or writes to cwd) leaks out."""
    path_before, mods_before, cwd = list(sys.path), set(sys.modules), os.getcwd()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRAINING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_STORE_PATH", str(tmp_path))
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw:1")           # run_once.py requires it at import
    yield
    os.chdir(cwd)
    sys.path[:] = path_before
    for k in set(sys.modules) - mods_before:
        sys.modules.pop(k, None)


def _load(rel, monkeypatch, env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    before = set(sys.modules)
    sys.path.insert(0, _ROOT)
    sys.path.insert(0, os.path.dirname(_path(rel)))
    name = "_envurl_%d" % next(_counter)
    try:
        spec = importlib.util.spec_from_file_location(name, _path(rel))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    except ModuleNotFoundError as e:                              # a third-party dependency, not our code
        pytest.skip("optional dependency missing: %s" % e.name)
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
        for k in set(sys.modules) - before:                       # drop what the load imported
            sys.modules.pop(k, None)


@pytest.mark.parametrize("rel", _MODULES)
def test_discovery_finds_settings(rel):
    sites, names = _sites(rel)
    assert sites and names, f"{rel}: no _env_url settings found (module changed? update this test)"


@pytest.mark.parametrize("rel", _MODULES)
def test_blank_value_behaves_exactly_like_unset(rel, monkeypatch):
    sites, names = _sites(rel)
    base = _load(rel, monkeypatch, {n: None for n in names})
    want = {a: getattr(base, a) for a, _, _ in sites}
    for blank in ("", "   ", "\t\n"):
        mod = _load(rel, monkeypatch, {n: blank for n in names})
        got = {a: getattr(mod, a) for a, _, _ in sites}
        assert got == want, f"{rel}: blank {blank!r} differs from unset"


@pytest.mark.parametrize("rel", _MODULES)
def test_padded_value_is_trimmed(rel, monkeypatch):
    sites, names = _sites(rel)
    padded = {n: "  http://%s:1/ " % n.lower().replace("_", "-") for n in names}
    mod = _load(rel, monkeypatch, padded)
    for attr, env, rstrip in sites:
        raw = padded[env].strip()
        assert getattr(mod, attr) == (raw.rstrip("/") if rstrip else raw), f"{rel}: {attr}"


@pytest.mark.parametrize("rel", _MODULES)
def test_helper_semantics(rel, monkeypatch):
    f = _load(rel, monkeypatch, {"ZZ_TEST_URL": None})._env_url
    assert f("ZZ_TEST_URL", "http://d/") == "http://d"                      # default is rstripped when rstrip
    assert f("ZZ_TEST_URL", "http://d/", rstrip=False) == "http://d/"
    for raw in ("", "   ", "\t"):
        monkeypatch.setenv("ZZ_TEST_URL", raw)
        assert f("ZZ_TEST_URL", "http://d") == f("ZZ_TEST_URL", "http://d", rstrip=False) == "http://d"
    for raw in ("/", "  /  ", "///"):                                         # slash-only: blank only when rstripping
        monkeypatch.setenv("ZZ_TEST_URL", raw)
        assert f("ZZ_TEST_URL", "http://d") == "http://d"
        assert f("ZZ_TEST_URL", "http://d", rstrip=False) == raw.strip()
    monkeypatch.setenv("ZZ_TEST_URL", " http://h:1/// ")
    assert f("ZZ_TEST_URL", "x") == "http://h:1"
    assert f("ZZ_TEST_URL", "x", rstrip=False) == "http://h:1///"


@pytest.mark.parametrize("rel", _MODULES)
def test_no_raw_service_url_reads_with_defaults_remain(rel):
    offenders = []
    for n in ast.walk(_tree(rel)):
        if not isinstance(n, ast.Call) or len(n.args) < 2:
            continue
        f = n.func
        is_env = (isinstance(f, ast.Attribute) and f.attr in ("getenv", "get")
                  and ((isinstance(f.value, ast.Name) and f.value.id == "os")
                       or (isinstance(f.value, ast.Attribute) and f.value.attr == "environ")))
        a0 = n.args[0]
        if is_env and isinstance(a0, ast.Constant) and a0.value in _URL_NAMES:
            offenders.append((a0.value, n.lineno))
    assert not offenders, f"{rel}: use _env_url() for {offenders}"
