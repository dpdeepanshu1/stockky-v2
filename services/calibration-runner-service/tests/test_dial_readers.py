"""E98 P0 item 4 (the E38 rule): every overlay dial the Market Weather layer can send has a LIVE READER in the service it is sent to.

A dial is "wired to nothing" when the layer sends it, the service accepts it (catalog: overlay=True, live=True) and writes it into a config attribute,
but no code reads that attribute at call time (an import-time constant, a renamed variable, a deleted check). The overlay then looks applied and changes
nothing. For each key of regime.profiles.DIALS and each service it is sent to, this test proves, from the SOURCE (ast, no service imports):

  1. the service's switch catalog lists the key with overlay=True and live=True, and names the (module, attribute) the overlay writes to
  2. that attribute is defined in that module
  3. it is READ INSIDE A FUNCTION (call time, so an overlay written later is seen) in the file(s) named in READERS below
  4. the chokepoint that holds the read is itself called by the live trading path (a regex on the caller's source)

READERS is the table to extend when a dial is added: test_every_dial_has_a_reader_entry fails for a dial that has none. Negative controls at the bottom show the
checker really fails on an import-time-only read, a missing read and a dead chokepoint. Plain asserts, offline.
"""
from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

from regime import profiles  # noqa: E402

SERVICE_DIR = {"real-trade": REPO / "services" / "real-trade-service", "position-stocks": REPO / "services" / "position-stocks-service"}

# dial -> service -> {"read": files where the call-time read must be, "used": [(caller file, regex, minimum matches)]}
READERS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "MARKET_STATE_RISK_MULT": {
        "real-trade": {"read": ["risk_engine/effective_risk.py"],
                       "used": [("risk_engine/engine.py", r"effective_risk_pct \*= _market_state_risk_mult\(\)", 1),
                                ("risk_engine/effective_risk.py", r"\* market_state_risk_mult\(\)", 1),
                                ("main.py", r"market_state_risk_mult\(\)", 1)]},
        "position-stocks": {"read": ["capital/ledger.py"],
                            "used": [("capital/ledger.py", r"\* _market_state_risk_mult\(\)", 1), ("orders/short_entry.py", r"ledger\._market_state_risk_mult\(\)", 1)]},
    },
    "MARKET_STATE_CONVICTION_ADD": {
        "real-trade": {"read": ["candidate_engine/candidates.py"], "used": [("candidate_engine/candidates.py", r"score < _min_conviction\(\)", 3)]},
    },
    "OPENING_ENTRY_NOT_BEFORE_IST": {
        "real-trade": {"read": ["entry_engine/opening_guard.py"], "used": [("entry_engine/entry.py", r"opening_guard\.reason\(", 1)]},
    },
    "RISK_MAX_ENTRIES_PER_DAY": {
        "real-trade": {"read": ["risk_engine/engine.py"], "used": [("risk_engine/engine.py", r"account\.entries_today >= MAX_ENTRIES_PER_DAY", 1)]},
    },
    "ENTRY_LAST_TIME_IST": {
        "real-trade": {"read": ["entry_engine/calendar_gate.py"], "used": [("entry_engine/entry.py", r"calendar_gate\.late_entry_block_reason\(\)", 1)]},
    },
    "MAX_CONCURRENT_SCALP_POSITIONS": {
        "position-stocks": {"read": ["orders/entry.py", "capital/ledger.py"], "used": [("orders/entry.py", r"open_count >= config\.MAX_CONCURRENT_SCALP_POSITIONS", 1)]},
    },
    "SCALP_ENTRY_CUTOFF_IST": {
        "position-stocks": {"read": ["orders/calendar_gate.py"], "used": [("orders/entry.py", r"calendar_gate\.scalp_cutoff_reason\(\)", 1)]},
    },
    "MARKET_STATE_SHORT_MULT": {
        "position-stocks": {"read": ["capital/ledger.py"], "used": [("capital/ledger.py", r"\* _market_state_short_mult\(\)", 1), ("orders/short_entry.py", r"ledger\._short_risk_mult\(", 1)]},
    },
}


# ---------------------------------------------------------------------------------------------------------------- the checker
def catalog_flags(path: Path) -> Dict[str, Dict[str, Any]]:
    """{KEY: {overlay, live, targets}} from the Flag(...) calls of a control/catalog.py, read as source."""
    out: Dict[str, Dict[str, Any]] = {}
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Flag" and n.args and isinstance(n.args[0], ast.Constant):
            kw = {k.arg: k.value for k in n.keywords if k.arg}
            def lit(name: str, default: Any) -> Any:
                try:
                    return ast.literal_eval(kw[name]) if name in kw else default
                except ValueError:
                    return default
            out[n.args[0].value] = {"overlay": lit("overlay", False), "live": lit("live", False), "targets": lit("targets", ())}
    return out


def defined_at_module_level(path: Path, attr: str) -> bool:
    for n in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in n.targets):
            return True
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == attr:
            return True
    return False


def call_time_reads(path: Path, names: Set[str]) -> int:
    """How many places INSIDE A FUNCTION (or lambda) read one of `names`: config.NAME, a bare NAME load, or the string "NAME" (getattr / an env helper).
    A read at module level runs once at import and so can never see an overlay: it does not count."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    n_found = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        for x in ast.walk(fn):
            if isinstance(x, ast.Attribute) and isinstance(x.ctx, ast.Load) and x.attr in names:
                n_found += 1
            elif isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in names:
                n_found += 1
            elif isinstance(x, ast.Constant) and isinstance(x.value, str) and x.value in names:
                n_found += 1
    return n_found


def problems(dial: str, svc: str, root: Path, entry: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    flag = catalog_flags(root / "control" / "catalog.py").get(dial)
    if not flag:
        return [f"{svc}: {dial} is not in control/catalog.py"]
    if not flag["overlay"]:
        out.append(f"{svc}: {dial} is not overlay=True in the catalog (the service would refuse the layer's dial)")
    if not flag["live"]:
        out.append(f"{svc}: {dial} is not live=True (an overlay cannot change a restart-only switch)")
    if not flag["targets"]:
        return out + [f"{svc}: {dial} has no (module, attribute) target in the catalog"]
    names: Set[str] = {dial}
    for mod, attr in flag["targets"]:
        names.add(attr)
        mod_path = root / (mod.replace(".", "/") + ".py")
        if not mod_path.exists():
            out.append(f"{svc}: {dial} targets module {mod}, which does not exist")
        elif not defined_at_module_level(mod_path, attr):
            out.append(f"{svc}: {dial} targets {mod}.{attr}, which is not defined there")
    reads = {f: call_time_reads(root / f, names) for f in entry["read"]}
    if not any(reads.values()):
        out.append(f"{svc}: {dial} is read by no function in {entry['read']} (wired to nothing; import-time reads do not count)")
    for f, rx, minimum in entry["used"]:
        found = len(re.findall(rx, (root / f).read_text(encoding="utf-8")))
        if found < minimum:
            out.append(f"{svc}: the live path does not call the reader of {dial}: /{rx}/ found {found}x in {f}, needs {minimum}")
    return out


def check_dial(dial: str) -> None:
    assert dial in profiles.DIALS, dial
    for svc in profiles.DIALS[dial]["services"]:
        entry = READERS.get(dial, {}).get(svc)
        assert entry, f"{dial} is sent to {svc} but READERS has no entry for it: name the call site that reads it"
        found = problems(dial, svc, SERVICE_DIR[svc], entry)
        assert not found, "\n".join(found)
    for svc in READERS.get(dial, {}):
        assert svc in profiles.DIALS[dial]["services"], f"READERS lists {dial} for {svc}, but the layer never sends it there"


# ---------------------------------------------------------------------------------------------------------------- one test per dial
def test_reader_MARKET_STATE_RISK_MULT():
    check_dial("MARKET_STATE_RISK_MULT")


def test_reader_MARKET_STATE_CONVICTION_ADD():
    check_dial("MARKET_STATE_CONVICTION_ADD")


def test_reader_OPENING_ENTRY_NOT_BEFORE_IST():
    check_dial("OPENING_ENTRY_NOT_BEFORE_IST")


def test_reader_RISK_MAX_ENTRIES_PER_DAY():
    check_dial("RISK_MAX_ENTRIES_PER_DAY")


def test_reader_ENTRY_LAST_TIME_IST():
    check_dial("ENTRY_LAST_TIME_IST")


def test_reader_MAX_CONCURRENT_SCALP_POSITIONS():
    check_dial("MAX_CONCURRENT_SCALP_POSITIONS")


def test_reader_SCALP_ENTRY_CUTOFF_IST():
    check_dial("SCALP_ENTRY_CUTOFF_IST")


def test_reader_MARKET_STATE_SHORT_MULT():
    check_dial("MARKET_STATE_SHORT_MULT")


def test_every_dial_has_a_reader_entry():
    """A dial added to profiles.DIALS without a named reader fails here, so a new dial can never be added "wired to nothing"."""
    assert set(READERS) == set(profiles.DIALS), (sorted(set(profiles.DIALS) - set(READERS)), sorted(set(READERS) - set(profiles.DIALS)))
    for dial in profiles.DIALS:
        fn = globals().get(f"test_reader_{dial}")
        assert callable(fn), f"no test_reader_{dial} above: copy one"


# ---------------------------------------------------------------------------------------------------------------- negative controls
def _svc(tmp: Path, code: str, catalog: str, extra: Dict[str, str] | None = None) -> Path:
    (tmp / "control").mkdir(parents=True, exist_ok=True)
    (tmp / "control" / "catalog.py").write_text(catalog, encoding="utf-8")
    (tmp / "mod.py").write_text(code, encoding="utf-8")
    for name, text in (extra or {}).items():
        (tmp / name).write_text(text, encoding="utf-8")
    return tmp


CAT = 'FLAGS = [Flag("X_DIAL", "x", "g", "int", overlay=True, targets=(("mod", "X_DIAL"),), live=True, shipped="1")]\n'


def test_checker_passes_a_call_time_read():
    with tempfile.TemporaryDirectory() as d:
        root = _svc(Path(d), "X_DIAL = 1\ndef gate():\n    return X_DIAL > 0\n", CAT, {"caller.py": "gate()\n"})
        assert problems("X_DIAL", "svc", root, {"read": ["mod.py"], "used": [("caller.py", r"gate\(\)", 1)]}) == []


def test_checker_fails_an_import_time_only_read():
    with tempfile.TemporaryDirectory() as d:
        root = _svc(Path(d), "X_DIAL = 1\nLIMIT = X_DIAL * 2\n", CAT, {"caller.py": "x = 1\n"})
        p = problems("X_DIAL", "svc", root, {"read": ["mod.py"], "used": []})
        assert any("wired to nothing" in x for x in p), p


def test_checker_fails_a_dead_chokepoint_and_a_bad_catalog():
    with tempfile.TemporaryDirectory() as d:
        root = _svc(Path(d), "X_DIAL = 1\ndef gate():\n    return X_DIAL\n", CAT, {"caller.py": "pass\n"})
        p = problems("X_DIAL", "svc", root, {"read": ["mod.py"], "used": [("caller.py", r"gate\(\)", 1)]})
        assert any("does not call the reader" in x for x in p), p
    with tempfile.TemporaryDirectory() as d:
        root = _svc(Path(d), "def gate():\n    return X_DIAL\n", CAT.replace("overlay=True", "overlay=False").replace("live=True", "live=False"))
        p = problems("X_DIAL", "svc", root, {"read": ["mod.py"], "used": []})
        assert any("not overlay=True" in x for x in p) and any("not live=True" in x for x in p) and any("not defined" in x for x in p), p


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok", n)
