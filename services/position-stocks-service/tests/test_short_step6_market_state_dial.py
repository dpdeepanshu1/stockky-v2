"""Short selling step 6 (position-stocks-service): the Market Weather SHORT dial, MARKET_STATE_SHORT_MULT.
1.0 by default (no change), 0 = no short today, only ever lowers a short's risk, read at call time, FAILS CLOSED when unreadable, and never touches a long.
Runs under pytest or plainly:  cd services/position-stocks-service && python3 tests/test_short_step6_market_state_dial.py
(sqlalchemy-free: the ledger helpers are executed on their own, as tests/test_regime_hooks.py does.)"""
from __future__ import annotations

import ast
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from control import catalog, kit  # noqa: E402


@contextmanager
def _set(name, value):
    old = getattr(config, name)
    setattr(config, name, value)
    try:
        yield
    finally:
        setattr(config, name, old)


def _ledger_fns():
    src = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    want = ("_market_state_short_mult", "_short_risk_mult")
    fns = [n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name in want]
    assert len(fns) == 2
    ns = {"config": config}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "ledger(step6)", "exec"), ns)
    return ns["_market_state_short_mult"], ns["_short_risk_mult"]


def test_default_changes_nothing():
    m, r = _ledger_fns()
    assert config.MARKET_STATE_SHORT_MULT == 1.0 and m() == 1.0
    assert r("SHORT") == config.SHORT_RISK_MULT and r("LONG") == 1.0 and r(None) == 1.0


def test_dial_is_clamped_to_0_1_and_unreadable_fails_closed():
    m, _ = _ledger_fns()
    for raw, want in ((5.0, 1.0), (1.0, 1.0), (0.5, 0.5), (0.0, 0.0), (-2.0, 0.0), (float("nan"), 0.0), ("junk", 0.0), (None, 0.0)):
        with _set("MARKET_STATE_SHORT_MULT", raw):
            assert m() == want, raw


def test_a_short_risks_the_product_and_zero_means_none():
    _, r = _ledger_fns()
    with _set("MARKET_STATE_SHORT_MULT", 0.5):
        assert abs(r("SHORT") - config.SHORT_RISK_MULT * 0.5) < 1e-12
    with _set("MARKET_STATE_SHORT_MULT", 0.0):
        assert r("SHORT") == 0.0
    with _set("SHORT_RISK_MULT", float("nan")), _set("MARKET_STATE_SHORT_MULT", 1.0):
        assert r("SHORT") == 0.5                                    # step 4's fallback is unchanged


def test_a_long_never_sees_the_short_dial():
    _, r = _ledger_fns()
    with _set("MARKET_STATE_SHORT_MULT", 0.0):
        assert r("LONG") == 1.0 and r("") == 1.0 and r(None) == 1.0
        try:
            r("UP")
        except ValueError:
            pass
        else:
            raise AssertionError("an unknown side must still raise")


def test_config_parsing_is_blank_safe_and_clamped():
    for env, want in (("", 1.0), ("   ", 1.0), ("abc", 1.0), ("0", 0.0), ("0.5", 0.5), ("9", 1.0), ("-1", 0.0), ("nan", 1.0)):
        os.environ["MARKET_STATE_SHORT_MULT"] = env
        try:
            assert config._ms_float("MARKET_STATE_SHORT_MULT", 1.0, 0.0, 1.0) == want, env
        finally:
            os.environ.pop("MARKET_STATE_SHORT_MULT", None)


def test_catalog_entry_is_a_live_overlay_dial_that_zero_tightens():
    f = {x.key: x for x in catalog.CATALOG}["MARKET_STATE_SHORT_MULT"]
    assert (f.kind, f.lo, f.hi, f.looser, f.overlay, f.live, f.shipped) == ("float", 0.0, 1.0, "higher", True, True, "1.0")
    assert f.group == "Market Weather" and f.targets == (("config", "MARKET_STATE_SHORT_MULT"),) and not f.zero_off
    assert kit.canonical(f, "0") == "0" and kit.canonical(f, "0.5") == "0.5"
    assert kit.classify(f, "1", "0") == "TIGHTENS" and kit.classify(f, "1", "0.5") == "TIGHTENS" and kit.classify(f, "0.5", "0") == "TIGHTENS"
    assert kit.classify(f, "0", "1") == "LOOSENS" and kit.classify(f, "0", "0.5") == "LOOSENS" and kit.classify(f, "1", "1") == "SAME"
    for bad in ("1.5", "-0.1", "abc", ""):
        try:
            kit.canonical(f, bad)
        except Exception:  # noqa: BLE001 - the kit's own refusal type
            continue
        raise AssertionError(f"{bad!r} must be refused")


def test_reserve_capital_refuses_a_short_when_the_dial_is_zero():
    src = (ROOT / "capital" / "ledger.py").read_text(encoding="utf-8")
    a = src.index("def reserve_capital(")
    body = src[a:]
    i_mode, i_zero, i_row = body.index('!= "live"'), body.index("_side_mult <= 0"), body.index("row = _get_or_create(db)")
    assert i_mode < i_zero < i_row                                  # refused before any capital row is touched
    assert 'str(side or "").strip().upper() == "SHORT" and _side_mult <= 0' in body


def test_short_entry_refuses_first_and_sizes_with_the_dial():
    src = (ROOT / "orders" / "short_entry.py").read_text(encoding="utf-8")
    a = src.index("def _attempt(")
    body = src[a:]
    assert body.index("_market_state_short_mult() <= 0.0") < body.index("short_gates.gather_facts") < body.index("compute_levels(")
    assert '"SHORT_MARKET_STATE_OFF"' in body
    assert 'ledger._short_risk_mult("SHORT")' in src                # the shadow budget carries the dial through the product
    assert "import orders.entry" not in src and "dhan_client" not in src.replace("no broker", "")


def test_no_long_path_reads_the_short_dial():
    hits = []
    for p in ROOT.rglob("*.py"):
        if "tests" in p.parts or p.name == "config.py" or "__pycache__" in p.parts:
            continue
        if "MARKET_STATE_SHORT_MULT" in p.read_text(encoding="utf-8", errors="ignore") or "_market_state_short_mult" in p.read_text(encoding="utf-8", errors="ignore"):
            hits.append(p.relative_to(ROOT).as_posix())
    assert sorted(hits) == ["capital/ledger.py", "control/catalog.py", "orders/short_entry.py"], hits


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
