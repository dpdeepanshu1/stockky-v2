"""Short selling step 3 (exit side). Stdlib only: side.py behaviour + text/ast checks that every exit path asks it."""
import ast, importlib.util, pathlib, types

ROOT = pathlib.Path(__file__).resolve().parent.parent
def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
side = _load("side_t", "orders/side.py")
def P(s=None): return types.SimpleNamespace(side=s, id=1)
def src(rel): return (ROOT / rel).read_text()

def test_legacy_sides_are_long():
    for s in (None, "", "LONG", "long", " Long "):
        assert side.position_side(P(s)) == "LONG" and side.exit_transaction_type(P(s)) == "SELL" and side.entry_transaction_type(P(s)) == "BUY"

def test_short_flips_order_sides():
    p = P("SHORT")
    assert side.is_short(p) and side.exit_transaction_type(p) == "BUY" and side.entry_transaction_type(p) == "SELL"

def test_unknown_side_raises():
    try: side.position_side(P("SIDEWAYS"))
    except ValueError: return
    raise AssertionError("unknown side must raise")

def test_pnl_long_and_short():
    assert side.pnl(P(), 100, 103, 10) == 30 and side.pnl(P("SHORT"), 100, 103, 10) == -30
    assert side.pnl(P("SHORT"), 100, 97, 10) == 30
    assert abs(side.pnl_pct(P("SHORT"), 100, 97) - 3.0) < 1e-9 and abs(side.pnl_pct(P(), 100, 97) + 3.0) < 1e-9
    assert side.pnl_pct(P(), 0, 5) == 0.0

def test_target_hit_direction():
    assert side.target_hit_at(P(), 103.0, 103.0) and not side.target_hit_at(P(), 99, 103)
    assert side.target_hit_at(P("SHORT"), 97.0, 97.0) and not side.target_hit_at(P("SHORT"), 101, 97)

def test_shorts_never_carried():
    assert side.can_carry_overnight(P()) and not side.can_carry_overnight(P("SHORT"))

def test_no_hardcoded_sell_in_exit_paths():
    for rel in ("orders/eod_squareoff.py", "orders/reconcile.py"):
        s = src(rel)
        assert 'transaction_type="SELL"' not in s, rel
        assert '.upper() != "SELL"' not in s and '.upper() == "SELL"' not in s, rel
    assert '.upper() != "BUY"' not in src("orders/reconcile.py")

def test_no_long_only_pnl_left():
    for rel in ("orders/reconcile.py", "orders/overnight_stop.py"):
        s = src(rel)
        for bad in ("(real_exit_price - pos.entry_price)", "(price - pos.entry_price) *", "(new_exit - new_entry) *", "(exit_price - pos.entry_price) * pos.quantity", "(fill_price - pos.entry_price) *"):
            assert bad not in s, (rel, bad)

def test_side_imported_everywhere_it_is_used():
    for rel in ("orders/eod_squareoff.py", "orders/reconcile.py", "orders/overnight_stop.py", "orders/stop_watchdog.py"):
        s = src(rel); ast.parse(s)
        assert "side as _side" in s or "import side as _side" in s, rel

def test_trailing_and_breakeven_are_side_aware():
    # step 3 made these skip shorts; step 4 replaced the skip with the mirrored maths (low-water mark, stop trails ABOVE it)
    for rel in ("orders/trailing.py", "orders/breakeven.py"):
        s = src(rel)
        assert "side_mod.is_short" in s and "side_mod.pnl_pct" in s, rel
    assert '"SHORT"' in src("orders/trailing.py")

def test_stop_watchdog_breach_direction():
    s = src("orders/stop_watchdog.py")
    assert "short=_side.is_short(pos)" in s and "ltp >= stop +" in s

def test_charges_short_swaps_legs():
    s = src("orders/charges_ledger.py")
    assert "charges_for_values(x, e) if short" in s and "short=" in s
    assert "short=" in src("orders/trade_report.py")

def test_eod_never_carries_or_stops_a_short():
    s = src("orders/eod_squareoff.py")
    assert "can_carry_overnight(pos)" in s and "_side.is_short(pos)" in s and "refusing to close it as CNC" in s

def test_close_all_goes_through_side_aware_path():
    s = src("orders/close_all.py")
    assert "close_position_now" in s and "place_order" not in s

def test_reconcile_rearm_levels_mirrored():
    s = src("orders/reconcile.py")
    assert "_sgn = -1.0 if _side.is_short(pos) else 1.0" in s

def test_all_edited_files_parse():
    for rel in ("orders/side.py","orders/eod_squareoff.py","orders/reconcile.py","orders/overnight_stop.py","orders/stop_watchdog.py","orders/breakeven.py","orders/trailing.py","orders/charges_ledger.py","orders/trade_report.py","main.py"):
        ast.parse(src(rel))
