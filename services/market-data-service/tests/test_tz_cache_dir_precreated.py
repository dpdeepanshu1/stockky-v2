"""
tests/test_tz_cache_dir_precreated.py

Group 123: yfinance logged "Failed to create TzCache folder '/tmp/yfinance_tz' ... [Errno 17]
File exists" three times at boot (concurrent first Ticker calls racing on mkdir). main.py
creates the directory itself with exist_ok=True BEFORE pointing yfinance at it. This is a
source-level guard (no yfinance/fastapi import needed) so the order cannot silently regress.

Run:  cd services/market-data-service && python -m pytest tests/test_tz_cache_dir_precreated.py -v
"""
import ast
import os

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _calls(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            out.append((node.lineno, name, node))
    return out


def test_makedirs_exist_ok_runs_before_set_tz_cache_location():
    tree = ast.parse(open(MAIN, encoding="utf-8").read())
    calls = _calls(tree)
    makedirs = [
        (ln, n) for ln, name, n in calls
        if name == "makedirs" and any(k.arg == "exist_ok" and getattr(k.value, "value", None) is True
                                      for k in n.keywords)
    ]
    setters = [ln for ln, name, _ in calls if name == "set_tz_cache_location"]
    assert setters, "main.py no longer points yfinance at a tz cache dir"
    assert makedirs, "tz cache dir is not pre-created with exist_ok=True (boot race returns)"
    assert min(ln for ln, _ in makedirs) < min(setters)


def test_tz_cache_dir_is_env_overridable():
    src = open(MAIN, encoding="utf-8").read()
    assert 'os.getenv("YF_TZ_CACHE_DIR", "/tmp/yfinance_tz")' in src
