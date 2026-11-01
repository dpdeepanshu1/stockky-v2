"""E273 round 14: the evening swing scan skips names Dhan cannot trade. Plain runner: python3 tests/test_e273_swing_scan_tradable.py (service folder)."""
from __future__ import annotations

import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, SVC)
import test_swing_lane as base  # noqa: E402

import config  # noqa: E402
from swing import engine  # noqa: E402

BE = {"HFCL", "TBZ"}


def _un(db, sym):
    return "NOT_TRADABLE_SERIES: " + sym if sym in BE else None


def _veto(**kw):
    return engine.scan_vetoes(None, untradable_fn=_un)


def _set(**kw):
    old = {k: getattr(config, k, None) for k in kw}
    for k, v in kw.items():
        setattr(config, k, v)
    return old


def _restore(old):
    for k, v in old.items():
        setattr(config, k, v)


def test_untradable_name_is_a_veto_and_tradable_is_not():
    old = _set(SWING_SCAN_TRADABLE_FILTER=True, SWING_LIVE_REAL_ENABLED=True, ENTRY_TRADABLE_GATE_ENABLED=True)
    try:
        f = _veto()
        assert f("HFCL", date(2026, 10, 26)) == ["NOT_TRADABLE_SERIES: HFCL"]
        assert f("TCS", date(2026, 10, 26)) == []
    finally:
        _restore(old)


def test_switches_that_turn_the_filter_off():
    for kw in ({"SWING_SCAN_TRADABLE_FILTER": False}, {"SWING_LIVE_REAL_ENABLED": False}, {"ENTRY_TRADABLE_GATE_ENABLED": False}):
        old = _set(SWING_SCAN_TRADABLE_FILTER=True, SWING_LIVE_REAL_ENABLED=True, ENTRY_TRADABLE_GATE_ENABLED=True)
        _set(**kw)
        try:
            assert _veto()("HFCL", date(2026, 10, 26)) == [], kw
        finally:
            _restore(old)


def test_a_failing_lookup_is_no_objection():
    def boom(db, s):
        raise RuntimeError("list down")
    f = engine.scan_vetoes(None, untradable_fn=boom)
    assert f("HFCL", date(2026, 10, 26)) == []
    f2 = engine.scan_vetoes(None, untradable_fn=lambda d, s: None)
    assert f2("HFCL", date(2026, 10, 26)) == []


def test_scan_skips_untradable_top_names_and_plans_the_next_ranked():
    old = _set(SWING_SCAN_TRADABLE_FILTER=True, SWING_LIVE_REAL_ENABLED=True, ENTRY_TRADABLE_GATE_ENABLED=True, SWING_PICKS_PER_DAY=2, SWING_BACKUP_PICKS=1)
    try:
        names = ["HFCL", "TBZ", "AAA", "BBB", "CCC", "DDD"]
        rows = [base.row(n, "2026-10-26", 100, 99, 100, rank=i + 1) for i, n in enumerate(names)]
        repo = base.MemRepo()
        r = base._scan(repo, "2026-10-26", rows, _veto())
        assert r["planned"]["planned"] == 3 and r["planned"]["vetoed"] == 2
        planned = sorted(t["symbol"] for t in repo.trades(kind="SHADOW", status="PLANNED"))
        assert planned == ["AAA", "BBB", "CCC"]
        sk = {t["symbol"]: t["skip_reason"] for t in repo.trades(kind="SHADOW", status="SKIPPED")}
        assert set(sk) == {"HFCL", "TBZ"} and "NOT_TRADABLE_SERIES" in sk["HFCL"]
    finally:
        _restore(old)


def test_deeper_candidate_pool_and_wiring_by_source():
    src = open(os.path.join(SVC, "swing", "engine.py"), encoding="utf-8").read()
    assert "max(picks_n * 4, 20)" in src and "vetoes_fn=scan_vetoes(db)" in src
    cat = open(os.path.join(SVC, "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("SWING_SCAN_TRADABLE_FILTER"' in cat
    assert config.SWING_SCAN_TRADABLE_FILTER is True


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v) and v.__module__ == "__main__":
            v(); n += 1; print("ok", k)
    print(n, "passed")
