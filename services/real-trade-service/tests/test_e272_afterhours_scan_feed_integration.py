"""E272 round 6: run_afterhours_scan end to end with stand-ins for httpx / models / symbol_master / config (this sandbox has no sqlalchemy or httpx).
Plain-function runner: python3 tests/test_e272_afterhours_scan_feed_integration.py. Covers: feed up in "both" and "feed" mode, feed down fallback, a veto row,
a veto that a later positive cannot replace, and "legacy" never asking the feed. The real database upsert is NOT exercised (fake session)."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class Row:
    def __init__(self, **kw):
        self.priority_score = 0.0
        self.catalyst_source = None
        self.__dict__.update(kw)


class FakeQuery:
    def __init__(self, db):
        self.db = db

    def filter_by(self, **kw):
        self.kw = kw
        return self

    def first(self):
        for r in self.db.rows:
            if all(getattr(r, k, None) == v for k, v in self.kw.items() if k != "consumed"):
                return r
        return None


class FakeDB:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def query(self, _model):
        return FakeQuery(self)

    def add(self, r):
        self.rows.append(r)

    def commit(self):
        pass

    def rollback(self):
        pass


import pytest as _pytest_e277
pytestmark = _pytest_e277.mark.usefixtures("e277_restore_after_each_test")   # E277: _install() swaps config / models / notifier / watchlist_engine.* in sys.modules; undo it after each test

def _install(mode, feed_doc, rss_items=None):
    cfg = types.SimpleNamespace(AFTERHOURS_EVENT_FEED_MODE=mode, AFTERHOURS_EVENT_FEED_TIMEOUT_S=1.0, EVENT_URL="http://event", AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS=3,
                                AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS=60, AFTERHOURS_SCAN_MIN_INJECT_SCORE=30.0)
    models = types.ModuleType("models")
    models.NextDayWatchlistEntry = Row
    sm = types.ModuleType("symbol_master")

    async def all_symbols(db):
        return {"ABC", "XYZ", "LMN", "KWD"}
    sm.get_all_symbols = all_symbols
    sys.modules.update({"httpx": types.ModuleType("httpx"), "config": cfg, "models": models, "symbol_master": sm})
    notifier = types.ModuleType("notifier")

    async def notify_async(msg):
        notifier.sent.append(msg)
    notifier.notify_async = notify_async
    notifier.sent = []
    sys.modules["notifier"] = notifier
    for k in [k for k in sys.modules if k.startswith("watchlist_engine")]:
        del sys.modules[k]
    pkg = types.ModuleType("watchlist_engine")
    pkg.__path__ = [os.path.join(ROOT, "watchlist_engine")]
    sys.modules["watchlist_engine"] = pkg
    spec = importlib.util.spec_from_file_location("watchlist_engine.afterhours_scan", os.path.join(ROOT, "watchlist_engine", "afterhours_scan.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["watchlist_engine.afterhours_scan"] = mod
    spec.loader.exec_module(mod)
    from watchlist_engine import event_reader
    calls = []

    def fake_fetch(url, timeout=4.0):
        calls.append(url)
        return feed_doc
    event_reader.fetch_active = fake_fetch
    rss_calls = []

    async def fake_rss(feed):
        rss_calls.append(feed["source"])
        return list(rss_items or [])
    mod._fetch_rss_items = fake_rss

    async def no_bulk():
        return {}
    mod._fetch_bulk_deal_hits = no_bulk
    return mod, calls, rss_calls, notifier


def _ev(sym, typ, pol, src="Moneycontrol", head="h", ts=1.0):
    return {"symbol": sym, "type": typ, "polarity": pol, "source": src, "headline": head, "published_ts": ts}


DOC = {"ok": True, "events": [_ev("ABC", "results", 1, "Moneycontrol"), _ev("ABC", "results", 1, "LiveMint", head="h2"), _ev("XYZ", "regulatory", -1, "NDTVProfit", head="SEBI probe")]}


def _run(mod, db, md="2026-11-02"):
    return asyncio.run(mod.run_afterhours_scan(db, "REAL", md))


def test_both_mode_writes_typed_row_and_veto_row_and_still_runs_the_rss_scan():
    mod, calls, rss, _ = _install("both", DOC)
    db = FakeDB()
    n = _run(mod, db)
    by = {r.symbol: r for r in db.rows}
    assert n == 2 and calls == ["http://event"] and len(rss) == 4          # the four RSS feeds still ran
    assert by["ABC"].priority_score == 97.7 and "ev=results|p=+1" in by["ABC"].catalyst_source and by["ABC"].catalyst_type == "results"
    assert by["XYZ"].priority_score == 0.0 and "p=-1" in by["XYZ"].catalyst_source


def test_feed_mode_skips_rss_when_the_feed_answers():
    mod, calls, rss, _ = _install("feed", DOC)
    _run(mod, FakeDB())
    assert calls and rss == []


def test_feed_mode_falls_back_to_rss_when_the_feed_is_down():
    item = {"title": "KWD wins big order, profit jumps", "pubDate": None}
    mod, calls, rss, _ = _install("feed", None, [item])
    db = FakeDB()
    _run(mod, db)
    assert len(rss) == 4 and calls == ["http://event"]                    # fell back to the keyword scan; nothing raised


def test_legacy_mode_never_asks_the_feed():
    mod, calls, rss, _ = _install("legacy", DOC)
    _run(mod, FakeDB())
    assert calls == [] and len(rss) == 4


def test_a_stored_veto_is_not_replaced_by_a_later_positive_and_a_veto_replaces_a_positive():
    mod, *_ = _install("both", DOC)
    veto_row = Row(mode="REAL", symbol="LMN", market_date="2026-11-02", consumed=False, priority_score=0.0, catalyst_source="LiveMint|ev=regulatory|p=-1|s=0.9")
    assert mod._upsert_wins({"score": 97.0, "polarity": 1}, veto_row) is False
    assert mod._upsert_wins({"score": 0.0, "polarity": -1}, veto_row) is False      # already a veto: unchanged
    pos_row = Row(priority_score=88.0, catalyst_source="Moneycontrol|ev=order|p=+1|s=0.65")
    assert mod._upsert_wins({"score": 0.0, "polarity": -1}, pos_row) is True
    legacy_row = Row(priority_score=40.0, catalyst_source="Moneycontrol")
    assert mod._upsert_wins({"score": 50.0}, legacy_row) is True and mod._upsert_wins({"score": 30.0}, legacy_row) is False   # old rule untouched


def test_second_pass_with_a_veto_overwrites_the_stored_positive_row():
    mod, *_ = _install("both", {"ok": True, "events": [_ev("XYZ", "order", 1, "LiveMint")]})
    db = FakeDB()
    _run(mod, db)
    assert [r.symbol for r in db.rows] == ["XYZ"] and db.rows[0].priority_score == 65.0 * 1.0 or db.rows[0].priority_score == 65.0
    import watchlist_engine.event_reader as er
    er.fetch_active = lambda url, timeout=4.0: {"ok": True, "events": [_ev("XYZ", "regulatory", -1, "NDTVProfit")]}
    _run(mod, db)
    assert len(db.rows) == 1 and db.rows[0].priority_score == 0.0 and "p=-1" in db.rows[0].catalyst_source


def test_veto_line_in_the_telegram_message():
    mod, _, _, notifier = _install("both", DOC)
    mod._should_notify_scan = lambda *a, **k: True
    _run(mod, FakeDB())
    msg = notifier.sent[-1]
    assert "VETO" in msg and "XYZ" in msg


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed")
