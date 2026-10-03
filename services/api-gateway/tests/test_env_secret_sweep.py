"""tests/test_env_secret_sweep.py — group 70: whitespace-only / padded token, API-key and credential env vars.

Follow-on to group 67 (database + Upstash URLs) and groups 68-69 (notification channels). The same bug
class sat on every credential read outside those: `os.getenv("X", "")` / `os.environ.get("X")` keeps a
padded value as-is (a trailing newline pasted into a dashboard field or an env_file), so a padded Fernet
key, TOTP secret, API key or bot token reaches the library untrimmed and fails there, and a
whitespace-only value is truthy, so "is it configured?" checks (`if not KEY`) pass and the first real
call fails. Every such read now goes through `(os.getenv("NAME") or "").strip()` (keys that default to
None keep that: `... or None`).

Group 67's guard only matched `os.getenv(`; five Upstash reads written as `os.environ.get(` slipped
through (circuit_breaker.py in api-gateway / market-data / decision, rate_limit_monitor.py,
scheduler/run_once.py). The guard in test_env_blank_sweep.py now matches both call forms, and the
guard below covers the credential names.

Deliberately NOT stripped: ORACLE_PASSWORD / ORACLE_WALLET_PASSWORD (a password may legitimately keep edge
whitespace). Group 71 (test_env_oracle_sweep.py) covers the Oracle vars: ORACLE_DSN / USER / wallet dir are
trimmed, and the passwords stay verbatim but a whitespace-only value counts as unset.

Run from services/api-gateway:
    python3 -m pytest tests/test_env_secret_sweep.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import re
import sys
import types

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SKIP_DIRS = {"tests", "__pycache__", ".venv", "venv", "node_modules", ".git"}
_counter = itertools.count()

_CREDENTIAL_NAMES = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "DHAN_CREDENTIAL_ENC_KEY", "DHAN_TOTP_SECRET", "DHAN_CLIENT_ID", "DHAN_PIN",
    "ANGELONE_CLIENT_ID", "ANGELONE_MPIN", "ANGELONE_API_KEY", "ANGELONE_TOTP_SECRET",
    "SESSION_SECRET", "ADMIN_PASSWORD_HASH", "ADMIN_PASSWORD_HASH_B64",
    "INDIANAPI_KEY", "HF_API_KEY", "NEWSAPI_KEY", "ALPHA_VANTAGE_API_KEY",
    "TWELVE_DATA_API_KEY", "TWELVEDATA_API_KEY", "POLYGON_API_KEY", "GEMINI_API_KEY",
    "SUPABASE_SERVICE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL",
)
_RAW = re.compile(r'_?os\.(?:getenv|environ\.get)\(\s*"(%s)"\s*(,[^)]*)?\)' % "|".join(_CREDENTIAL_NAMES))


def _sources():
    for cur, dirs, files in os.walk(SERVICES):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(cur, f)
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    yield os.path.relpath(p, SERVICES), fh.read()


def test_the_guard_finds_the_credential_reads():
    # If the services are reorganised and nothing matches, the guard below would pass vacuously.
    hits = [rel for rel, text in _sources() if _RAW.search(text)]
    assert len(hits) >= 10, hits


def test_every_credential_read_is_stripped():
    offenders = []
    for rel, text in _sources():
        for m in _RAW.finditer(text):
            wrapped = text[max(0, m.start() - 1):m.end() + len(' or "").strip()')]
            if wrapped != "(" + m.group(0) + ' or "").strip()':
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{line}: {m.group(0)}")
    assert offenders == []


# --- behaviour: configs load standalone, so exercise the real module-level reads --------------------

def _load(rel, monkeypatch, env, name_prefix="_secret"):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    path = os.path.join(SERVICES, *rel.split("/"))
    before = set(sys.modules)
    sys.path.insert(0, os.path.dirname(path))
    sys.path.insert(0, os.path.dirname(os.path.dirname(path)))
    name = "%s_%d" % (name_prefix, next(_counter))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    except ModuleNotFoundError as e:  # a third-party dependency, not our code
        pytest.skip("optional dependency missing: %s" % e.name)
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
        for k in set(sys.modules) - before:
            sys.modules.pop(k, None)


_RT_VARS = ("SESSION_SECRET", "DHAN_CREDENTIAL_ENC_KEY", "DHAN_TOTP_SECRET", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "ADMIN_PASSWORD_HASH", "ADMIN_PASSWORD_HASH_B64")
_RT_ATTR = {"SESSION_SECRET": "SESSION_SECRET", "DHAN_CREDENTIAL_ENC_KEY": "DHAN_CREDENTIAL_ENC_KEY",
            "DHAN_TOTP_SECRET": "DHAN_TOTP_SECRET", "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID": "TELEGRAM_CHAT_ID", "ADMIN_PASSWORD_HASH": "ADMIN_PASSWORD_HASH"}
_PS_VARS = ("SESSION_SECRET", "DHAN_CREDENTIAL_ENC_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
            "ADMIN_PASSWORD_HASH", "ADMIN_PASSWORD_HASH_B64", "ANGELONE_CLIENT_ID", "ANGELONE_MPIN",
            "ANGELONE_API_KEY", "ANGELONE_TOTP_SECRET")


def _clean(names):
    return {n: None for n in names}


@pytest.mark.parametrize("rel,names", [
    ("real-trade-service/config.py", _RT_VARS),
    ("position-stocks-service/config.py", _PS_VARS),
])
@pytest.mark.parametrize("blank", ["", " ", "\t", " \n "])
def test_blank_credentials_read_as_unset(monkeypatch, rel, names, blank):
    mod = _load(rel, monkeypatch, {**_clean(names), **{n: blank for n in names if n != "ADMIN_PASSWORD_HASH_B64"}})
    for n in names:
        if n in ("ADMIN_PASSWORD_HASH_B64",):
            continue
        assert getattr(mod, n) == "", n


@pytest.mark.parametrize("rel,names", [
    ("real-trade-service/config.py", [n for n in _RT_VARS if n != "ADMIN_PASSWORD_HASH_B64"]),
    ("position-stocks-service/config.py", [n for n in _PS_VARS if n != "ADMIN_PASSWORD_HASH_B64"]),
])
def test_padded_credentials_are_trimmed(monkeypatch, rel, names):
    env = {**_clean(names), "ADMIN_PASSWORD_HASH_B64": None}
    env.update({n: "  v-" + n + " \n" for n in names})
    mod = _load(rel, monkeypatch, env)
    for n in names:
        assert getattr(mod, n) == "v-" + n, n


@pytest.mark.parametrize("rel", ["real-trade-service/config.py", "position-stocks-service/config.py"])
def test_blank_admin_hash_does_not_block_the_b64_fallback(monkeypatch, rel):
    import base64
    h = "$2b$12$abcdefghijklmnopqrstuv"
    b64 = base64.b64encode(h.encode()).decode()
    mod = _load(rel, monkeypatch, {"ADMIN_PASSWORD_HASH": "   ", "ADMIN_PASSWORD_HASH_B64": " " + b64 + "\n"})
    assert mod.ADMIN_PASSWORD_HASH == h


@pytest.mark.parametrize("rel", ["real-trade-service/config.py", "position-stocks-service/config.py"])
def test_real_admin_hash_still_wins_over_b64(monkeypatch, rel):
    import base64
    b64 = base64.b64encode(b"from-b64").decode()
    mod = _load(rel, monkeypatch, {"ADMIN_PASSWORD_HASH": " from-plain ", "ADMIN_PASSWORD_HASH_B64": b64})
    assert mod.ADMIN_PASSWORD_HASH == "from-plain"


# --- behaviour: circuit breaker Redis init (api-gateway copy; the other two are the same lines) ------

def _fake_upstash(monkeypatch):
    calls = []

    class Redis:
        def __init__(self, url=None, token=None):
            calls.append((url, token))

        def ping(self):
            return True

    mod = types.ModuleType("upstash_redis")
    mod.Redis = Redis
    monkeypatch.setitem(sys.modules, "upstash_redis", mod)
    return calls


_CB_ENV = {"USE_REDIS": "1", "CB_REDIS_SYNC": "1", "DISABLE_REDIS": None, "DISABLE_UPSTASH": None}


@pytest.mark.parametrize("url,token", [(" ", "tok"), ("https://u.upstash.io", " \t"), ("", "  ")])
def test_circuit_breaker_ignores_blank_upstash_values(monkeypatch, url, token):
    calls = _fake_upstash(monkeypatch)
    cb = _load("api-gateway/circuit_breaker.py", monkeypatch,
               {**_CB_ENV, "UPSTASH_REDIS_REST_URL": url, "UPSTASH_REDIS_REST_TOKEN": token}, "_cb")
    assert cb._get_redis() is None
    assert calls == []


def test_circuit_breaker_passes_trimmed_upstash_values(monkeypatch):
    calls = _fake_upstash(monkeypatch)
    cb = _load("api-gateway/circuit_breaker.py", monkeypatch,
               {**_CB_ENV, "UPSTASH_REDIS_REST_URL": " https://u.upstash.io\n",
                "UPSTASH_REDIS_REST_TOKEN": "  tok123 "}, "_cb")
    assert cb._get_redis() is not None
    assert calls == [("https://u.upstash.io", "tok123")]
