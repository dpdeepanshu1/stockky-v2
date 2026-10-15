"""group 358: PREPARE-to-Buy refresh notes (LOG-ONLY). buy_sniper.prepare_refresh_note / prepare_refresh_report say why a refreshed PREPARE row
deserves a second look (structure now fails the entry check / a P9 structure event is near the bar); the route
/data-feed/refresh-prepare-to-buy carries them in its reply and logs them, and its refresh itself never changes. The P9 trigger logic is a
mirror of decision-prediction's structure_policy.prepare_triggers - a randomised test pins the two together. Plain asserts; runs with pytest
or `python3 tests/test_group358_prepare_structure_notes.py`."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import random
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_POLICY = os.path.join(os.path.dirname(_SERVICE), "decision-prediction-service", "decision", "structure_policy.py")
_ENV = ("SNIPER_PREPARE_STRUCTURE_MODE", "SNIPER_STRUCTURE_MODE", "SNIPER_MIN_CONVICTION", "SNIPER_MIN_PRICE", "MAX_PRICE", "STRUCTURE_MAX_CHASE_ATR",
        "STRUCTURE_MAX_STRETCH_ATR", "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_MIN_RR", "STRUCTURE_BEARISH_BLOCK", "STRUCTURE_BEARISH_BOS_BARS",
        "STRUCTURE_MAX_PRICE_DEV_PCT", "STRUCTURE_MAX_AGE_DAYS", "STRUCTURE_REJECT_STOP_WIDE", "STRUCTURE_PASSTHROUGH")
_MODS = ("buy_sniper", "data_feed")
_REAL_SLEEP = asyncio.sleep

try:  # under pytest: leave os.environ / sys.modules / asyncio.sleep exactly as found
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_process_state():
        env = {k: os.environ.get(k) for k in _ENV}
        mods = {k: sys.modules.get(k) for k in _MODS}
        yield
        asyncio.sleep = _REAL_SLEEP
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
except ImportError:  # pragma: no cover
    pass


def _clean(**env):
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in env.items()})


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def bs(**env):
    _clean(**env)
    m = _load(os.path.join(_SERVICE, "buy_sniper.py"), "buy_sniper_t358")
    sys.modules["buy_sniper"] = m
    if _SERVICE not in sys.path:
        sys.path.insert(0, _SERVICE)
    return m


SUM = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "ms_chase_atr": 0.4, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0,
       "ms_stop": 96.0, "ms_target": 112.0, "ms_rr_flags": [], "ms_bos_dir": None, "ms_bos_bars": None, "ms_bos_failed": None}


def _row(**kw):
    d = dict(SUM)
    d.update(kw)
    d.update({"symbol": "ABC", "decision": "PREPARE TO BUY", "price": 100.0})
    return d


# ── note ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_no_summary_is_no_opinion():
    m = bs()
    n = m.prepare_refresh_note({"symbol": "A", "price": 100.0}, 101.0)
    assert n["opinion"] == "no_opinion" and n["headline"] is None and not n["turned_bad"] and "no structure summary" in n["no_data"]


def test_quiet_supportive_structure_has_nothing_to_say():
    m = bs()
    n = m.prepare_refresh_note(_row(), 100.0, 100.0)
    assert n["opinion"] == "ok" and not n["turned_bad"] and n["triggers"] == [] and n["headline"] is None


def test_turned_bad_at_the_fresh_price_is_flipped_when_the_old_price_passed():
    m = bs()
    n = m.prepare_refresh_note(_row(), 103.0, 100.0)   # +1.5 ATR: chase 0.4 -> 1.9 > 1.5
    assert n["turned_bad"] and n["flipped"] and "chase" in n["reasons"][0]
    assert n["headline"].startswith("Structure now blocks entry: chase") and "passed at the last stored price" in n["headline"]


def test_bad_at_both_prices_is_not_flipped():
    m = bs()
    n = m.prepare_refresh_note(_row(ms_trend="bearish"), 100.0, 99.0)
    assert n["turned_bad"] and not n["flipped"] and "passed at" not in n["headline"] and "bearish" in n["headline"]


def test_unknown_old_price_is_never_flipped():
    m = bs()
    for old in (None, 0, "x", -5):
        assert not m.prepare_refresh_note(_row(), 103.0, old)["flipped"]


def test_trigger_headline_and_priority():
    m = bs()
    n = m.prepare_refresh_note(_row(ms_bos_dir="bullish", ms_bos_bars=2, ms_bos_failed=False, ms_in_golden=True), 100.0)
    assert n["triggers"] == ["bos_near", "entering_fib"] and not n["turned_bad"]
    assert n["headline"] == "Structure event: fresh bullish break of structure, price in the Fibonacci golden zone"
    both = m.prepare_refresh_note(_row(ms_bos_dir="bullish", ms_bos_bars=2, ms_bos_failed=False, ms_chase_atr=3.0), 100.0)
    assert both["turned_bad"] and both["triggers"] == ["bos_near"] and both["headline"].startswith("Structure now blocks entry")  # blocker wins, trigger kept


def test_each_trigger_and_its_negatives():
    m = bs()
    t = lambda **k: m.prepare_triggers(_row(**k), 100.0)
    assert t(ms_bos_dir="bullish", ms_bos_bars=3, ms_bos_failed=False) == ["bos_near"]
    assert t(ms_bos_dir="bullish", ms_bos_bars=4, ms_bos_failed=False) == []
    assert t(ms_bos_dir="bullish", ms_bos_bars=1, ms_bos_failed=True) == []
    assert t(ms_bos_dir="bearish", ms_bos_bars=1, ms_bos_failed=False) == []
    assert t(ms_choch_dir="bullish", ms_choch_bars=5) == ["choch"] and t(ms_choch_dir="bullish", ms_choch_bars=6) == []
    assert t(ms_sweep_dir="bullish", ms_sweep_bars=3) == ["sweep"] and t(ms_sweep_dir="bearish", ms_sweep_bars=1) == []
    zone = dict(ms_fvg_side="bullish", ms_fvg_top=101.0, ms_fvg_bottom=98.0, ms_fvg_status="open")
    assert t(**zone) == ["entering_fvg"]
    assert t(**{**zone, "ms_fvg_status": "filled"}) == [] and t(**{**zone, "ms_fvg_top": 90.0, "ms_fvg_bottom": 88.0}) == []
    assert t(ms_ob_side="bullish", ms_ob_top=101.0, ms_ob_bottom=98.0, ms_ob_status="fresh") == ["entering_ob"]
    assert t(ms_in_golden=True) == ["entering_fib"] and t(ms_in_golden=False) == []
    assert t(ms_bos_dir="bullish", ms_bos_bars=True, ms_bos_failed=False) == []   # a bool is not a bar count


def test_stale_or_unusable_structure_shows_no_triggers():
    m = bs()
    for q in ("insufficient", "stale", "unavailable"):
        n = m.prepare_refresh_note(_row(ms_quality=q, ms_in_golden=True), 100.0)
        assert n["opinion"] == "no_opinion" and n["triggers"] == [] and n["headline"] is None
    n = m.prepare_refresh_note(_row(ms_in_golden=True), 150.0)   # +50% vs the structure close = split / bad data
    assert n["opinion"] == "no_opinion" and "split" in n["no_data"]


def test_off_mode_and_bad_mode():
    m = bs(SNIPER_PREPARE_STRUCTURE_MODE="off")
    n = m.prepare_refresh_note(_row(ms_trend="bearish"), 100.0)
    assert n["opinion"] == "off" and n["headline"] is None
    assert m.prepare_refresh_report({"ABC": _row()}, {"ABC": 100.0})["notes"] == {}
    assert bs(SNIPER_PREPARE_STRUCTURE_MODE="banana")._prep_mode() == "live"
    assert bs()._prep_mode() == "live"


def test_prepare_mode_is_independent_of_the_sniper_gate_mode():
    m = bs(SNIPER_STRUCTURE_MODE="off")   # the card gate off must not silence the refresh notes (notes use their own switch)
    assert m.prepare_refresh_note(_row(ms_trend="bearish"), 100.0)["turned_bad"] is True


def test_report_counts_and_only_interesting_rows():
    m = bs()
    rows = {"BAD": _row(), "QUIET": _row(), "EVENT": _row(ms_in_golden=True), "NOSUM": {"symbol": "NOSUM", "price": 50.0}}
    live = {"BAD": 103.0, "QUIET": 100.0, "EVENT": 100.0, "NOSUM": 50.0, "GONE": 10.0}
    rep = m.prepare_refresh_report(rows, live, {"BAD": 100.0})
    assert (rep["checked"], rep["no_opinion"], rep["turned_bad"], rep["flipped"], rep["with_triggers"]) == (3, 2, 1, 1, 1)
    assert sorted(rep["notes"]) == ["BAD", "EVENT"] and rep["mode"] == "live"


def test_garbage_never_raises():
    m = bs()
    for row in (None, [], "x", {"ms_quality": "ok"}, {"ms_quality": "ok", "ms_price": "x", "ms_atr": object()}, {"structure_summary": 5}):
        for px in (None, "x", float("nan"), -1, 100.0):
            m.prepare_refresh_note(row, px, "y")
            m.prepare_triggers(row, px)
    assert m.prepare_refresh_report(None, None)["checked"] == 0 and m.prepare_refresh_report({"A": None}, {"A": 1.0})["no_opinion"] == 1


def test_structure_summary_dict_on_the_row_is_read_too():
    m = bs()
    row = {"symbol": "ABC", "price": 100.0, "structure_summary": dict(SUM, ms_trend="bearish")}
    assert m.prepare_refresh_note(row, 100.0)["turned_bad"]


# ── drift guard: the mirror must equal the decision service's own P9 ───────────────────────────────────────────────────────

def test_triggers_match_the_decision_service_policy():
    if not os.path.exists(_POLICY):
        return  # standalone gateway checkout
    sp = _load(_POLICY, "structure_policy_t358")
    m = bs()
    rnd = random.Random(358)
    pick = lambda *xs: rnd.choice(xs)
    n_nonempty = 0
    for _ in range(4000):
        sm = {"ms_quality": "ok", "ms_price": pick(100.0, 100.0, 0, None, "x", True), "ms_atr": pick(2.0, 2.0, 0, None, -1.0, "y"),
              "ms_bos_dir": pick("bullish", "bearish", None), "ms_bos_bars": pick(0, 1, 3, 4, 9, None, True, "2"), "ms_bos_failed": pick(True, False, None, 0, 1),
              "ms_choch_dir": pick("bullish", "bearish", None), "ms_choch_bars": pick(0, 5, 6, None),
              "ms_sweep_dir": pick("bullish", "bearish", None), "ms_sweep_bars": pick(0, 3, 4, None),
              "ms_fvg_side": pick("bullish", "bearish", None), "ms_fvg_status": pick("open", "filled", "broken", "expired", None),
              "ms_fvg_top": pick(101.0, 103.0, 90.0, 0, None), "ms_fvg_bottom": pick(98.0, 97.0, 0, None, 88.0),
              "ms_ob_side": pick("bullish", "bearish", None), "ms_ob_status": pick("fresh", "filled", "broken", None),
              "ms_ob_top": pick(101.0, 99.0, None), "ms_ob_bottom": pick(98.0, 95.0, None), "ms_in_golden": pick(True, False, None)}
        px = pick(None, 100.0, 99.5, 102.0, 96.0, 0, -3, "z")
        want = sp.prepare_triggers(sp.build_structure_features(sm, px if isinstance(px, (int, float)) else None))
        got = m.prepare_triggers(sm, px)
        assert got == want, (sm, px, got, want)
        n_nonempty += 1 if want else 0
    assert n_nonempty > 500   # the comparison really exercised the triggers


# ── the route ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.content = status, body, b"x"

    def json(self):
        return self._b


def _route(quotes, feeds, candidates, patched):
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "refresh_prepare_to_buy")
    code = ast.get_source_segment(src, node)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            sym = url.rsplit("/", 1)[-1]
            if quotes.get(sym) == "boom":
                raise RuntimeError("down")
            return _Resp(200, {"cmp": quotes[sym]}) if sym in quotes else _Resp(404, {})

    def patch(sym, px):
        patched[sym] = px
        # an in-memory store may share the dict it hands out: mutate the feed row exactly like data_feed.patch_feed_price does
        if sym in feeds:
            feeds[sym]["price"] = px
        return True

    df = types.ModuleType("data_feed")
    df.find_prepare_to_buy_candidates = lambda min_score=58.0, max_score=68.0: list(candidates)
    df.get_all_stock_feeds = lambda syms: {k: feeds[k] for k in syms if k in feeds}
    df.patch_feed_price = patch
    sys.modules["data_feed"] = df

    async def _nosleep(*a, **k):
        return None
    asyncio.sleep = _nosleep

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code, self.detail = status_code, detail

    g = {"httpx": types.SimpleNamespace(AsyncClient=_Client), "HTTPException": _HTTPException, "MARKET_DATA_URL": "http://md", "logger": logging.getLogger("t358"),
         "Dict": dict, "Any": object,
         "_structure_metric": lambda *a, **k: None}  # group360: counter helper, a no-op here
    exec("from __future__ import annotations\n" + code, g)
    return g["refresh_prepare_to_buy"]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_route_reply_carries_notes_and_the_refresh_itself_is_unchanged():
    bs()
    patched = {}
    feeds = {"BAD": _row(), "QUIET": _row(), "NOSUM": {"symbol": "NOSUM", "price": 50.0}}
    out = _run(_route({"BAD": 103.0, "QUIET": 100.0, "NOSUM": 51.0}, feeds, ["BAD", "QUIET", "NOSUM"], patched)())
    assert patched == {"BAD": 103.0, "QUIET": 100.0, "NOSUM": 51.0}          # every quote still patched: log-only
    assert out["refreshed_count"] == 3 and out["updated"] == ["BAD", "QUIET", "NOSUM"] and out["message"] == "Refreshed 3/3 Prepare-to-Buy quotes"
    assert out["structure_mode"] == "live" and list(out["structure_notes"]) == ["BAD"]
    assert out["structure_notes"]["BAD"]["flipped"] is True                   # the old price was snapshotted before the patch mutated the row
    assert out["structure_counts"] == {"checked": 2, "no_opinion": 1, "turned_bad": 1, "flipped": 1, "with_triggers": 0}


def test_route_logs_the_flagged_symbols():
    bs()
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r.getMessage())
    lg = logging.getLogger("t358")
    h = _H()
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        _run(_route({"BAD": 103.0}, {"BAD": _row()}, ["BAD"], {})())
    finally:
        lg.removeHandler(h)
    assert any("prepare refresh structure BAD: Structure now blocks entry" in r for r in records)
    assert any(r.startswith("prepare refresh structure: checked=1") for r in records)


def test_route_off_mode_adds_nothing_and_still_refreshes():
    bs(SNIPER_PREPARE_STRUCTURE_MODE="off")
    patched = {}
    out = _run(_route({"BAD": 103.0}, {"BAD": _row()}, ["BAD"], patched)())
    assert patched == {"BAD": 103.0} and "structure_notes" not in out and "structure_mode" not in out and out["refreshed_count"] == 1


def test_route_survives_a_broken_notes_path():
    m = bs()
    m.prepare_refresh_report = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    patched = {}
    out = _run(_route({"BAD": 103.0}, {"BAD": _row()}, ["BAD"], patched)())
    assert patched == {"BAD": 103.0} and out["refreshed_count"] == 1 and "structure_notes" not in out


def test_route_quote_failures_are_reported_as_before():
    bs()
    out = _run(_route({"A": "boom"}, {"A": _row()}, ["A", "B"], {})())
    assert out["refreshed_count"] == 0 and len(out["errors"]) == 2 and out["structure_counts"]["checked"] == 0


def test_route_with_no_candidates_keeps_its_early_reply():
    bs()
    out = _run(_route({}, {}, [], {})())
    assert out["refreshed_count"] == 0 and out["message"] == "No Prepare-to-Buy candidates in score band" and "structure_notes" not in out


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    asyncio.sleep = _REAL_SLEEP
    print(f"{n} passed 0 failed")
