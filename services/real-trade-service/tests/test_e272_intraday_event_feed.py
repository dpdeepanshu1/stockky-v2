"""E272 round 7: market-hours news as a reader over the typed event feed (veto, trigger, 09:05 rebuild, new cadence) and the veto row kept through finalize.
Plain-function runner: python3 tests/test_e272_intraday_event_feed.py (also collected by pytest). No sqlalchemy / httpx needed: models, symbol_master, notifier, httpx and
resilience.local_cache are replaced by small stand-ins, the cadence functions are cut out of auto_pilot.py, and the wiring in entry.py / auto_pilot.py is checked by source text."""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import types
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

# ---- stand-ins (only when the real package is not installed) -----------------------------------------------------------------
try:
    import sqlalchemy  # noqa: F401
    import httpx  # noqa: F401
    _REAL = True
except ImportError:
    _REAL = False


class _Col:
    def __eq__(self, o):
        return True

    def in_(self, *a):
        return True


class _Model:
    def __init__(self, **kw):
        self.__dict__.update(kw)


_SNAP: dict = {}
if not _REAL:
    m = types.ModuleType("models")
    for n in ("TradeCandidate", "TradePosition", "NextDayWatchlistEntry", "TradeGateState"):
        setattr(m, n, type(n, (_Model,), {"symbol": _Col(), "status": _Col(), "mode": _Col(), "consumed": _Col(), "afterhours_news_scan_enabled": _Col()}))
    sys.modules["models"] = m
    sm = types.ModuleType("symbol_master")
    sm.get_all_symbols = lambda db=None: None
    sys.modules["symbol_master"] = sm
    nt = types.ModuleType("notifier")

    async def _notify(text):
        return True
    nt.notify_async = _notify
    sys.modules["notifier"] = nt
    sys.modules["httpx"] = types.ModuleType("httpx")
    lc = types.ModuleType("resilience.local_cache")
    lc.load_snapshot = lambda db, key: _SNAP.get(key)
    lc.save_snapshot = lambda db, key, data: _SNAP.__setitem__(key, data)
    sys.modules["resilience.local_cache"] = lc          # the real `resilience` package stays (circuit_breaker etc.); only the DB-backed snapshot store is replaced

import config  # noqa: E402
from watchlist_engine import event_reader as er  # noqa: E402
from watchlist_engine import intraday_news as inews  # noqa: E402
from tz_utils import ist_today_str  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _snapshot_store_stand_in(monkeypatch):
    """E283: with the real sqlalchemy installed (Docker) the stand-ins above are skipped and intraday_news read the real DB-backed snapshot store, so
    every _store() was invisible to it. Point resilience.local_cache at the in-memory _SNAP for each test only (monkeypatch undoes it)."""
    if _REAL:
        import resilience.local_cache as _lc
        monkeypatch.setattr(_lc, "load_snapshot", lambda db, key: _SNAP.get(key))
        monkeypatch.setattr(_lc, "save_snapshot", lambda db, key, data: _SNAP.__setitem__(key, data))
    yield



_AP = open(os.path.join(ROOT, "execution", "auto_pilot.py"), encoding="utf-8").read()
_AS = open(os.path.join(ROOT, "watchlist_engine", "afterhours_scan.py"), encoding="utf-8").read()
_EN = open(os.path.join(ROOT, "entry_engine", "entry.py"), encoding="utf-8").read()


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


def hit(pol, etype="results", strength=0.7, mins_ago=5, now=None, head="h"):
    now = now or datetime.now(UTC)
    return {"polarity": pol, "event_type": etype, "strength": strength, "catalyst_type": "results", "headline": head, "source": "Moneycontrol|ev=%s|p=%s|s=%s" % (etype, "+1" if pol > 0 else "-1", strength),
            "score": 0.0 if pol < 0 else round(strength * 100, 1), "published": _iso(now - timedelta(minutes=mins_ago))}


class FakeDB:
    """query(Model).filter_by(...).all() -> the rows given for that model name."""
    def __init__(self, rows=None):
        self.rows = rows or {}

    def query(self, model, *a):
        name = getattr(model, "__name__", "x")
        rows = self.rows.get(name, [])

        class Q:
            def filter_by(self_, **kw):
                return Q2([r for r in rows if all(getattr(r, k, None) == v for k, v in kw.items())])
        return Q()


class Q2:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


def _fresh_state():
    _SNAP.clear()
    inews.reset_state_for_tests()
    config.INTRADAY_EVENT_VETO = True
    config.INTRADAY_EVENT_VETO_MIN_STRENGTH = 0.40
    config.INTRADAY_EVENT_VETO_MAX_AGE_MINUTES = 360


def _store(symbols):
    _SNAP[inews.SNAPSHOT_KEY] = {"date": ist_today_str(), "symbols": symbols}
    inews.reset_state_for_tests()


# ---- entries from feed hits --------------------------------------------------------------------------------------------------
def test_entries_keep_type_polarity_strength_and_no_ranking_nudge():
    e = inews.entries_from_hits({"ABC": hit(1, "order", 0.65), "XYZ": hit(-1, "regulatory", 0.9)})
    assert e["ABC"]["kind"] == "pos" and e["ABC"]["typed"] and e["ABC"]["type"] == "order" and e["ABC"]["strength"] == 0.65 and e["ABC"]["bonus"] == 0.0
    assert e["XYZ"]["kind"] == "neg" and e["XYZ"]["polarity"] == -1 and e["XYZ"]["strength"] == 0.9


def test_unknown_polarity_generic_token_and_undated_are_skipped():
    undated = dict(hit(1), published="")
    e = inews.entries_from_hits({"A": hit(0), "IT": hit(1), "B": undated, "OK": hit(1)})
    assert set(e) == {"OK"}


# ---- merge: negative wins, veto lives longer ---------------------------------------------------------------------------------
def test_typed_negative_beats_a_later_typed_positive():
    now = datetime.now(UTC)
    neg = inews.entries_from_hits({"ABC": hit(-1, "regulatory", 0.9, mins_ago=60, now=now)})
    pos = inews.entries_from_hits({"ABC": hit(1, "order", 0.65, mins_ago=1, now=now)})
    out = inews._merge(neg, pos, now - timedelta(minutes=180), now - timedelta(minutes=360))
    assert out["ABC"]["kind"] == "neg"


def test_veto_outlives_the_news_window_but_a_positive_does_not():
    now = datetime.now(UTC)
    old = inews.entries_from_hits({"NEG": hit(-1, "regulatory", 0.9, mins_ago=240, now=now), "POS": hit(1, "order", 0.65, mins_ago=240, now=now)})
    out = inews._merge(old, {}, now - timedelta(minutes=180), now - timedelta(minutes=360))
    assert set(out) == {"NEG"}


def test_legacy_entries_keep_the_old_newest_wins_rule():
    now = datetime.now(UTC)
    a = {"X": {"kind": "neg", "bonus": -10.0, "ts": _iso(now - timedelta(minutes=30))}}
    b = {"X": {"kind": "pos", "bonus": 5.0, "ts": _iso(now - timedelta(minutes=5))}}
    assert inews._merge(a, b, now - timedelta(minutes=180))["X"]["kind"] == "pos"


def test_changed_only_reports_new_or_different():
    e = inews.entries_from_hits({"A": hit(1, head="one")})
    assert inews._changed(e, e) == {}
    assert set(inews._changed(e, inews.entries_from_hits({"A": hit(1, head="two"), "B": hit(1)}))) == {"A", "B"}


# ---- veto reader (Gate 5.9) --------------------------------------------------------------------------------------------------
def test_veto_from_snapshot_and_strength_floor():
    _fresh_state()
    _store(inews.entries_from_hits({"BAD": hit(-1, "regulatory", 0.9), "WEAK": hit(-1, "other", 0.2), "GOOD": hit(1, "order", 0.65)}))
    db = FakeDB()
    assert "negative regulatory" in inews.event_veto_reason(db, "REAL", "BAD")
    assert inews.event_veto_reason(db, "REAL", "WEAK") is None          # below INTRADAY_EVENT_VETO_MIN_STRENGTH
    assert inews.event_veto_reason(db, "REAL", "GOOD") is None          # a positive never vetoes
    assert inews.event_veto_reason(db, "REAL", "NONE") is None


def test_veto_expires_after_the_veto_window_and_on_another_day():
    _fresh_state()
    _store(inews.entries_from_hits({"OLD": hit(-1, "regulatory", 0.9, mins_ago=400)}))
    assert inews.event_veto_reason(FakeDB(), "REAL", "OLD") is None
    _SNAP[inews.SNAPSHOT_KEY]["date"] = "2020-01-01"
    inews.reset_state_for_tests()
    _store({})
    assert inews.event_veto_reason(FakeDB(), "REAL", "OLD") is None


def test_veto_row_from_the_next_day_watchlist_counts_even_when_consumed():
    _fresh_state()
    _store({})
    row = models_row("REAL", "NIGHT", er.encode_tag("Moneycontrol", "regulatory", -1, 0.9), consumed=True)
    ok = models_row("REAL", "FINE", er.encode_tag("Moneycontrol", "order", 1, 0.65))
    db = FakeDB({"NextDayWatchlistEntry": [row, ok]})
    assert "after-hours" in inews.event_veto_reason(db, "REAL", "NIGHT")
    assert inews.event_veto_reason(db, "REAL", "FINE") is None
    assert inews.event_veto_reason(db, "DEMO", "NIGHT") is None            # other mode


def test_veto_switch_off_and_unreadable_db_fail_open():
    _fresh_state()
    _store(inews.entries_from_hits({"BAD": hit(-1, "regulatory", 0.9)}))
    config.INTRADAY_EVENT_VETO = False
    assert inews.event_veto_reason(FakeDB(), "REAL", "BAD") is None
    config.INTRADAY_EVENT_VETO = True

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")
    _SNAP.clear()
    inews.reset_state_for_tests()
    assert inews.event_veto_reason(Boom(), "REAL", "BAD") is None


def test_typed_entry_adds_no_ranking_bonus():
    _fresh_state()
    _store(inews.entries_from_hits({"ABC": hit(1, "order", 0.65), "BAD": hit(-1, "regulatory", 0.9)}))
    assert inews.intraday_news_bonus(FakeDB(), "ABC") == 0.0 and inews.intraday_news_bonus(FakeDB(), "BAD") == 0.0


def models_row(mode, sym, tag, consumed=False):
    return types.SimpleNamespace(mode=mode, symbol=sym, market_date=ist_today_str(), catalyst_source=tag, headline="head", consumed=consumed)


# ---- feed freshness ----------------------------------------------------------------------------------------------------------
def test_fresh_events_use_the_announce_time_and_the_veto_window():
    now = datetime.now(UTC)
    evs = [{"symbol": "A", "published_ts": (now - timedelta(minutes=10)).timestamp()}, {"symbol": "B", "published_ts": (now - timedelta(minutes=500)).timestamp()},
           {"symbol": "C", "published": _iso(now - timedelta(minutes=300))}, "junk"]
    assert [e["symbol"] for e in inews._fresh_events({"events": evs}, now)] == ["A", "C"]


# ---- trigger -----------------------------------------------------------------------------------------------------------------
class TrigDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def query(self, *a, **k):
        class Q:
            def filter_by(s, **kw):
                return Q2([])

            def filter(s, *x):
                return Q2([])
        return Q()

    def add(self, o):
        self.added.append(o)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _at_ist(h, m):
    return datetime(2026, 11, 2, h, m, tzinfo=IST).astimezone(UTC)        # a Monday


def _trigger_env(price=100.0):
    _fresh_state()
    config.INTRADAY_EVENT_TRIGGER = True
    config.INTRADAY_EVENT_TRIGGER_MIN_STRENGTH = 0.60
    config.INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES = 30
    config.INTRADAY_EVENT_TRIGGER_MAX_PER_DAY = 3
    config.INTRADAY_EVENT_TRIGGER_TYPES = ("results", "order", "deal")
    config.INTRADAY_EVENT_TRIGGER_START_IST = "09:20"
    mf = types.ModuleType("market_feed")
    feed = types.ModuleType("market_feed.feed")

    async def preview(symbols):
        return {s: types.SimpleNamespace(price=price) for s in symbols} if price else {}
    feed.get_preview_quotes = preview
    mf.feed = feed
    sys.modules["market_feed"] = mf
    sys.modules["market_feed.feed"] = feed


def _snap_with(now, **syms):
    return {"date": ist_today_str(), "symbols": inews.entries_from_hits({k: v for k, v in syms.items()})}


def test_trigger_queues_a_fresh_strong_positive_after_0920_with_a_reference_price():
    _trigger_env()
    now = _at_ist(9, 30)
    snap = {"symbols": inews.entries_from_hits({"ABC": hit(1, "order", 0.65, mins_ago=5, now=now), "OLD": hit(1, "order", 0.65, mins_ago=45, now=now),
                                               "RAT": hit(1, "rating", 0.45, mins_ago=5, now=now), "LOW": hit(1, "order", 0.5, mins_ago=5, now=now),
                                               "BAD": hit(-1, "regulatory", 0.9, mins_ago=5, now=now)})}
    db = TrigDB()
    done = asyncio.run(inews._inject_triggers(db, snap, now, ["REAL"]))
    assert done == [("REAL", "ABC")] and len(db.added) == 1
    c = db.added[0]
    assert c.source_tab == "intraday_event" and c.signal_price == 100.0 and c.overnight_priority is False and c.mode == "REAL"
    assert snap["triggered"] == {"REAL": ["ABC"]}


def test_trigger_not_before_0920_not_twice_not_without_a_price_not_when_off():
    _trigger_env()
    ent = lambda now: {"symbols": inews.entries_from_hits({"ABC": hit(1, "order", 0.65, mins_ago=5, now=now)})}
    now = _at_ist(9, 10)
    assert asyncio.run(inews._inject_triggers(TrigDB(), ent(now), now, ["REAL"])) == []
    now = _at_ist(9, 30)
    s = ent(now)
    assert len(asyncio.run(inews._inject_triggers(TrigDB(), s, now, ["REAL"]))) == 1
    assert asyncio.run(inews._inject_triggers(TrigDB(), s, now, ["REAL"])) == []        # already triggered today
    _trigger_env(price=0)
    assert asyncio.run(inews._inject_triggers(TrigDB(), ent(now), now, ["REAL"])) == []  # no reference price: no chase
    _trigger_env()
    config.INTRADAY_EVENT_TRIGGER = False
    assert asyncio.run(inews._inject_triggers(TrigDB(), ent(now), now, ["REAL"])) == []


def test_trigger_daily_cap_per_mode():
    _trigger_env()
    config.INTRADAY_EVENT_TRIGGER_MAX_PER_DAY = 2
    now = _at_ist(10, 0)
    snap = {"symbols": inews.entries_from_hits({s: hit(1, "order", 0.65, mins_ago=5, now=now) for s in ("A1", "B1", "C1")})}
    assert len(asyncio.run(inews._inject_triggers(TrigDB(), snap, now, ["REAL"]))) == 2


# ---- cadence (cut out of auto_pilot.py) --------------------------------------------------------------------------------------
def _cadence(**cfg):
    src = _AP
    tree = ast.parse(src)
    want = {"_is_trading_day_ist", "_intraday_v2", "_intraday_end_str", "_intraday_news_window_active", "_intraday_news_next_sleep_seconds"}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
    assert {n.name for n in body} == want
    base = dict(AFTERHOURS_CADENCE_V2=True, INTRADAY_NEWS_START_IST="09:00", INTRADAY_NEWS_END_IST="15:45", INTRADAY_V2_END_IST="15:30", INTRADAY_V2_FAST_END_IST="09:20",
                INTRADAY_V2_FAST_INTERVAL_SECONDS=60, INTRADAY_V2_SLOW_INTERVAL_SECONDS=180, INTRADAY_NEWS_INTERVAL_SECONDS=900)
    base.update(cfg)
    ns = {"config": types.SimpleNamespace(**base)}
    exec(compile(ast.Module(body=body, type_ignores=[]), "auto_pilot_cut", "exec"), ns)
    return ns


def at(h, m=0, s=0):
    return datetime(2026, 11, 2, h, m, s, tzinfo=IST)


def test_v2_every_minute_to_0920_then_every_three_minutes_to_1530():
    ns = _cadence()
    f, w = ns["_intraday_news_next_sleep_seconds"], ns["_intraday_news_window_active"]
    assert w(at(9, 0)) and w(at(15, 29)) and not w(at(15, 30)) and not w(at(8, 59))
    assert f(at(9, 0)) == 60 and f(at(9, 5)) == 60
    assert f(at(9, 19, 30)) == 30                                         # capped at the 09:20 boundary, never below 30 s
    assert f(at(9, 20)) == 180 and f(at(12, 0)) == 180
    assert f(at(15, 28)) == 120                                           # capped at the 15:30 end


def test_v2_off_is_the_old_15_minute_loop_to_1545():
    ns = _cadence(AFTERHOURS_CADENCE_V2=False)
    f, w = ns["_intraday_news_next_sleep_seconds"], ns["_intraday_news_window_active"]
    assert w(at(15, 44)) and not w(at(15, 45))
    assert f(at(10, 0)) == 900 and f(at(9, 5)) == 900


def test_outside_the_window_sleeps_to_the_next_start():
    ns = _cadence()
    f = ns["_intraday_news_next_sleep_seconds"]
    assert f(at(7, 0)) == 2 * 3600 and f(at(15, 30)) == 6 * 3600


# ---- finalize keeps a veto row -----------------------------------------------------------------------------------------------
def test_finalize_wiring_keeps_vetoes_outside_the_top_n():
    assert "event_reader.is_veto" in _AS.split("async def finalize_nextday_watchlist")[1]
    body = _AS.split("async def finalize_nextday_watchlist")[1].split("shortlist = [r.symbol for r in keep]")[0]
    assert "keep = tradable[:max_picks]" in body and "discard = tradable[max_picks:]" in body


def test_finalize_behaviour_with_stand_in_rows():
    import asyncio as _a
    from watchlist_engine import afterhours_scan as ahs
    rows = [types.SimpleNamespace(symbol=f"S{i}", priority_score=float(i + 10), catalyst_source="Moneycontrol", catalyst_type="results", headline="h", consumed=False, consumed_at=None)
            for i in range(config.AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES + 2)]
    vetoes = [types.SimpleNamespace(symbol=f"V{i}", priority_score=0.0, catalyst_source=er.encode_tag("Moneycontrol", "regulatory", -1, 0.9), catalyst_type="news", headline="bad", consumed=False, consumed_at=None)
              for i in range(3)]

    class DB:
        def query(self, *a):
            class Q:
                def filter_by(s, **k):
                    return Q2(rows + vetoes)
            return Q()

        def commit(self):
            pass
    short = _a.run(ahs.finalize_nextday_watchlist(DB(), "REAL", "2026-11-02"))
    assert len(short) == config.AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES and not any(x.startswith("V") for x in short)
    assert all(v.consumed is False for v in vetoes)                       # veto rows survive unconsumed
    assert sum(1 for r in rows if r.consumed) == 2                        # only the weakest tradable rows are discarded


# ---- whole pass with a stand-in feed and database ----------------------------------------------------------------------------
class MemDB:
    """add / commit / rollback and query(Model).filter_by(**kw) / .filter(...) over in-memory rows, keyed by model class name."""
    def __init__(self):
        self.rows = {}

    def add(self, o):
        self.rows.setdefault(type(o).__name__, []).append(o)

    def commit(self):
        pass

    def rollback(self):
        pass

    def query(self, model, *cols):
        rows = self.rows.get(getattr(model, "__name__", "x"), [])

        class Q:
            def filter_by(s, **kw):
                return Q2([r for r in rows if all(getattr(r, k, None) == v for k, v in kw.items())])

            def filter(s, *a):
                return Q2(list(rows))
        return Q()


def _feed_doc(now, items):
    return {"ok": True, "events": [dict(symbol=sy, type=t, polarity=p, source=src, headline=h, published=_iso(now - timedelta(minutes=m)),
                                         published_ts=(now - timedelta(minutes=m)).timestamp()) for sy, t, p, src, h, m in items]}


def _wire_pass(monkeypatch_doc, now_ist):
    _trigger_env()
    inews._ah_fetch = None
    sm = sys.modules["symbol_master"]

    async def syms(db=None):
        return {"ABC", "BAD", "DEF", "OLDNEG"}
    sm.get_all_symbols = syms
    inews.symbol_master.get_all_symbols = syms

    async def fetch():
        return monkeypatch_doc
    inews._fetch_feed = fetch
    inews._modes_enabled = lambda db: ["REAL"]
    ap = types.ModuleType("execution.auto_pilot")
    calls = []

    async def inject(db, mode):
        calls.append(mode)
        return 1
    ap._inject_nextday_watchlist_candidates = inject
    sys.modules["execution.auto_pilot"] = ap
    pkg = sys.modules.get("execution") or types.ModuleType("execution")
    pkg.auto_pilot = ap
    sys.modules["execution"] = pkg
    return calls


def test_whole_feed_pass_stores_vetoes_and_queues_a_trigger():
    now = _at_ist(9, 30)
    doc = _feed_doc(now, [("ABC", "order", 1, "Moneycontrol", "ABC bags Rs 900 crore order", 4), ("BAD", "regulatory", -1, "LiveMint", "BAD faces SEBI probe", 3),
                          ("DEF", "other", 0, "LiveMint", "DEF holds AGM", 2)])
    _wire_pass(doc, now)
    db = MemDB()
    res = asyncio.run(inews.run_intraday_news_check(db, now=now))
    assert res["ran"] and res["source"] == "feed" and res["pos"] == 1 and res["neg"] == 1 and res["triggered"] == 1
    snap = _SNAP[inews.SNAPSHOT_KEY]
    assert set(snap["symbols"]) == {"ABC", "BAD"} and snap["triggered"] == {"REAL": ["ABC"]}
    cands = db.rows["TradeCandidate"]
    assert len(cands) == 1 and cands[0].symbol == "ABC" and cands[0].source_tab == "intraday_event"
    assert inews.event_veto_reason(db, "REAL", "BAD") and inews.event_veto_reason(db, "REAL", "ABC") is None
    # the same feed on the next tick changes nothing and triggers nothing again
    res2 = asyncio.run(inews.run_intraday_news_check(db, now=now + timedelta(minutes=3)))
    assert res2["new"] == 0 and res2["triggered"] == 0 and len(db.rows["TradeCandidate"]) == 1


def test_preopen_rebuild_runs_once_at_0905_adds_rows_and_turns_a_row_into_a_veto():
    now = _at_ist(9, 6)
    doc = _feed_doc(now, [("ABC", "order", 1, "Moneycontrol", "ABC bags order", 200), ("BAD", "regulatory", -1, "LiveMint", "BAD probe", 300),
                          ("OLDNEG", "regulatory", -1, "LiveMint", "OLDNEG fraud", 900)])
    calls = _wire_pass(doc, now)
    db = MemDB()
    tag = er.encode_tag("Moneycontrol", "order", 1, 0.65)
    db.add(_row_obj("REAL", "BAD", tag, 65.0))
    res = asyncio.run(inews.run_intraday_news_check(db, now=now))
    assert res["rebuild"] == {"added": 1, "vetoed": 2, "injected": 1} and calls == ["REAL"]
    rows = {r.symbol: r for r in db.rows["NextDayWatchlistEntry"]}
    assert er.is_veto(rows["BAD"].catalyst_source) and rows["BAD"].priority_score == 0.0       # the existing positive row became a veto
    assert er.is_veto(rows["OLDNEG"].catalyst_source) and not er.is_veto(rows["ABC"].catalyst_source)
    assert _SNAP[inews.SNAPSHOT_KEY]["rebuild_done"] == ist_today_str()
    res2 = asyncio.run(inews.run_intraday_news_check(db, now=now + timedelta(minutes=1)))
    assert "rebuild" not in res2                                            # once a day


def _row_obj(mode, sym, tag, score):
    import models
    return models.NextDayWatchlistEntry(mode=mode, symbol=sym, catalyst_type="order", catalyst_source=tag, headline="h", priority_score=score, market_date=ist_today_str(), consumed=False)


def test_feed_down_falls_back_to_the_rss_pass():
    now = _at_ist(9, 30)
    _wire_pass(None, now)
    seen = []

    async def rss(feed):
        seen.append(feed["source"])
        return []
    inews._ah._fetch_rss_items = rss
    res = asyncio.run(inews.run_intraday_news_check(MemDB(), now=now))
    assert res["ran"] and res.get("source") != "feed" and seen                # the old pass ran; nothing was vetoed or triggered
    config.INTRADAY_EVENT_SOURCE = "legacy"
    seen.clear()
    asyncio.run(inews.run_intraday_news_check(MemDB(), now=now))
    assert seen                                                               # legacy = RSS only
    config.INTRADAY_EVENT_SOURCE = "feed"


# ---- wiring by source text ---------------------------------------------------------------------------------------------------
def test_entry_has_gate_59_and_it_runs_after_the_other_event_gates():
    assert "Gate 5.9" in _EN and 'gate_tag = "event_veto"' in _EN and "event_veto_reason(db, mode, cand.symbol)" in _EN
    assert _EN.index("Gate 5.8") < _EN.index("Gate 5.9") < _EN.index("Gate 5.5: same-symbol")


def test_loop_uses_the_v2_end_and_source_in_the_log_line():
    assert "_intraday_end_str()" in _AP and "config.INTRADAY_EVENT_SOURCE" in _AP


def test_config_defaults():
    assert config.INTRADAY_EVENT_SOURCE in ("feed", "legacy") and config.INTRADAY_V2_FAST_INTERVAL_SECONDS >= 30 and config.INTRADAY_V2_SLOW_INTERVAL_SECONDS >= 60
    assert "results" in config.INTRADAY_EVENT_TRIGGER_TYPES and config.INTRADAY_EVENT_TRIGGER_MAX_PER_DAY == 3


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} tests passed")
