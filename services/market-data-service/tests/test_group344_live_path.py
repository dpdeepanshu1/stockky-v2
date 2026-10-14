"""group344 (13 Oct log review): Dhan live_quotes writer (change-only, priority first, memory read), transient creds pause, candle cooldown base,
feed-coverage endpoint, Dhan partial-pricing breakdown."""
import time

import pytest

from dhan_data import config, live_store
from tests.test_group275_dhan_followups import _Engine, _rows, store  # noqa: F401


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE", "on")


# ------------------------------------------------------------ change-only writes
def test_unchanged_rows_are_skipped_inside_the_window(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "60")
    assert live_store._write_sync(_rows(10)) == 10
    assert live_store._write_sync(_rows(10)) == 0
    assert live_store.status()["unchanged_skipped"] == 10


def test_a_moved_price_is_written(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "60")
    live_store._write_sync(_rows(5))
    moved = _rows(5)
    moved[2]["price"] += 1.0
    assert live_store._write_sync(moved) == 1


def test_window_zero_writes_everything(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0")
    live_store._write_sync(_rows(5))
    assert live_store._write_sync(_rows(5)) == 5


def test_failed_rows_are_not_remembered_as_sent(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "60")
    store.fail_on = {1}
    assert live_store._write_sync(_rows(10)) == 0
    store.fail_on = set()
    live_store._backoff_until = 0.0        # the timeout above starts the (intended) Oracle back-off; this test is about what is remembered as sent
    assert live_store._write_sync(_rows(10)) == 10


def test_rows_become_due_again_after_the_window(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "60")
    live_store._write_sync(_rows(3))
    for k, (ltp, ts) in list(live_store._last_sent.items()):
        live_store._last_sent[k] = (ltp, ts - 120.0)
    assert live_store._write_sync(_rows(3)) == 3


# ------------------------------------------------------------ priority first
def test_priority_symbols_are_written_first_and_never_skipped(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "60")
    live_store._write_sync(_rows(20))
    assert live_store.mark_priority(["S7", "s3.NS"]) == 2
    out = live_store._filter_and_order(live_store.build_params(_rows(20)))
    syms = {p["s"] for p in out}
    assert {"S7", "S3"} <= syms and len(out) == 2        # everything else unchanged -> skipped
    assert live_store.status()["priority_rows"] >= 2


def test_priority_chunk_is_its_own_transaction(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0")
    live_store.mark_priority(["S1", "S2", "S3"])
    live_store._write_sync(_rows(250))
    assert store.executed[0] == 3


def test_priority_marks_expire(store):
    live_store.mark_priority(["S1"], ttl_s=1.0)
    live_store._prio["S1"] = time.monotonic() - 1.0
    assert "S1" not in live_store.priority_symbols()


# ------------------------------------------------------------ memory read
def test_latest_serves_a_fresh_row(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_MEM_MAX_AGE_S", "6")
    live_store._remember(_rows(3))
    q = live_store.latest("S1.NS")
    assert q["ltp"] == 101.0 and q["source"] == "dhan" and q["ohlc"]["prev_close"] == 100.0 and q["updated_at"]


def test_latest_is_none_when_old_off_or_unknown(store, monkeypatch):
    live_store._remember(_rows(3))
    live_store._latest["S1"] = (time.monotonic() - 100.0, time.time() - 100.0, live_store._latest["S1"][2])
    assert live_store.latest("S1") is None
    assert live_store.latest("NOPE") is None
    monkeypatch.setenv("DHAN_LIVE_MEM_MAX_AGE_S", "0")
    assert live_store.latest("S2") is None


def test_latest_is_none_when_dhan_is_not_the_writer(store, monkeypatch):
    live_store._remember(_rows(3))
    monkeypatch.setenv("DHAN_LIVE_WRITE", "off")
    assert live_store.latest("S1") is None


def test_submit_remembers_rows(store):
    live_store.submit(_rows(2))
    assert live_store.latest("S0") is not None


# ------------------------------------------------------------ transient creds pause
def test_transient_pause_default_and_override(monkeypatch):
    monkeypatch.delenv("DHAN_TRANSIENT_PAUSE_S", raising=False)
    assert config.transient_pause_s() == 5.0
    monkeypatch.setenv("DHAN_TRANSIENT_PAUSE_S", "")
    assert config.transient_pause_s() == 5.0
    monkeypatch.setenv("DHAN_TRANSIENT_PAUSE_S", "12")
    assert config.transient_pause_s() == 12.0
    monkeypatch.setenv("DHAN_TRANSIENT_PAUSE_S", "0")
    assert config.transient_pause_s() == 1.0


# ------------------------------------------------------------ candle cooldown base
def test_candle_base_default_is_90_and_never_below_shared(monkeypatch):
    import angelone_budget as ab
    monkeypatch.delenv("ANGELONE_CANDLE_COOLDOWN_BASE_S", raising=False)
    assert ab._candle_cooldown_base_s() == 90.0
    monkeypatch.setenv("ANGELONE_CANDLE_COOLDOWN_BASE_S", "1")
    assert ab._candle_cooldown_base_s() == ab._cooldown_base_s()
    monkeypatch.setenv("ANGELONE_CANDLE_COOLDOWN_BASE_S", "")
    assert ab._candle_cooldown_base_s() == 90.0


# ------------------------------------------------------------ feed coverage endpoint + breakdown
def test_feed_coverage_endpoint_shape(monkeypatch):
    import main
    import angelone_scrip_master as asm
    from dhan_data import scrip_master as dsm
    monkeypatch.setattr(asm, "get_token", lambda s: "123" if s == "GOOD" else None)
    monkeypatch.setattr(dsm, "ensure_loaded", lambda block=False: True)
    monkeypatch.setattr(dsm, "security_id", lambda s: ("1", 1) if s == "GOOD" else None)
    out = main.feed_coverage(main.FeedCoverageRequest(symbols=["good.ns", "BIRET", "AAKASH", ""]))["symbols"]
    by = {r["symbol"]: r for r in out}
    assert by["GOOD"]["primary_feed"] is True
    assert by["BIRET"]["primary_feed"] is False and by["BIRET"]["angelone_token"] is False
    assert len(out) == 3


def test_bulk_request_priority_defaults_false():
    import main
    assert main.BulkQuoteRequest(symbols=["A"]).priority is False
    assert main.BulkQuoteRequest(symbols=["A"], priority=True).priority is True


def test_last_call_breakdown_is_a_copy():
    from dhan_data import quotes
    quotes._last_call.update({"requested": 3})
    b = quotes.last_call_breakdown()
    b["requested"] = 99
    assert quotes._last_call["requested"] == 3


def test_group345_breakdown_is_per_calling_thread():
    import threading
    from dhan_data import quotes
    seen = {}

    def run(name, n):
        quotes._tl.last = {"requested": n, "fresh": n}
        seen[name] = quotes.last_call_breakdown()["requested"]
    ts = [threading.Thread(target=run, args=(f"t{i}", i + 1)) for i in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert seen == {"t0": 1, "t1": 2, "t2": 3}
