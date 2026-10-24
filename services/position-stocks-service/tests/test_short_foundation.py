"""Short selling, Part 2 step 2: the `side` column (default LONG) and the SHORT_MODE switch (ships off; `live` accepted since step 8). Nothing may READ either yet, so every
long path is unchanged. Reads files as text / ast and runs config in a clean interpreter, so it needs no sqlalchemy and runs anywhere."""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent


def _class(name: str) -> ast.ClassDef:
    tree = ast.parse((ROOT / "models.py").read_text(encoding="utf-8"))
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def test_side_column_is_not_null_and_defaults_to_long():
    assigns = {t.id: n.value for n in _class("ScalpPosition").body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    call = assigns["side"]
    kw = {k.arg: ast.literal_eval(k.value) for k in call.keywords}
    assert kw == {"nullable": False, "default": "LONG"}, kw
    assert ast.literal_eval(call.args[0].args[0]) == 5          # String(5)


def test_only_the_position_table_got_a_side_column():
    for cls in ("ScalpCapitalLedger", "ScalpChargesLedger", "ScalpGateState"):
        names = {t.id for n in _class(cls).body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        assert "side" not in names, cls


def test_the_migration_adds_side_as_not_null_long_for_existing_rows():
    src = (ROOT / "db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    rows = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_COLUMN_MIGRATIONS" for t in n.targets))
    hit = [r for r in rows if r[0] == "scalp_positions" and r[1] == "side"]
    assert hit == [("scalp_positions", "side", "VARCHAR2(5)", "VARCHAR(5)", "'LONG'", "'LONG'")], hit        # both defaults set (never mixed), quoted for DDL
    assert len({(r[0], r[1]) for r in rows}) == len(rows)           # no duplicate entries


def _mode(env_value):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SHORT_", "PS_", "STRUCTURE_"))}
    if env_value is not None:
        env["SHORT_MODE"] = env_value
    env["CONTROL_DIR"] = os.path.join(os.environ.get("TMPDIR", "/tmp"), "short_foundation_ctl")
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, '.'); import config; print('MODE=' + config.SHORT_MODE)"],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    line = next((x for x in r.stdout.splitlines() if x.startswith("MODE=")), None)
    assert line, r.stderr[-500:]
    return line[5:]


def test_short_mode_ships_off_and_only_off_shadow_live_are_accepted():
    assert _mode(None) == "off"
    assert _mode("shadow") == "shadow" and _mode(" SHADOW ") == "shadow"
    assert _mode("live") == "live" and _mode(" LIVE ") == "live"          # step 8: accepted (real orders still need the cap and the broker stop)
    for bad in ("on", "true", "1", "", "junk", "real"):
        assert _mode(bad) == "off", bad


def test_catalog_switch_has_a_live_choice_that_counts_as_loosening_and_is_off_by_default():
    src = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    m = re.search(r'Flag\("SHORT_MODE".*?shipped="([a-z]+)"', src, re.S)
    assert m and m.group(1) == "off"
    assert re.search(r'Flag\("SHORT_MODE"[^)]*choices=\("off", "shadow", "live"\)', src, re.S)
    blk = src[src.index('Flag("SHORT_MODE"'):src.index('Flag("SHORT_LIVE_QTY_CAP"')]
    assert 'recommended="shadow"' in blk                                    # the preset never recommends live
    # enums are classified by the order of `choices` (strictest -> loosest): live is last, so shadow -> live and off -> live LOOSEN and need the confirm
    ch = re.search(r'Flag\("SHORT_MODE".*?choices=\(([^)]*)\)', src, re.S).group(1)
    order = [x.strip().strip('"') for x in ch.split(",")]
    assert order == ["off", "shadow", "live"]


def test_short_mode_is_protected_from_the_calibrator():
    src = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    assert re.search(r'PROTECTED[^\n]*\{\s*\n\s*"SHORT_MODE"', src)


def test_only_the_ledger_reads_short_mode_so_every_long_path_is_unchanged():
    readers = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        # step 4: capital/ledger.py reserve_capital(side="SHORT") is the one reader (fail closed unless live); nothing else may read it yet
        if rel.startswith("tests/") or rel in ("config.py", "control/catalog.py", "capital/ledger.py", "orders/short_entry.py", "orders/short_live.py"):
            continue
        if "SHORT_MODE" in p.read_text(encoding="utf-8", errors="ignore"):
            readers.append(rel)
    assert not readers, f"SHORT_MODE is read in {readers}: only capital/ledger.py, orders/short_entry.py and orders/short_live.py may read it"


def test_real_trade_stays_long_only():
    for p in (REPO / "services" / "real-trade-service").rglob("*.py"):
        if "tests" in p.parts:
            continue
        assert "SHORT_MODE" not in p.read_text(encoding="utf-8", errors="ignore"), p
