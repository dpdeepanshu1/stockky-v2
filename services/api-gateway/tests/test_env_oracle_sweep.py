"""tests/test_env_oracle_sweep.py — group 71: whitespace-only / padded Oracle env vars.

Last open piece of the env-blank sweep (groups 67-70). `ORACLE_DSN` is the single switch that moves a
process from Postgres/Neon onto Oracle Autonomous DB, and it was read as `bool(os.environ.get("ORACLE_DSN"))`
in ~20 places: a whitespace-only value (a trailing newline pasted into an env_file, `ORACLE_DSN=" "`) is
truthy, so the process flipped into Oracle mode with an empty DSN even when DATABASE_URL pointed at Postgres.
The same read also fed `connect_args["dsn"]`, and padded ORACLE_USER / ORACLE_WALLET_DIR / TNS_ADMIN values
reached oracledb untrimmed.

Rules enforced here:
  * ORACLE_DSN, ORACLE_USER, ORACLE_WALLET_DIR, TNS_ADMIN, ORACLE_WALLET_HOST_DIR are read as
    `(os.environ.get("NAME") or "").strip()`; a whitespace-only value is unset.
  * ORACLE_PASSWORD, ORACLE_ADMIN_PASSWORD and ORACLE_WALLET_PASSWORD are NEVER stripped (a password may
    legitimately keep edge whitespace) but a whitespace-only value counts as unset, so it no longer shadows the
    ORACLE_ADMIN_PASSWORD fallback or gets sent as the password. They are read through `_secret_env(...)`.

Not covered on purpose: ORACLE_CALL_TIMEOUT_MS (numeric, already tolerant of blank / junk values).

Run from services/api-gateway:
    python3 -m pytest tests/test_env_oracle_sweep.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import re
import sys
import textwrap

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SKIP_DIRS = {"tests", "__pycache__", ".venv", "venv", "node_modules", ".git"}
_counter = itertools.count()

_TEXT_NAMES = ("ORACLE_DSN", "ORACLE_USER", "ORACLE_WALLET_DIR", "TNS_ADMIN", "ORACLE_WALLET_HOST_DIR")
_SECRET_NAMES = ("ORACLE_PASSWORD", "ORACLE_ADMIN_PASSWORD", "ORACLE_WALLET_PASSWORD")
_ALL_ORACLE_ENV = _TEXT_NAMES + _SECRET_NAMES

_TEXT_RAW = re.compile(r'_?os\.(?:getenv|environ\.get)\(\s*"(%s)"\s*(,[^)]*)?\)' % "|".join(_TEXT_NAMES))
_SECRET_RAW = re.compile(r'_?os\.(?:getenv|environ\.get)\(\s*"(%s)"\s*(,[^)]*)?\)' % "|".join(_SECRET_NAMES))

ORACLE_COMPAT_COPIES = (
    "real-trade-service/oracle_compat.py",
    "position-stocks-service/oracle_compat.py",
    "api-gateway/oracle_compat.py",
    "market-data-service/oracle_compat.py",
    "analysis-intelligence-service/fundamental/oracle_compat.py",
    "decision-prediction-service/decision/oracle_compat.py",
    "decision-prediction-service/training/oracle_compat.py",
    "notification-scheduler-service/notification/oracle_compat.py",
)


def _sources():
    for cur, dirs, files in os.walk(SERVICES):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(cur, f)
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    yield os.path.relpath(p, SERVICES).replace(os.sep, "/"), fh.read()


def _line(text, pos):
    return text.count("\n", 0, pos) + 1


# --- source-level guards --------------------------------------------------------------------------

def test_the_guards_find_the_oracle_reads():
    # If the services are reorganised and nothing matches, the guards below would pass vacuously.
    text_hits = [rel for rel, text in _sources() if _TEXT_RAW.search(text)]
    assert len(text_hits) >= 14, text_hits
    secret_hits = [rel for rel, text in _sources() if "_secret_env(" in text or _SECRET_RAW.search(text)]
    assert len(secret_hits) >= 9, secret_hits


def test_every_oracle_text_read_is_stripped():
    offenders = []
    for rel, text in _sources():
        for m in _TEXT_RAW.finditer(text):
            wrapped = text[max(0, m.start() - 1):m.end() + len(' or "").strip()')]
            if wrapped != "(" + m.group(0) + ' or "").strip()':
                offenders.append(f"{rel}:{_line(text, m.start())}: {m.group(0)}")
    assert offenders == []


# The only place a password var may be read without `_secret_env`: position-stocks config.py, which keeps
# the module-level constants and applies the same verbatim / blank-is-unset rule on the next line.
_RAW_PASSWORD_ALLOWED = {"position-stocks-service/config.py"}


def test_oracle_passwords_go_through_secret_env():
    offenders = []
    for rel, text in _sources():
        if rel in _RAW_PASSWORD_ALLOWED:
            continue
        for m in _SECRET_RAW.finditer(text):
            # the body of the _secret_env helper itself reads through `os.environ.get(name)`, not a literal
            offenders.append(f"{rel}:{_line(text, m.start())}: {m.group(0)}")
    assert offenders == []


def test_oracle_passwords_are_never_stripped():
    # A password may legitimately start/end with whitespace; neither a raw read nor a _secret_env call
    # may be wrapped in .strip().
    bad = re.compile(r'(?:_secret_env\([^)]*\)|%s)\s*\.strip\(\)' % _SECRET_RAW.pattern)
    offenders = []
    for rel, text in _sources():
        for m in bad.finditer(text):
            offenders.append(f"{rel}:{_line(text, m.start())}: {m.group(0)}")
    assert offenders == []


def test_oracle_compat_copies_are_identical():
    texts = {}
    for rel in ORACLE_COMPAT_COPIES:
        with open(os.path.join(SERVICES, *rel.split("/")), encoding="utf-8") as fh:
            texts[rel] = fh.read()
    assert len(set(texts.values())) == 1, sorted(texts)


# --- behaviour: every copy of oracle_compat ------------------------------------------------------

def _load_path(path, monkeypatch, env, name_prefix="_oracle_sweep"):
    for k in _ALL_ORACLE_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
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
        _purge_repo_modules(before)


def _purge_repo_modules(before):
    # Drop only modules loaded from this repo; third-party packages (numpy, sqlalchemy, ...) must stay
    # imported, numpy in particular refuses to be imported twice in one process.
    for k in set(sys.modules) - before:
        f = getattr(sys.modules.get(k), "__file__", None) or ""
        if os.path.abspath(f).startswith(SERVICES) or k.startswith(("_oracle_sweep_", "_calib_")):
            sys.modules.pop(k, None)


def _compat(rel, monkeypatch, env):
    return _load_path(os.path.join(SERVICES, *rel.split("/")), monkeypatch, env)


_BLANKS = ["", " ", "\t", " \n "]


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
@pytest.mark.parametrize("blank", _BLANKS)
def test_blank_dsn_is_not_oracle_mode(monkeypatch, rel, blank):
    oc = _compat(rel, monkeypatch, {"ORACLE_DSN": blank})
    assert oc.oracle_is_configured("") is False
    # a Postgres URL must keep winning over a blank DSN (it used to be flipped to Oracle)
    assert oc.oracle_is_configured("postgresql://u:p@h/db") is False


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_real_or_padded_dsn_is_oracle_mode(monkeypatch, rel):
    assert _compat(rel, monkeypatch, {"ORACLE_DSN": "stockkydb_high"}).oracle_is_configured("") is True
    assert _compat(rel, monkeypatch, {"ORACLE_DSN": "  stockkydb_high \n"}).oracle_is_configured("") is True
    # an explicit oracle:// URL still enables Oracle with no DSN env at all
    assert _compat(rel, monkeypatch, {}).oracle_is_configured("oracle+oracledb://u:p@h/s") is True


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_engine_kwargs_trim_text_vars(monkeypatch, rel):
    oc = _compat(rel, monkeypatch, {
        "ORACLE_DSN": "  stockkydb_high \n", "ORACLE_USER": " APPUSER\n",
        "ORACLE_WALLET_DIR": " /oracle_wallet \n", "ORACLE_PASSWORD": "pw",
    })
    ca = oc.oracle_engine_kwargs(False)["connect_args"]
    assert ca["dsn"] == "stockkydb_high"
    assert ca["user"] == "APPUSER"
    assert ca["config_dir"] == ca["wallet_location"] == "/oracle_wallet"


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
@pytest.mark.parametrize("blank", _BLANKS)
def test_engine_kwargs_blank_text_vars_are_unset(monkeypatch, rel, blank):
    oc = _compat(rel, monkeypatch, {"ORACLE_USER": blank, "ORACLE_DSN": blank,
                                    "ORACLE_WALLET_DIR": blank, "TNS_ADMIN": blank})
    ca = oc.oracle_engine_kwargs(False)["connect_args"]
    assert ca["user"] == "ADMIN"          # default restored
    assert ca["dsn"] == ""
    assert "config_dir" not in ca and "wallet_location" not in ca  # not a directory called " "


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_wallet_dir_falls_back_to_tns_admin(monkeypatch, rel):
    oc = _compat(rel, monkeypatch, {"ORACLE_WALLET_DIR": "  ", "TNS_ADMIN": " /tns \n"})
    ca = oc.oracle_engine_kwargs(False)["connect_args"]
    assert ca["config_dir"] == "/tns"


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_passwords_are_kept_verbatim(monkeypatch, rel):
    oc = _compat(rel, monkeypatch, {"ORACLE_PASSWORD": "  p@ss w0rd \n", "ORACLE_WALLET_PASSWORD": " wpw "})
    ca = oc.oracle_engine_kwargs(False)["connect_args"]
    assert ca["password"] == "  p@ss w0rd \n"
    assert ca["wallet_password"] == " wpw "


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
@pytest.mark.parametrize("blank", _BLANKS)
def test_blank_passwords_are_unset(monkeypatch, rel, blank):
    oc = _compat(rel, monkeypatch, {"ORACLE_PASSWORD": blank, "ORACLE_ADMIN_PASSWORD": blank,
                                    "ORACLE_WALLET_PASSWORD": blank})
    ca = oc.oracle_engine_kwargs(False)["connect_args"]
    assert "password" not in ca
    assert "wallet_password" not in ca


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_blank_primary_password_falls_back_to_admin_password(monkeypatch, rel):
    oc = _compat(rel, monkeypatch, {"ORACLE_PASSWORD": "   ", "ORACLE_ADMIN_PASSWORD": "fallback-pw"})
    assert oc.oracle_engine_kwargs(False)["connect_args"]["password"] == "fallback-pw"
    # and a real primary password still wins over the fallback
    oc = _compat(rel, monkeypatch, {"ORACLE_PASSWORD": "primary", "ORACLE_ADMIN_PASSWORD": "fallback-pw"})
    assert oc.oracle_engine_kwargs(False)["connect_args"]["password"] == "primary"


@pytest.mark.parametrize("rel", ORACLE_COMPAT_COPIES)
def test_full_url_form_ignores_discrete_credentials(monkeypatch, rel):
    # unchanged behaviour: with a full oracle:// URL only the wallet location/password ride in connect_args
    oc = _compat(rel, monkeypatch, {"ORACLE_DSN": "x", "ORACLE_USER": "u", "ORACLE_PASSWORD": "p",
                                    "ORACLE_WALLET_DIR": "/w", "ORACLE_WALLET_PASSWORD": "wp"})
    ca = oc.oracle_engine_kwargs(True)["connect_args"]
    assert set(ca) == {"config_dir", "wallet_location", "wallet_password"}


# --- behaviour: the other modules that switch on ORACLE_DSN ---------------------------------------

@pytest.mark.parametrize("rel", [
    "api-gateway/hotpicks_schema.py", "api-gateway/surprise_schema.py", "api-gateway/ipo_schema.py",
])
def test_gateway_schema_modules_ignore_a_blank_dsn(monkeypatch, rel):
    path = os.path.join(SERVICES, *rel.split("/"))
    for blank, expected in (("  ", False), ("", False), (" stockkydb_high ", True)):
        mod = _load_path(path, monkeypatch, {"ORACLE_DSN": blank})
        fn = next(getattr(mod, n) for n in dir(mod) if n.startswith("_is_oracle") or n in ("is_oracle", "_oracle"))
        assert bool(fn()) is expected, (rel, blank, "via oracle_compat")
        # the module's own fallback (taken when oracle_compat can't be imported) reads the env directly
        monkeypatch.setattr(mod, "_oc", None)
        assert bool(fn()) is expected, (rel, blank, "fallback")


def test_scanner_dialect_fallbacks_ignore_a_blank_dsn():
    # surprise_scanner / surprise_premarket: `"oracle" if <DSN set> else "postgresql"` — a literal guard is
    # enough here; the wrapped form is already enforced by test_every_oracle_text_read_is_stripped.
    for rel in ("api-gateway/surprise_scanner.py", "api-gateway/surprise_premarket.py"):
        with open(os.path.join(SERVICES, *rel.split("/")), encoding="utf-8") as fh:
            assert '"oracle" if (os.environ.get("ORACLE_DSN") or "").strip() else "postgresql"' in fh.read(), rel


def test_position_stocks_config_oracle_constants(monkeypatch):
    rel = "position-stocks-service/config.py"
    blank = _load_path(os.path.join(SERVICES, *rel.split("/")), monkeypatch, {
        "ORACLE_DSN": " ", "ORACLE_USER": "\t", "ORACLE_PASSWORD": "  ", "ORACLE_WALLET_PASSWORD": " \n",
        "ORACLE_WALLET_DIR": "  ",
    })
    assert blank.ORACLE_DSN == ""
    assert blank.ORACLE_USER == "ADMIN"
    assert blank.ORACLE_PASSWORD == "" and blank.ORACLE_WALLET_PASSWORD == ""
    assert blank.ORACLE_WALLET_DIR == "/oracle_wallet"

    padded = _load_path(os.path.join(SERVICES, *rel.split("/")), monkeypatch, {
        "ORACLE_DSN": " db_high\n", "ORACLE_USER": " APP ", "ORACLE_PASSWORD": " pw ",
        "ORACLE_WALLET_PASSWORD": " wpw\n", "ORACLE_WALLET_DIR": " /w ",
    })
    assert padded.ORACLE_DSN == "db_high"
    assert padded.ORACLE_USER == "APP"
    assert padded.ORACLE_PASSWORD == " pw " and padded.ORACLE_WALLET_PASSWORD == " wpw\n"  # verbatim
    assert padded.ORACLE_WALLET_DIR == "/w"


def test_training_models_embedded_oracle_helpers(monkeypatch):
    rel = "decision-prediction-service/training/models.py"
    path = os.path.join(SERVICES, *rel.split("/"))
    blank = _load_path(path, monkeypatch, {"ORACLE_DSN": "  ", "ORACLE_PASSWORD": "  ", "ORACLE_ADMIN_PASSWORD": "adm",
                                            "ORACLE_WALLET_DIR": " ", "TNS_ADMIN": " /tns "})
    assert blank._ORACLE_MODE is False
    assert blank._oracle_is_configured("postgresql://u:p@h/db") is False
    ca = blank._oracle_engine_kwargs(False)["connect_args"]
    assert ca["dsn"] == "" and ca["user"] == "ADMIN"
    assert ca["password"] == "adm"            # blank primary falls through to the fallback
    assert ca["config_dir"] == "/tns"

    padded = _load_path(path, monkeypatch, {"ORACLE_DSN": " db_high\n", "ORACLE_PASSWORD": " pw ",
                                             "ORACLE_WALLET_PASSWORD": " wpw "})
    assert padded._ORACLE_MODE is True
    ca = padded._oracle_engine_kwargs(False)["connect_args"]
    assert ca["dsn"] == "db_high"
    assert ca["password"] == " pw " and ca["wallet_password"] == " wpw "


# --- behaviour: calibrate_decay_profiles._autoload_env_if_missing ---------------------------------

def _run_autoload(tmp_path, monkeypatch, env_file_text, env):
    """Run the script's import-time autoload against a throwaway repo tree whose root holds `.env`."""
    scripts = tmp_path / "services" / "real-trade-service" / "scripts"
    scripts.mkdir(parents=True)
    src = os.path.join(SERVICES, "real-trade-service", "scripts", "calibrate_decay_profiles.py")
    target = scripts / "calibrate_decay_profiles.py"
    target.write_text(open(src, encoding="utf-8").read(), encoding="utf-8")
    (tmp_path / ".env").write_text(textwrap.dedent(env_file_text), encoding="utf-8")
    for k in ("DATABASE_URL", "ORACLE_WALLET_HOST_DIR") + _ALL_ORACLE_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    before = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location("_calib_%d" % next(_counter), str(target))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except ModuleNotFoundError as e:
            pytest.skip("optional dependency missing: %s" % e.name)
        except Exception:
            pass  # the script's later imports (db/models) may not resolve in the throwaway tree; the autoload ran first
    finally:
        _purge_repo_modules(before)
        if sys.path and sys.path[0] == ".":
            sys.path.pop(0)


def test_calibrate_env_file_fills_a_blank_exported_dsn(tmp_path, monkeypatch):
    _run_autoload(tmp_path, monkeypatch, "ORACLE_DSN=stockkydb_high\n", {"ORACLE_DSN": "   "})
    assert os.environ["ORACLE_DSN"] == "stockkydb_high"


def test_calibrate_env_file_never_overrides_a_real_exported_value(tmp_path, monkeypatch):
    _run_autoload(tmp_path, monkeypatch, "ORACLE_DSN=from_env_file\n", {"ORACLE_DSN": "already_set"})
    assert os.environ["ORACLE_DSN"] == "already_set"
