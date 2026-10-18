"""group441: scripts/retire_legacy_board.py. Everything runs on a TEMP COPY of position-stocks; the real tree is never written.
Plain asserts (python3 scripts/test_retire_legacy_board.py) or pytest."""
from __future__ import annotations

import importlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import retire_legacy_board as r  # noqa: E402
import board_parity_check as bpc  # noqa: E402
from test_board_parity_check import _status  # noqa: E402

REAL = SCRIPTS.parent / "services" / "position-stocks-service"


def _copy_service():
    d = Path(tempfile.mkdtemp(prefix="retire_"))
    (d / "screening").mkdir()
    shutil.copy(REAL / "screening" / "market_structure.py", d / "screening" / "market_structure.py")
    shutil.copytree(REAL / "stockky_ms", d / "stockky_ms")
    return d


def _run(d, status, apply=False):
    buf = io.StringIO()
    rc = r.run(d, status, apply, 200, 3, out=lambda s="": buf.write(str(s) + "\n"))
    return rc, buf.getvalue()


def _bars(n=300, seed=3):
    rnd, px, t, out = random.Random(seed), 100.0, 1_700_000_000.0, []
    for i in range(n):
        o = px
        big = rnd.random() < 0.08
        c = o + rnd.gauss(0, 0.3) + (rnd.choice([-1, 1]) * rnd.uniform(1.5, 3.0) if big else 0)
        h, l = max(o, c) + rnd.random() * 0.3, min(o, c) - rnd.random() * 0.3
        out.append((t + i * 300, o, h, l, c, 1000 + rnd.random() * 500))
        px = c
    return out


def _load_retired(d):
    sys.path.insert(0, str(d))
    for m in [k for k in sys.modules if k == "stockky_ms" or k.startswith("stockky_ms.") or k == "screening" or k.startswith("screening.")]:
        del sys.modules[m]
    try:
        return importlib.import_module("screening.market_structure"), importlib.import_module("stockky_ms.compat_board")
    finally:
        sys.path.remove(str(d))


def test_refuses_without_a_go_and_writes_nothing():
    d = _copy_service()
    before = (d / "screening" / "market_structure.py").read_text()
    for st in (_status("insufficient_sample", compared=50), _status("contract_break", engine_added=1), _status(fallbacks=2), _status(days=1), {}):
        for apply in (False, True):
            rc, out = _run(d, st, apply)
            assert rc == 1 and "NOT ALLOWED" in out, out
            assert (d / "screening" / "market_structure.py").read_text() == before
            assert not (d / "screening" / ("market_structure.py" + r.BACKUP_SUFFIX)).exists()


def test_dry_run_with_go_shows_the_diff_and_writes_nothing():
    d = _copy_service()
    before = (d / "screening" / "market_structure.py").read_text()
    rc, out = _run(d, _status(), apply=False)
    assert rc == 0 and "DRY RUN" in out and "+    return _cb.find_zones(" in out and "-def _is_displacement" in out
    assert (d / "screening" / "market_structure.py").read_text() == before


def test_apply_with_go_rewrites_keeps_signatures_and_backs_up():
    d = _copy_service()
    path = d / "screening" / "market_structure.py"
    before = path.read_text()
    rc, out = _run(d, _status(), apply=True)
    assert rc == 0 and "Written" in out
    new = path.read_text()
    assert (d / "screening" / ("market_structure.py" + r.BACKUP_SUFFIX)).read_text() == before
    assert "_is_displacement" not in new and r.IMPORT_LINE in new and r.is_retired(new)
    assert len(new.split("\n")) < len(before.split("\n")) - 60
    ms, cb = _load_retired(d)
    import inspect
    sig = lambda f: [(k, v.default) for k, v in inspect.signature(f).parameters.items()]  # noqa: E731
    assert sig(ms.find_zones) == [("bars", inspect.Parameter.empty), ("atr_mult", 1.5), ("max_zones", 6), ("full_range", False), ("atr_period", 14), ("swing_n", 2)]
    assert sig(ms.swings) == [("bars", inspect.Parameter.empty), ("left", 2), ("right", 2)]
    for seed in (1, 2, 3):
        bars = _bars(seed=seed)
        for fr in (False, True):
            assert ms.find_zones(bars, 1.5, 6, fr) == cb.find_zones(bars, atr_mult=1.5, max_zones=6, full_range=fr)
            assert ms.find_zones(bars, atr_mult=1.0, max_zones=3, swing_n=3) == cb.find_zones(bars, atr_mult=1.0, max_zones=3, atr_period=14, swing_n=3)
        assert ms.swings(bars) == cb.swings(bars) and ms.swings(bars, 3, 1) == cb.swings(bars, 3, 1)
    assert ms.find_zones(_bars(seed=1)) != []                      # the synthetic series does produce zones, so the equality above means something


def test_untouched_functions_are_identical_and_other_helpers_stay():
    import ast
    src = (REAL / "screening" / "market_structure.py").read_text()
    new, rep = r.retire_source(src)
    a, b = r._top_funcs(ast.parse(src)), r._top_funcs(ast.parse(new))
    assert rep["removed_helpers"] == ["_is_displacement"] and rep["retired"] == ["swings", "find_zones"]
    assert set(a) - set(b) == {"_is_displacement"}
    for n in set(b) - set(r.RETIRE):
        assert ast.dump(a[n]) == ast.dump(b[n]), n


def test_helper_is_kept_when_something_else_still_uses_it():
    src = (REAL / "screening" / "market_structure.py").read_text() + "\n\ndef other_user(b, a):\n    return _is_displacement(b, a, 1.0)\n"
    new, rep = r.retire_source(src)
    assert rep["removed_helpers"] == [] and "def _is_displacement" in new


def test_second_run_is_a_no_op():
    d = _copy_service()
    assert _run(d, _status(), True)[0] == 0
    path = d / "screening" / "market_structure.py"
    once = path.read_text()
    rc, out = _run(d, _status(), True)
    assert rc == 0 and "Already retired" in out and path.read_text() == once


def test_refuses_an_unexpected_file_or_a_missing_adapter():
    d = _copy_service()
    (d / "screening" / "market_structure.py").write_text("def other():\n    return 1\n")
    rc, out = _run(d, _status(), True)
    assert rc == 1 and "not the expected legacy file" in out
    d2 = _copy_service()
    (d2 / "stockky_ms" / "compat_board.py").unlink()
    rc, out = _run(d2, _status(), True)
    assert rc == 1 and "not vendored" in out
    d3 = _copy_service()
    (d3 / "screening" / "market_structure.py").write_text("def swings(:\n")
    assert _run(d3, _status(), True)[0] == 1
    assert _run(Path(tempfile.mkdtemp()), _status(), True)[0] == 1          # no file at all


def test_cli_reads_a_status_file_and_never_writes_the_real_tree():
    real = REAL / "screening" / "market_structure.py"
    before = real.read_text()
    d = _copy_service()
    f = d / "status.json"
    f.write_text(json.dumps(_status()))
    assert r.main(["--file", str(f), "--service-dir", str(d)]) == 0                 # dry run
    assert r.is_retired((d / "screening" / "market_structure.py").read_text()) is False
    assert r.main(["--file", str(f), "--service-dir", str(d), "--apply"]) == 0
    assert r.is_retired((d / "screening" / "market_structure.py").read_text())
    assert r.main([]) == 1 and r.main(["--file", str(d / "nope.json")]) == 1
    assert real.read_text() == before


def test_the_real_tree_is_still_legacy_in_the_shipped_zip():
    assert r.is_retired((REAL / "screening" / "market_structure.py").read_text()) is False


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
