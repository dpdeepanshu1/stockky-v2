"""group442: the switch engine (services/*/control/kit.py), both catalogs and scripts/check_control_catalog.py.
Plain asserts (python3 scripts/test_control_kit.py) or pytest. Uses temp dirs and fake modules only; the real services are never started."""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import check_control_catalog as chk  # noqa: E402

RT = ROOT / "services" / "real-trade-service"
PS = ROOT / "services" / "position-stocks-service"


def _kit():
    for m in [k for k in sys.modules if k == "control" or k.startswith("control.")]:
        del sys.modules[m]
    sys.path.insert(0, str(RT))
    try:
        return importlib.import_module("control.kit")
    finally:
        sys.path.remove(str(RT))


kit = _kit()
F = kit.Flag


def _fake(name="fake_cfg_mod", **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def _ctl(flags, d=None):
    return kit.Controller("svc", flags, kit.Store(d or tempfile.mkdtemp(prefix="ctl_")))


def _flags():
    return (
        F("T_GATE", "gate", "g", "bool", "h", targets=(("fake_cfg_mod", "GATE"),), live=True, looser="off", shipped="true", recommended="true"),
        F("T_LOGONLY", "logonly", "g", "bool", "h", targets=(("fake_cfg_mod", "LOGONLY"),), live=True, looser="on", shipped="false", recommended="true"),
        F("T_MODE", "mode", "g", "enum", "h", choices=("enforce", "log", "off"), same_rank=("log", "off"), targets=(("fake_cfg_mod", "MODE"),), live=True, shipped="off"),
        F("T_CHASE", "chase", "g", "float", "h", lo=0, hi=10, looser="higher", zero_off=True, targets=(("fake_cfg_mod", "CHASE"),), live=True, shipped="1.5"),
        F("T_ROOM", "room", "g", "float", "h", lo=0, hi=5, looser="lower", targets=(("fake_cfg_mod", "ROOM"),), live=True, shipped="0.5"),
        F("T_N", "n", "g", "int", "h", lo=0, hi=10, looser="neutral", targets=(("fake_cfg_mod", "N"),), live=True, shipped="2"),
        F("T_RESTART", "restart", "g", "bool", "h", targets=(("fake_cfg_mod", "RESTART"),), live=False, looser="off", shipped="true"),
    )


def _mod():
    return _fake(GATE=True, LOGONLY=False, MODE="off", CHASE=1.5, ROOM=0.5, N=2, RESTART=True)


def _clean_env():
    for k in list(os.environ):
        if k.startswith("T_"):
            del os.environ[k]


def test_float_default_written_as_3_0_is_not_pending_restart():
    # group446: the running value reads back canonical ("3"), the catalog default was the raw text ("3.0"): that is NOT a pending restart.
    _clean_env()
    _fake(STRETCH=3.0)
    c = _ctl((F("T_STRETCH", "stretch", "g", "float", "h", lo=0, hi=12, looser="higher", targets=(("fake_cfg_mod", "STRETCH"),), live=True, shipped="3.0"),))
    row = [x for x in c.describe()["flags"] if x["key"] == "T_STRETCH"][0]
    assert row["pending_restart"] is False and row["effective"] == "3"
    _clean_env()


def test_the_two_engine_copies_are_identical():
    a = hashlib.sha256((RT / "control" / "kit.py").read_bytes()).hexdigest()
    b = hashlib.sha256((PS / "control" / "kit.py").read_bytes()).hexdigest()
    assert a == b, "services/real-trade-service/control/kit.py and services/position-stocks-service/control/kit.py must be byte-identical"
    for n in ("boot.py", "__init__.py"):
        assert (RT / "control" / n).read_bytes() == (PS / "control" / n).read_bytes(), n


def test_canonical_values_and_ranges():
    c = kit.canonical
    g, ch, m, n = _flags()[0], _flags()[3], _flags()[2], _flags()[5]
    assert c(g, "1") == "true" and c(g, "OFF") == "false" and c(g, True) == "true"
    for bad in ("maybe", "", None):
        try:
            c(g, bad)
            assert False, bad
        except ValueError:
            pass
    assert c(m, "LOG") == "log"
    try:
        c(m, "shadow")
        assert False
    except ValueError:
        pass
    assert c(ch, "2") == "2" and c(ch, "1.50") == "1.5" and c(n, "3") == "3"
    for v in ("-1", "11", "nan", "inf", "x"):
        try:
            c(ch, v)
            assert False, v
        except ValueError:
            pass
    try:
        c(n, "2.5")
        assert False
    except ValueError:
        pass


def test_classification():
    fl = {f.key: f for f in _flags()}
    k = kit.classify
    assert k(fl["T_GATE"], "true", "false") == "LOOSENS" and k(fl["T_GATE"], "false", "true") == "TIGHTENS" and k(fl["T_GATE"], "true", "true") == "SAME"
    assert k(fl["T_LOGONLY"], "false", "true") == "LOOSENS"
    assert k(fl["T_MODE"], "enforce", "log") == "LOOSENS" and k(fl["T_MODE"], "log", "enforce") == "TIGHTENS"
    assert k(fl["T_MODE"], "log", "off") == "NEUTRAL" and k(fl["T_MODE"], "off", "log") == "NEUTRAL"        # neither ever blocks
    assert k(fl["T_CHASE"], "1.5", "3") == "LOOSENS" and k(fl["T_CHASE"], "1.5", "0") == "LOOSENS"          # 0 = test off = loosest
    assert k(fl["T_CHASE"], "0", "1.5") == "TIGHTENS"
    assert k(fl["T_ROOM"], "0.5", "0.2") == "LOOSENS" and k(fl["T_ROOM"], "0.5", "0") == "LOOSENS" and k(fl["T_ROOM"], "0.5", "1") == "TIGHTENS"
    assert k(fl["T_N"], "2", "5") == "NEUTRAL" and k(fl["T_GATE"], None, "false") == "NEUTRAL"


def test_time_of_day_switch_values_and_direction():
    """E41: entry cut-offs are HH:MM switches; a LATER cut-off loosens."""
    f = F("X_CUTOFF_IST", "cut-off", "g", "time", "h", looser="later", shipped="14:00", live=False)
    assert kit.validate_flag(f) == []
    assert kit.canonical(f, "9:05") == "09:05" and kit.canonical(f, " 14:30 ") == "14:30"
    for bad in ("", "24:00", "14:60", "1430", "14:3", "abc", "14:00:00"):
        try:
            kit.canonical(f, bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should be refused")
    assert kit.classify(f, "14:00", "14:30") == "LOOSENS"
    assert kit.classify(f, "14:30", "14:00") == "TIGHTENS"
    assert kit.classify(f, "14:00", "14:00") == "SAME"
    g = F("X_EARLY", "t", "g", "time", "h", looser="earlier", shipped="09:20")
    assert kit.classify(g, "09:20", "09:30") == "TIGHTENS" and kit.classify(g, "09:30", "09:20") == "LOOSENS"
    assert F("X_BAD", "t", "g", "time", "h", looser="higher", shipped="14:00").looser == "higher"
    assert any("looser" in x for x in kit.validate_flag(F("X_BAD", "t", "g", "time", "h", looser="higher", shipped="14:00")))
    assert any("shipped" in x for x in kit.validate_flag(F("X_BLANK", "t", "g", "time", "h", looser="later", shipped="")))


def test_entry_time_limits_are_on_the_page_with_the_config_defaults():
    import ast
    want = {"real-trade-service": {"ENTRY_LAST_TIME_IST": "14:00", "CALENDAR_EXPIRY_ENTRY_CUTOFF_IST": "14:30", "CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST": "14:30"},
            "position-stocks-service": {"SCALP_ENTRY_CUTOFF_IST": "14:00", "CALENDAR_EXPIRY_ENTRY_CUTOFF_IST": "14:30", "CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST": "14:30"}}
    for svc, keys in want.items():
        cat = (ROOT / "services" / svc / "control" / "catalog.py").read_text()
        cfg = (ROOT / "services" / svc / "config.py").read_text()
        for k, v in keys.items():
            assert f'Flag("{k}"' in cat, (svc, k)
            # the shipped value on the page equals the default written in config.py
            line = next(l for l in cfg.splitlines() if l.startswith(f"{k} = "))
            assert f'"{v}"' in line, (svc, k, line)


def test_catalog_entries_validate():
    for f in _flags():
        assert kit.validate_flag(f) == [], f.key
    assert kit.validate_flag(F("lower", "t", "g", "bool", "h", shipped="true"))
    assert kit.validate_flag(F("API_TOKEN", "t", "g", "bool", "h", looser="off", shipped="true"))                  # a secret-looking name is never a switch
    assert kit.validate_flag(F("X", "t", "g", "enum", "h", choices=("a",), shipped="a"))
    assert kit.validate_flag(F("X", "t", "g", "float", "h", looser="higher", shipped="1"))                          # no range
    assert kit.validate_flag(F("X", "t", "g", "bool", "h", looser="off", live=True, shipped="true"))                # live without targets
    assert kit.validate_flag(F("X", "t", "g", "bool", "h", looser="off", shipped="true", recommended="perhaps"))


def test_set_applies_live_persists_audits_and_reads_back():
    _clean_env()
    mod = _mod()
    d = tempfile.mkdtemp(prefix="ctl_")
    c = _ctl(_flags(), d)
    r = c.set("T_ROOM", "0.8", "admin")                                  # tightening: no confirm needed
    assert r["applied_live"] and not r["restart_required"] and mod.ROOM == 0.8 and os.environ["T_ROOM"] == "0.8"
    row = [x for x in c.describe()["flags"] if x["key"] == "T_ROOM"][0]
    assert row["running"] == "0.8" and row["source"] == "override" and not row["pending_restart"]
    c2 = _ctl(_flags(), d)                                                  # "restart": a new controller on the same directory sees the override
    assert c2.store.overrides()["T_ROOM"]["value"] == "0.8"
    assert kit.boot_apply(_flags(), d) == {"T_ROOM": "0.8"}
    audit = c.store.read_audit()
    assert audit[0]["action"] == "set" and audit[0]["key"] == "T_ROOM" and audit[0]["from"] == "0.5" and audit[0]["change"] == "TIGHTENS" and audit[0]["by"] == "admin"
    _clean_env()


def test_a_loosening_change_needs_confirm_and_changes_nothing_without_it():
    _clean_env()
    mod = _mod()
    c = _ctl(_flags())
    try:
        c.set("T_GATE", "false", "admin")
        assert False
    except kit.ControlError as e:
        assert e.status == 409 and e.detail["needs_confirm"] and e.detail["change"] == "LOOSENS"
    assert mod.GATE is True and "T_GATE" not in os.environ and c.store.overrides() == {}
    assert c.set("T_GATE", "false", "admin", confirm=True)["applied_live"] and mod.GATE is False
    _clean_env()


def test_unknown_key_bad_value_and_secrets_are_refused():
    _clean_env()
    _mod()
    c = _ctl(_flags())
    for key, val, status in (("NOT_A_SWITCH", "1", 404), ("ADMIN_PASSWORD_HASH", "x", 404), ("T_MODE", "shadow", 400), ("T_CHASE", "99", 400), ("T_N", "abc", 400)):
        try:
            c.set(key, val, "admin", confirm=True)
            assert False, key
        except kit.ControlError as e:
            assert e.status == status, (key, e.status)
    assert c.store.overrides() == {} and not any(k.startswith("T_") for k in os.environ)


def test_a_restart_only_switch_is_saved_and_flagged_pending():
    _clean_env()
    mod = _mod()
    c = _ctl(_flags())
    r = c.set("T_RESTART", "false", "admin", confirm=True)
    assert not r["applied_live"] and r["restart_required"] and "docker compose restart svc" in r["restart_hint"] and mod.RESTART is True
    row = [x for x in c.describe()["flags"] if x["key"] == "T_RESTART"][0]
    assert row["pending_restart"] and row["running"] == "true" and row["desired"] == "false"
    assert c.describe()["summary"]["pending_restart"] == 1
    _clean_env()


def test_clear_restores_the_environment_value_live():
    _clean_env()
    mod = _mod()
    os.environ["T_ROOM"] = "0.3"
    mod.ROOM = 0.3
    c = _ctl(_flags())
    c.set("T_ROOM", "0.9", "admin")
    assert mod.ROOM == 0.9
    r = c.clear("T_ROOM", "admin")
    assert r["cleared"] and r["applied_live"] and mod.ROOM == 0.3 and os.environ["T_ROOM"] == "0.3"
    c.set("T_N", "4", "admin")
    assert c.clear("T_N", "admin")["applied_live"] and mod.N == 2 and "T_N" not in os.environ            # no env value: back to the shipped default
    assert c.clear("T_N", "admin")["cleared"] is False
    _clean_env()


def test_derived_twin_follows_only_while_it_has_no_value_of_its_own():
    _clean_env()
    mod = _fake(REAL="enforce", DEMO="enforce")
    real = F("T_REAL", "real", "g", "enum", "h", choices=("enforce", "off"), targets=(("fake_cfg_mod", "REAL"),), derived=(("fake_cfg_mod", "DEMO", "T_DEMO"),), live=True, shipped="enforce")
    demo = F("T_DEMO", "demo", "g", "enum", "h", choices=("enforce", "off"), targets=(("fake_cfg_mod", "DEMO"),), live=True, shipped="enforce")
    c = _ctl((real, demo))
    c.set("T_REAL", "off", "admin", confirm=True)
    assert mod.REAL == "off" and mod.DEMO == "off"
    c.set("T_DEMO", "enforce", "admin")
    c.set("T_REAL", "enforce", "admin")
    c.set("T_REAL", "off", "admin", confirm=True)
    assert mod.REAL == "off" and mod.DEMO == "enforce"                                                      # the twin has its own value now
    _clean_env()


def test_preset_recommended_dry_run_then_apply_with_confirm():
    _clean_env()
    mod = _mod()
    mod.GATE, mod.LOGONLY = False, False                                                                    # gate OFF (rec ON = tightens), logonly OFF (rec ON = loosens)
    c = _ctl(_flags())
    dry = c.preset_recommended("admin", dry_run=True)
    assert {p["key"]: p["change"] for p in dry["changes"]} == {"T_GATE": "TIGHTENS", "T_LOGONLY": "LOOSENS"} and dry["loosening"] == 1 and mod.LOGONLY is False
    try:
        c.preset_recommended("admin", dry_run=False)
        assert False
    except kit.ControlError as e:
        assert e.status == 409
    out = c.preset_recommended("admin", dry_run=False, confirm=True)
    assert mod.GATE is True and mod.LOGONLY is True and sorted(out["applied"]) == ["T_GATE", "T_LOGONLY"]
    assert c.describe()["summary"]["off_recommended"] == 0
    _clean_env()


def test_unwritable_state_dir_keeps_working_in_memory_and_says_so():
    _clean_env()
    mod = _mod()
    f = Path(tempfile.mkdtemp()) / "afile"
    f.write_text("x")
    c = _ctl(_flags(), str(f / "sub"))
    assert c.store.persistent is False
    r = c.set("T_ROOM", "0.7", "admin")
    assert r["applied_live"] and "WARNING" in r["message"] and mod.ROOM == 0.7
    assert c.describe()["persistent"] is False and c.store.read_audit()[0]["key"] == "T_ROOM"
    _clean_env()


def test_damaged_overrides_file_is_set_aside_and_boot_never_raises():
    d = tempfile.mkdtemp(prefix="ctl_")
    (Path(d) / "overrides.json").write_text("{not json")
    assert kit.boot_apply(_flags(), d) == {} and (Path(d) / "overrides.json.damaged").exists()
    (Path(d) / "overrides.json").write_text('{"v":1,"overrides":{"T_ROOM":{"value":"999"},"GONE":{"value":"1"},"T_N":{"value":"3"}}}')
    _clean_env()
    assert kit.boot_apply(_flags(), d) == {"T_N": "3"}                                                       # invalid value and unknown key are ignored
    _clean_env()


def test_real_catalogs_are_valid_and_pass_the_static_check():
    assert chk.main([]) == 0
    for svc in chk.SERVICES:
        cat, k = chk._load_catalog(ROOT / "services" / svc)
        keys = [f.key for f in cat.CATALOG]
        assert len(keys) == len(set(keys)) and len(keys) >= 25, svc
        assert all(k.validate_flag(f) == [] for f in cat.CATALOG)
        assert all(f.live for f in cat.CATALOG), "every catalog switch is verified live; add live=False ones deliberately"


def test_the_static_check_catches_a_switch_that_would_only_look_live():
    d = Path(tempfile.mkdtemp(prefix="svc_"))
    (d / "config.py").write_text("GATE = True\nOTHER = True\nCOPIED = True\nDEAD = True\nDEFARG = 1\n")
    (d / "user.py").write_text(
        "import config\nfrom config import COPIED\nSNAP = config.OTHER\n\n\ndef ok():\n    return config.GATE\n\n\ndef g():\n    return getattr(config, 'GATE', True)\n\n\n"
        "def f(x=config.DEFARG):\n    return x\n\n\ndef uses():\n    return COPIED\n")
    r = chk.probe(d, "config", "GATE")
    assert r["defined"] and len(r["call_time"]) == 2 and not r["import_time"] and not r["copies"]
    r = chk.probe(d, "config", "OTHER")
    assert r["import_time"] and not r["call_time"]
    assert chk.probe(d, "config", "COPIED")["copies"]
    assert chk.probe(d, "config", "DEFARG")["import_time"]                                                   # a default argument is evaluated at import
    assert chk.probe(d, "config", "DEAD")["call_time"] == []
    assert chk.probe(d, "config", "MISSING")["defined"] is False
    assert chk.probe(d, "nope", "X").get("error")


def test_shipped_defaults_match_the_real_config_where_it_can_be_imported():
    """Imports each service's config in a clean environment (no .env) and compares the running value with the catalog's `shipped`."""
    import subprocess
    code = (
        "import os,sys\n"
        "for k in list(os.environ):\n"
        "    if k.startswith(('STRUCTURE','PS_','KRONOS','TIMESFM','CANDIDATE','ENTRY','OPENING','ORDER_FLOW','BOARD','BAR_RECORD','BROKER','OVERNIGHT','IMPORT')): del os.environ[k]\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import control.catalog as cat, control.kit as kit\n"
        "bad=[(f.key, kit.running_value(f), f.shipped) for f in cat.CATALOG if kit.running_value(f) is not None and kit.running_value(f) != kit.canonical(f, f.shipped)]\n"
        "print('BAD', bad)\n"
    )
    for svc in (RT, PS):
        r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(svc), capture_output=True, text=True)
        assert r.stdout.strip().endswith("BAD []"), (svc.name, r.stdout[-400:], r.stderr[-400:])


def test_main_py_wires_boot_first_and_mounts_the_routes():
    for svc, name in ((RT, "real-trade-service"), (PS, "position-stocks-service")):
        s = (svc / "main.py").read_text()
        i_boot, i_config = s.index("import control.boot"), s.index("\nimport config")
        assert i_boot < i_config, "control.boot must run before config is imported"
        assert f'Controller("{name}"' in s and "_control_kit.mount(app, _control, require_admin)" in s


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
