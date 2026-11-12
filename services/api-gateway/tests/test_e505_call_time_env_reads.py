"""E505: numeric settings read while a code path RUNS (inside a function, not at import) no longer fail on a typo or a blank value, and the two shared
byte-identical files (session_proof_client.py, boot_forensics.py) no longer stop a service from importing on a typo.
Same file in every service; it finds its own service folder.
1. no bare `int(...)` / `float(...)` of an environment read is left anywhere in the service (PORT, and conversions wrapped by a catching try or by `_num(...)`, excepted);
2. every file that calls env_int_safe / env_float_safe imports it;
3. the shared session_proof_client / boot_forensics copies are byte-identical to api-gateway's and survive junk / blank / nan settings with their defaults, and still honour a valid value;
4. where the module imports here: event_feed_check.cmd_dryrun and rl_explore.EpsilonGreedyBandit survive junk settings.
Runs with pytest or `python3 tests/test_e505_call_time_env_reads.py`."""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = _HERE
while os.path.basename(os.path.dirname(_SERVICE)) != "services" and os.path.dirname(_SERVICE) != _SERVICE:
    _SERVICE = os.path.dirname(_SERVICE)
_SERVICES = os.path.dirname(_SERVICE)
_SKIPDIRS = {"tests", "__pycache__", "node_modules", ".git", "venv", ".venv", "site-packages"}
_SHARED = {"session_proof_client.py": [("SESSION_PROOF_CLIENT_TTL_S", "_TTL_S", 30.0), ("SESSION_PROOF_CLIENT_TIMEOUT_S", "_TIMEOUT_S", 2.0)],
           "boot_forensics.py": [("BOOT_FORENSICS_HEARTBEAT_S", "_HEARTBEAT_S", 15.0), ("BOOT_FORENSICS_MEM_WARN_PCT", "_MEM_WARN_PCT", 85.0)]}


def _py_files():
    for cur, dirs, files in os.walk(_SERVICE):
        dirs[:] = [d for d in dirs if d not in _SKIPDIRS]
        for f in sorted(files):
            if f.endswith(".py") and not f.startswith("test_"):
                yield os.path.join(cur, f)


def _catches(h):
    if h.type is None:
        return True
    names = [ast.unparse(x) for x in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])]
    return any(n in ("Exception", "BaseException", "ValueError", "TypeError") for n in names)


def _bare_reads(path):
    src = open(path, encoding="utf-8").read()
    out = []

    def visit(n, prot):
        if isinstance(n, ast.Try):
            pr = prot or any(_catches(h) for h in n.handlers)
            for c in n.body:
                visit(c, pr)
            for h in n.handlers:
                for c in h.body:
                    visit(c, prot)
            for c in n.orelse + n.finalbody:
                visit(c, prot)
            return
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("int", "float") and n.args and not prot:
            seg = ast.get_source_segment(src, n) or ""
            if re.search(r"getenv|environ", seg) and '"PORT"' not in seg and "_num(" not in seg:
                out.append(f"{os.path.relpath(path, _SERVICE)}:{n.lineno}: {seg[:90]}")
        for c in ast.iter_child_nodes(n):
            visit(c, prot)

    visit(ast.parse(src), False)
    return out


def test_no_bare_numeric_env_read_is_left():
    out = []
    for p in _py_files():
        if os.path.basename(p) != "env_util.py":
            out += _bare_reads(p)
    assert out == [], out


def test_every_user_of_the_safe_helpers_imports_them():
    for p in _py_files():
        if os.path.basename(p) == "env_util.py":
            continue
        src = open(p, encoding="utf-8").read()
        for name in ("env_int_safe", "env_float_safe"):
            if re.search(r"\b" + name + r"\(", src):
                assert re.search(r"\bimport\b[^\n]*\b" + name + r"\b", src), (p, name)


def _shared_copies():
    for p in _py_files():
        if os.path.basename(p) in _SHARED:
            yield p


def test_shared_copies_are_identical_to_the_gateways():
    for p in _shared_copies():
        ref = open(os.path.join(_SERVICES, "api-gateway", os.path.basename(p)), encoding="utf-8").read()
        assert open(p, encoding="utf-8").read() == ref, p


def test_shared_copies_survive_junk_blank_and_nonfinite_settings():
    for p in _shared_copies():
        d, mod = os.path.dirname(p), os.path.basename(p)[:-3]
        specs = _SHARED[os.path.basename(p)]
        probe = f"import {mod} as m; print(' '.join(repr(getattr(m, a)) for a in {[s[1] for s in specs]!r}))"
        for val in ("abc", "", "   ", "nan", "inf", "1e999x"):
            env = dict(os.environ)
            env.update({s[0]: val for s in specs})
            r = subprocess.run([sys.executable, "-c", probe], cwd=d, env=env, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, (p, val, r.stderr[-300:])
            assert r.stdout.split() == [repr(s[2]) for s in specs], (p, val, r.stdout)
        env = dict(os.environ)
        env.update({s[0]: "7.5" for s in specs})
        r = subprocess.run([sys.executable, "-c", probe], cwd=d, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and r.stdout.split() == ["7.5"] * len(specs), (p, r.stdout, r.stderr[-200:])


def _load(path, name):
    d = os.path.dirname(path)
    sys.path.insert(0, d)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(d)


def test_event_feed_check_dryrun_survives_junk_settings():
    p = os.path.join(_SERVICE, "tools", "event_feed_check.py")
    if not os.path.exists(p):
        return
    old = {k: os.environ.get(k) for k in ("INTRADAY_EVENT_TRIGGER_MIN_STRENGTH", "INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES", "INTRADAY_EVENT_VETO_MIN_STRENGTH")}
    try:
        mod = _load(p, "_e505_event_feed_check")
        mod._get = lambda path, timeout=30: {"ok": True, "events": []}
        for val in ("abc", ""):
            for k in old:
                os.environ[k] = val
            buf = io.StringIO()
            with redirect_stdout(buf):
                mod.cmd_dryrun()
            assert "min strength 0.6 max age 30m | veto min 0.4" in buf.getvalue(), buf.getvalue()
        os.environ["INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES"] = "45"
        buf = io.StringIO()
        with redirect_stdout(buf):
            mod.cmd_dryrun()
        assert "max age 45m" in buf.getvalue()
    except ImportError:
        pass   # a dependency of the tool is missing in this environment
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_rl_explore_epsilon_survives_junk_settings():
    p = os.path.join(_SERVICE, "training", "rl_explore.py")
    if not os.path.exists(p):
        return
    old = os.environ.get("RL_EPSILON")
    try:
        mod = _load(p, "_e505_rl_explore")
        for val, want in (("abc", 0.08), ("", 0.08), ("0.2", 0.2)):
            os.environ["RL_EPSILON"] = val
            assert mod.EpsilonGreedyBandit(0.08).epsilon == want, val
    except ImportError:
        pass
    finally:
        if old is None:
            os.environ.pop("RL_EPSILON", None)
        else:
            os.environ["RL_EPSILON"] = old


if __name__ == "__main__":
    bad = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                bad += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:300])
    sys.exit(1 if bad else 0)
