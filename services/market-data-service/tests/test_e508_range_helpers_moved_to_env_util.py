"""E508: the private helpers that already rejected an out-of-range value (and nothing else) now use env_util, so there is one rule for numeric settings.
`angelone_budget._env_float`, `news/feed_fetch._env_int` and `news/main._env_int` did the same thing as `env_float_safe` / `env_int_safe` with `lo=` / `hi=`.
1. none of them is defined any more; 2. every setting below is read through env_util with exactly the range written here;
3. behaviour: an out-of-range / text / blank value gives the default, a valid value passes unchanged.
Left alone on purpose: helpers that CLAMP instead of falling back (structure_refresh, sniper_observation, angelone_batch_404, live_write_retry: a clamp keeps
the operator's intent, a fall-back would change it), `md_guard.py` (shared by three sub-apps with no common env_util), api-gateway `main._env_int` (has a fallback name).
Same file in analysis-intelligence and market-data; it checks the files that exist in its own service."""
from __future__ import annotations

import ast
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = _HERE
while os.path.basename(os.path.dirname(_SERVICE)) != "services" and os.path.dirname(_SERVICE) != _SERVICE:
    _SERVICE = os.path.dirname(_SERVICE)
_SERVICES = os.path.dirname(_SERVICE)
_SVC = os.path.basename(_SERVICE)


def _r(lo, hi):
    return {"lo": lo, "hi": hi}


_TABLE = {
    "market-data-service/angelone_budget.py": {
        "ANGELONE_GLOBAL_COOLDOWN_S": _r(1, 600), "ANGELONE_CANDLE_COOLDOWN_BASE_S": _r(1, 3600), "ANGELONE_CANDLE_COOLDOWN_MAX_S": _r(1, 3600),
        "ANGELONE_GLOBAL_COOLDOWN_MAX_S": _r(1, 1800), "ANGELONE_LANE_RESERVE_CANDIDATE": _r(0, 0.9), "ANGELONE_LANE_RESERVE_BACKGROUND": _r(0, 0.9),
        "ANGELONE_CANDLE_SLOWDOWN_FACTOR": _r(0.05, 1), "ANGELONE_CANDLE_SLOWDOWN_S": _r(0, 86400), "ANGELONE_POSITION_PROBE_INTERVAL_S": _r(0, 600),
        "ANGELONE_POSITION_PROBE_MIN_AGE_S": _r(0, 600), "ANGELONE_POSITION_PROBE_BACKOFF_S": _r(0, 3600), "ANGELONE_POSITION_LANE_REFRESH_S": _r(0, 3600),
        "ANGELONE_HOT_DEMAND_WINDOW_S": _r(1, 3600)},
    "analysis-intelligence-service/news/feed_fetch.py": {
        "NEWS_FEED_TIMEOUT_SEC": _r(2, 30), "NEWS_FEED_CACHE_TTL_SEC": _r(0, 3600), "NEWS_FEED_BLOCKED_TTL_SEC": _r(60, 86400),
        "NEWS_FEED_STALE_DAYS": _r(0, 60), "NEWS_FEED_PRIMARY_DEAD_TTL_SEC": _r(0, 86400), "NEWS_FEED_DEAD_TTL_SEC": _r(0, 86400)},
    "analysis-intelligence-service/news/main.py": {"HF_MAX_CALLS_PER_HOUR": {"lo": 0}, "HF_MAX_CALLS_PER_DAY": {"lo": 0}},
}
_LEGACY = {"_env_float", "_env_int"}
_FN = {"env_int_safe", "env_float_safe"}


def _mine():
    return {p: t for p, t in _TABLE.items() if p.split("/", 1)[0] == _SVC and os.path.exists(os.path.join(_SERVICES, p))}


def _tree(rel):
    return ast.parse(open(os.path.join(_SERVICES, rel), encoding="utf-8").read())


def test_the_private_helpers_are_gone():
    left = [f"{rel}:{n.lineno} def {n.name}" for rel in _mine() for n in ast.walk(_tree(rel)) if isinstance(n, ast.FunctionDef) and n.name in _LEGACY]
    assert left == [], left


def test_every_setting_is_read_through_env_util_with_its_range():
    bad = []
    for rel, want in _mine().items():
        got = {}
        for n in ast.walk(_tree(rel)):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in _FN and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                    got.setdefault(n.args[0].value, []).append({k.arg: ast.literal_eval(k.value) for k in n.keywords if k.arg in ("lo", "hi", "positive")})
        for setting, kws in want.items():
            if setting not in got:
                bad.append(f"{rel}: {setting} not read through env_util")
            elif any(g != kws for g in got[setting]):
                bad.append(f"{rel}: {setting} has {got[setting]}, wanted {kws}")
    assert bad == [], bad


def test_out_of_range_setting_gives_the_default_and_a_valid_one_passes():
    rel = "market-data-service/angelone_budget.py"
    if rel not in _mine():
        return
    d = os.path.dirname(os.path.join(_SERVICES, rel))
    code = "import angelone_budget as b; print(b._cooldown_base_s(), b._lane_reserve if hasattr(b,'_lane_reserve') else '')"
    def run(v):
        e = {k: x for k, x in os.environ.items() if not k.startswith("ANGELONE_")}
        if v is not None:
            e["ANGELONE_GLOBAL_COOLDOWN_S"] = v
        e.update(PYTHONPATH=d, PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e, cwd=d, timeout=60)
        assert r.returncode == 0, r.stderr[-300:]
        return r.stdout.split()[0]
    assert run(None) == "30.0"
    for bad in ("0", "-5", "601", "abc", "nan", ""):
        assert run(bad) == "30.0", bad
    assert run("45") == "45.0"


if __name__ == "__main__":
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v(); print("ok  ", k)
            except Exception as e:
                fails += 1; print("FAIL", k, repr(e)[:300])
    sys.exit(1 if fails else 0)
