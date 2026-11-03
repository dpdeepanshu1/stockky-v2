"""
tests/test_e285_round151_tick_budget_and_sell_stop.py

E285 round 151 (3 Nov 2026, P0). Covers:
  * feed/ws_client.py total tick budget: over-budget flag with hysteresis, thinning of idle symbols only, protected symbols
    (read by a non-bulk caller) keep every tick, the bulk scan read does not protect, the newest tick always survives a rewrite
  * tick_count / last_tick (no buffer copy), tick_stats telemetry, WS_TICK_BUDGET=0 switches the budget off
  * half-sell probe memory guard (orders/half_sell_probe._memory_refusal)
  * execution/dhan_client.place_intraday_sell_stop: the Dhan refusal "Trigger Price should be greater than Price" is retried once
    as a STOP_LOSS stop-limit under the trigger; any other refusal raises at once

No fixtures: every test restores what it changes, so the file also runs under a plain loop.
Run from services/position-stocks-service:  python3 -m pytest tests/test_e285_round151_tick_budget_and_sell_stop.py -q
"""
from __future__ import annotations

import os
import sys
import time
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANGELONE_CLIENT_ID", "")
os.environ.setdefault("ANGELONE_MPIN", "")
os.environ.setdefault("ANGELONE_API_KEY", "")
os.environ.setdefault("ANGELONE_TOTP_SECRET", "")

import feed.ws_client as wsc  # noqa: E402


def _reset(budget=1000, thin=5.0, protect=900.0):
    wsc._tick_buffers.clear()
    wsc._last_read_ts.clear()
    wsc._budget_state.update({"total": 0, "symbols": 0, "over": False, "thinned_skips": 0, "thinned_rewrites": 0,
                              "last_check": 0.0, "last_log": 0.0, "ingests": 0, "peak_total": 0, "warned_mem": False})
    wsc._TICK_BUDGET, wsc._TICK_THIN_S, wsc._TICK_PROTECT_S = budget, thin, protect


def _feed(sym, n, t0=1_000_000.0, step=0.5, px=100.0):
    for i in range(n):
        wsc._ingest_tick(sym, px + (i % 7) * 0.05, t0 + i * step, 0, None, None)


def test_tick_count_and_last_tick_do_not_copy_and_handle_empty():
    _reset()
    assert wsc.tick_count("NOPE") == 0
    assert wsc.last_tick("NOPE") is None
    _feed("AAA", 5)
    assert wsc.tick_count("AAA") == 5
    assert wsc.last_tick("AAA")[0] == 1_000_000.0 + 4 * 0.5
    assert "NOPE" not in wsc._tick_buffers          # asking never creates a buffer


def test_bulk_read_does_not_protect_but_normal_read_does():
    _reset()
    _feed("AAA", 3)
    with wsc.bulk_scan():
        wsc.get_tick_buffer("AAA")
        with wsc.bulk_scan():                         # re-entrant
            wsc.get_tick_buffer("AAA")
        wsc.get_tick_buffer("AAA")
    assert not wsc._is_protected("AAA")
    wsc.get_tick_buffer("AAA")
    assert wsc._is_protected("AAA")
    wsc._last_read_ts["AAA"] = time.monotonic() - 2000      # read long ago: protection lapsed
    assert not wsc._is_protected("AAA")


def test_bulk_scan_is_thread_local():
    import threading
    _reset()
    _feed("AAA", 3)
    _feed("BBB", 3)
    seen = {}

    def other():
        wsc.get_tick_buffer("BBB")                   # an exit reader on another thread, while the scan thread is inside bulk_scan
        seen["done"] = True
    with wsc.bulk_scan():
        wsc.get_tick_buffer("AAA")
        th = threading.Thread(target=other)
        th.start()
        th.join()
    assert seen.get("done") and wsc._is_protected("BBB") and not wsc._is_protected("AAA")


def test_engine_scans_run_inside_bulk_scan_and_work_without_it():
    import screening.engine as eng
    _reset()
    depth = {}
    orig = eng.ws_client.get_tick_buffer
    try:
        eng.ws_client.get_tick_buffer = lambda s: depth.setdefault("d", getattr(wsc._bulk_tls, "depth", 0)) and []
        wsc._token_to_symbol.clear()
        wsc._token_to_symbol["1"] = "AAA"
        eng.scan()
    finally:
        eng.ws_client.get_tick_buffer = orig
        wsc._token_to_symbol.clear()
    assert depth.get("d") == 1


def test_under_budget_keeps_every_tick():
    _reset(budget=100000)
    _feed("AAA", 3000, step=0.1)
    wsc._maintain()
    assert wsc._budget_state["over"] is False
    assert wsc.tick_count("AAA") == 3000


def test_over_budget_thins_idle_symbol_but_not_protected_one():
    _reset(budget=1500)
    _feed("IDLE", 2000, step=0.2)      # 400 s of ticks, five per second
    _feed("OPEN", 2000, step=0.2)
    wsc.get_tick_buffer("OPEN")        # an exit / stop reader touched it: protected
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc._budget_state["over"] is True
    assert wsc._budget_state["thinned_rewrites"] >= 1
    # the idle symbol was rewritten to roughly one tick per 5 s (400 s -> about 80 ticks); the protected one is untouched
    assert wsc.tick_count("IDLE") < 120
    assert wsc.tick_count("OPEN") == 2000
    # new ticks while over budget: idle is thinned at ingest, protected keeps all
    before_idle, before_open = wsc.tick_count("IDLE"), wsc.tick_count("OPEN")
    _feed("IDLE", 50, t0=1_000_500.0, step=0.2)      # continues the same session (a big time jump would age out the old ticks)
    _feed("OPEN", 50, t0=1_000_500.0, step=0.2)
    assert wsc.tick_count("IDLE") - before_idle <= 3
    assert wsc.tick_count("OPEN") - before_open == 50
    assert wsc._budget_state["thinned_skips"] > 0


def test_rewrite_keeps_newest_tick_and_order():
    _reset(budget=10)
    _feed("IDLE", 1000, step=0.2)
    newest = wsc.last_tick("IDLE")
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc.last_tick("IDLE") == newest
    ts = [t for t, _p in wsc._tick_buffers["IDLE"]]
    assert ts == sorted(ts)


def test_hysteresis_switches_off_below_85_percent():
    _reset(budget=1000)
    _feed("AAA", 1200, step=0.1)
    wsc.get_tick_buffer("AAA")                      # protected: the thinning pass leaves it alone, so the counts below stay as set
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc._budget_state["over"] is True
    wsc._tick_buffers["AAA"] = deque(list(wsc._tick_buffers["AAA"])[:900], maxlen=wsc._MAX_TICKS)   # 900 > 85% of 1000 -> still over
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc._budget_state["over"] is True
    wsc._tick_buffers["AAA"] = deque(list(wsc._tick_buffers["AAA"])[:800], maxlen=wsc._MAX_TICKS)   # below 850 -> off
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc._budget_state["over"] is False


def test_budget_zero_is_off():
    _reset(budget=0)
    _feed("AAA", 2000, step=0.1)
    wsc._budget_state["last_check"] = 0.0
    wsc._maintain()
    assert wsc._budget_state["over"] is False
    assert wsc.tick_count("AAA") == 2000


def test_maintenance_runs_from_ingest_every_n_ticks():
    _reset(budget=20)
    old = wsc._MAINT_EVERY
    wsc._MAINT_EVERY = 50
    try:
        _feed("AAA", 400, step=0.1)
    finally:
        wsc._MAINT_EVERY = old
    assert wsc._budget_state["total"] > 0           # _maintain ran from inside _ingest_tick
    assert wsc._budget_state["over"] is True


def test_tick_stats_shape_and_ws_status_carries_it():
    _reset()
    _feed("AAA", 10)
    st = wsc.tick_stats()
    for k in ("ticks_buffered", "symbols_with_ticks", "est_buffer_mb", "budget", "over_budget", "protected_symbols", "thinned_skips", "peak_ticks_buffered"):
        assert k in st
    assert st["ticks_buffered"] == 10 and st["symbols_with_ticks"] == 1
    assert "ticks" in wsc.ws_status()


def test_maintenance_failure_never_drops_a_tick():
    _reset()
    old = wsc._maintain
    wsc._maintain = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    oldn = wsc._MAINT_EVERY
    wsc._MAINT_EVERY = 1
    try:
        _feed("AAA", 5)
    finally:
        wsc._maintain, wsc._MAINT_EVERY = old, oldn
    assert wsc.tick_count("AAA") == 5


def test_proc_memory_reads_without_raising():
    m = wsc._proc_memory()
    assert isinstance(m, dict)


# ── probe memory guard ─────────────────────────────────────────────────────
def test_probe_memory_guard_refuses_above_80_percent_and_ignores_unreadable():
    import config
    from orders import half_sell_probe as hp
    old_fn = wsc._proc_memory
    try:
        wsc._proc_memory = lambda: {"cgroup_mb": 1400.0, "cgroup_limit_mb": 1600.0}
        why = hp._memory_refusal()
        assert why and "memory guard" in why and "88%" in why
        wsc._proc_memory = lambda: {"cgroup_mb": 700.0, "cgroup_limit_mb": 1600.0}
        assert hp._memory_refusal() is None
        wsc._proc_memory = lambda: {}
        assert hp._memory_refusal() is None
        wsc._proc_memory = lambda: {"cgroup_mb": 1500.0, "cgroup_limit_mb": 1600.0}
        old_pct = config.PROBE_MAX_MEMORY_PCT
        config.PROBE_MAX_MEMORY_PCT = 0.0
        try:
            assert hp._memory_refusal() is None
        finally:
            config.PROBE_MAX_MEMORY_PCT = old_pct
    finally:
        wsc._proc_memory = old_fn


# ── SELL stop fallback ─────────────────────────────────────────────────────
class _FakeClient:
    def __init__(self, reject_first=True, reject_text="Trigger Price should be greater than Price", other_reject=None):
        self.calls, self.rows, self.reject_first, self.reject_text, self.other_reject = [], [], reject_first, reject_text, other_reject

    def place_order(self, **kw):
        self.calls.append(kw)
        if self.other_reject:
            raise RuntimeError(self.other_reject)
        if self.reject_first and kw["order_type"] == "STOP_LOSS_MARKET":
            raise RuntimeError(self.reject_text)
        oid = f"OID{len(self.calls)}"
        self.rows.append({"orderId": oid, "orderType": kw["order_type"], "orderStatus": "PENDING"})
        return {"status": "success", "data": {"orderId": oid}}

    def get_order_list(self):
        return {"status": "success", "data": self.rows}


def _with_client(fake, fn):
    from execution import dhan_client as dc
    old = (dc._get_sdk_client, dc.order_rate.acquire)
    dc._get_sdk_client = lambda db: fake
    dc.order_rate.acquire = lambda *a, **k: None
    try:
        return fn(dc)
    finally:
        dc._get_sdk_client, dc.order_rate.acquire = old


def test_sell_stop_retries_once_as_stop_limit_after_the_dhan_price_rule():
    fake = _FakeClient()
    res = _with_client(fake, lambda dc: dc.place_intraday_sell_stop(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", quantity=1, trigger_price=300.0))
    assert [c["order_type"] for c in fake.calls] == ["STOP_LOSS_MARKET", "STOP_LOSS"]
    assert fake.calls[0]["price"] == 0.0
    assert 0 < fake.calls[1]["price"] < fake.calls[1]["trigger_price"]
    assert res["order_type_sent"] == "STOP_LOSS" and res["limit_price"] == fake.calls[1]["price"] and res["orderId"] == "OID2"


def test_sell_stop_market_form_is_kept_when_dhan_accepts_it():
    fake = _FakeClient(reject_first=False)
    res = _with_client(fake, lambda dc: dc.place_intraday_sell_stop(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", quantity=1, trigger_price=300.0))
    assert len(fake.calls) == 1 and res["order_type_sent"] == "STOP_LOSS_MARKET" and res["limit_price"] == 0.0


def test_sell_stop_other_refusals_raise_at_once_without_a_second_order():
    fake = _FakeClient(other_reject="Insufficient margin")
    try:
        _with_client(fake, lambda dc: dc.place_intraday_sell_stop(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", quantity=1, trigger_price=300.0))
        raised = False
    except RuntimeError as e:
        raised = "Insufficient margin" in str(e)
    assert raised and len(fake.calls) == 1


def test_sell_stop_limit_is_always_below_trigger_even_for_a_tiny_price():
    fake = _FakeClient()
    _with_client(fake, lambda dc: dc.place_intraday_sell_stop(None, is_armed=True, security_id="1", exchange_segment="NSE_EQ", quantity=1, trigger_price=12.0))
    assert fake.calls[1]["price"] < fake.calls[1]["trigger_price"]


def test_sell_stop_unarmed_still_refuses():
    from execution import dhan_client as dc
    try:
        dc.place_intraday_sell_stop(None, is_armed=False, security_id="1", exchange_segment="NSE_EQ", quantity=1, trigger_price=300.0)
        ok = False
    except dc.DhanNotArmedError:
        ok = True
    assert ok


if __name__ == "__main__":      # plain-loop runner for a sandbox without pytest
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # noqa: BLE001
                fails += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
