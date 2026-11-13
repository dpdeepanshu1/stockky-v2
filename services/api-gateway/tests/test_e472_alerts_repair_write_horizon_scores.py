"""E472: three defects found reading the rest of gateway main.py (rounds 451+). Each is reproduced against the round 450 code first.

  1. `_quote_broadcast_loop` delivered `triggered[:5]` of the alerts `evaluate_price_alerts` returned, but that function stamps the 15 minute
     cooldown on EVERY alert that fired and saves it before it returns: with more than five alerts firing in one tick, alerts 6+ were marked
     triggered, never notified, and silent for 15 minutes. Now `_deliver_triggered_price_alerts`: five separate notifications (the storm guard),
     then ONE digest notification for the rest, and a WebSocket push for every alert.
  2. `_patch_single_stock_feed_impl` (Repair) wrote the whole row it had read at the start. `put_symbol` lets valid volatile fields win, so a
     live price patch that landed during the repair was overwritten by the older price. Now `_repair_row_for_write` applies only what repair
     changed on top of the row as stored now.
  3. The three Top-5 horizon boards at the end of a scan did arithmetic / `>=` on `horizons[h].score`, `combined_score`, `fundamental_score`
     (decision-service values, not coerced on the full-scan path): one text or NaN value raised TypeError after every symbol was scored and
     before the result was saved. Now `_scan_horizon_picks` (and `_score_sort_key` for the fallback ordering).

No fastapi needed (functions are lifted from main.py by AST and run against fakes). Plain asserts; runs with pytest or
`python3 tests/test_e472_alerts_repair_write_horizon_scores.py`."""
from __future__ import annotations

import ast
import asyncio
import logging
import math
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

_SRC = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _lift(names, ns):
    """Exec the named top-level functions / assignments of main.py into `ns` (raises AssertionError when one is missing)."""
    found = {}
    for n in _TREE.body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name in names:
            found[n.name] = n
        elif isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets):
            found[[t.id for t in n.targets if isinstance(t, ast.Name)][0]] = n
    missing = [x for x in names if x not in found]
    assert not missing, f"not found in main.py: {missing}"
    for name in names:
        exec(compile(ast.Module(body=[found[name]], type_ignores=[]), "main.py", "exec"), ns)
    return ns


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1. price alerts ─────────────────────────────────────────────────────────

class _Hub:
    def __init__(self):
        self.broadcasts = []

    async def broadcast(self, channel, payload):
        self.broadcasts.append((channel, payload))


def _alert(sym, **kw):
    d = {"symbol": sym, "current_price": 4000, "direction": "above", "target_price": 3900, "note": "n", "id": f"id-{sym}"}
    d.update(kw)
    return d


def _alert_ns(post_fails=(), post_status=200, wake_raises=False):
    posts, wakes = [], []

    def post(url, json=None, timeout=None):
        posts.append((url, json, timeout))
        if json and any(f in json["title"] for f in post_fails):
            raise RuntimeError("notify down")
        return types.SimpleNamespace(status_code=post_status)

    def wake():
        wakes.append(1)
        if wake_raises:
            raise RuntimeError("wake failed")
        return True

    hub = _Hub()
    ns = {"asyncio": asyncio, "httpx": types.SimpleNamespace(post=post), "ws_manager": hub, "NOTIFICATION_URL": "http://notif.local",
          "_wake_notification_service": wake, "logger": logging.getLogger("e472-alerts")}
    _lift(["_PRICE_ALERT_INDIVIDUAL_PER_TICK", "_price_alert_digest_text", "_deliver_triggered_price_alerts"], ns)
    return ns, posts, wakes, hub


def test_alerts_past_the_fifth_are_delivered_as_one_digest():
    ns, posts, wakes, hub = _alert_ns()
    out = run(ns["_deliver_triggered_price_alerts"]([_alert(f"S{i}") for i in range(8)]))
    titles = [p[1]["title"] for p in posts]
    assert titles[:5] == [f"Price Alert · S{i}" for i in range(5)]
    assert titles[5] == "Price Alerts · 3 more" and len(posts) == 6
    digest = posts[5][1]["message"]
    assert all(f"S{i} ₹4000 (above ₹3900)" in digest for i in (5, 6, 7)) and "S4" not in digest
    assert out == {"individual": 5, "digest": 3, "failed": 0}
    # every alert still gets its WebSocket push
    assert [p["symbol"] for c, p in hub.broadcasts if c == "alerts"] == [f"S{i}" for i in range(8)]


def test_five_or_fewer_alerts_behave_as_before():
    ns, posts, wakes, hub = _alert_ns()
    out = run(ns["_deliver_triggered_price_alerts"]([_alert("TCS")]))
    assert len(posts) == 1 and wakes == [1]
    url, body, timeout = posts[0]
    assert url == "http://notif.local/notify" and timeout == 8
    assert body == {"title": "Price Alert · TCS", "message": "⚡ TCS ₹4000 (above target ₹3900)", "channel": "all"}
    assert hub.broadcasts[-1] == ("alerts", {"type": "price_alert", "symbol": "TCS", "current_price": 4000, "target_price": 3900,
                                              "direction": "above", "note": "n", "id": "id-TCS"})
    assert out == {"individual": 1, "digest": 0, "failed": 0}


def test_empty_and_junk_input_is_a_no_op():
    ns, posts, wakes, hub = _alert_ns()
    assert run(ns["_deliver_triggered_price_alerts"](None)) == {"individual": 0, "digest": 0, "failed": 0}
    assert run(ns["_deliver_triggered_price_alerts"]([None, "x", 3])) == {"individual": 0, "digest": 0, "failed": 0}
    assert posts == [] and wakes == [] and hub.broadcasts == []


def test_a_failing_individual_alert_does_not_block_the_next_and_is_counted():
    ns, posts, wakes, hub = _alert_ns(post_fails=("AAA",))
    out = run(ns["_deliver_triggered_price_alerts"]([_alert("AAA"), _alert("BBB")]))
    assert len(posts) == 2 and out == {"individual": 1, "digest": 0, "failed": 1}
    # E489: the WebSocket push no longer depends on the notification: AAA failed to notify but still reaches the open browser tabs
    assert [p["symbol"] for c, p in hub.broadcasts] == ["AAA", "BBB"]


def test_a_http_error_from_the_notification_service_is_a_failure_not_a_delivery(caplog=None):
    ns, posts, wakes, hub = _alert_ns(post_status=500)
    out = run(ns["_deliver_triggered_price_alerts"]([_alert("AAA")]))
    assert out == {"individual": 0, "digest": 0, "failed": 1}
    assert [p["symbol"] for c, p in hub.broadcasts] == ["AAA"]   # E489: the failed notification does not cancel the WebSocket push


def test_a_failed_digest_is_counted_and_the_websocket_pushes_still_go_out():
    ns, posts, wakes, hub = _alert_ns(post_fails=("more",))
    out = run(ns["_deliver_triggered_price_alerts"]([_alert(f"S{i}") for i in range(7)]))
    assert out == {"individual": 5, "digest": 0, "failed": 2}
    assert len([1 for c, p in hub.broadcasts]) == 7


def test_a_failing_wake_skips_that_alert_only():
    ns, posts, wakes, hub = _alert_ns(wake_raises=True)
    out = run(ns["_deliver_triggered_price_alerts"]([_alert("AAA")]))
    assert posts == [] and out["failed"] == 1   # no notification went out...
    assert [p["symbol"] for c, p in hub.broadcasts] == ["AAA"]   # ...but (E489) the WebSocket push still did


def test_digest_text_is_capped_for_a_big_batch():
    ns, *_ = _alert_ns()
    text = ns["_price_alert_digest_text"]([_alert(f"S{i}") for i in range(60)])
    assert text.startswith("⚡ 60 more price alerts triggered:") and text.count("\n") == 41 and text.endswith("… and 20 more")


def test_the_quote_loop_uses_the_delivery_helper_for_every_alert():
    src = _SRC
    assert "for t in triggered[:5]:" not in src
    assert "await _deliver_triggered_price_alerts(triggered)" in src


# ── 2. repair writes only what it changed ───────────────────────────────────

def _repair_ns():
    return _lift(["_repair_row_for_write"], {})


def test_repair_row_keeps_a_price_written_during_the_repair():
    f = _repair_ns()["_repair_row_for_write"]
    original = {"symbol": "A", "last_price": 100.0, "current_price": 100.0, "price_refreshed_at": "T0", "rsi": 55.0, "volume": 10}
    patched = dict(original, roce=15.0, roce_seed=True, repair_patched=["roce"])
    fresh = dict(original, last_price=103.5, current_price=103.5, price_refreshed_at="T1", volume=250)
    out = f(original, patched, fresh)
    assert out["last_price"] == 103.5 and out["current_price"] == 103.5 and out["price_refreshed_at"] == "T1" and out["volume"] == 250
    assert out["roce"] == 15.0 and out["roce_seed"] is True and out["repair_patched"] == ["roce"]
    assert fresh["last_price"] == 103.5 and "roce" not in fresh          # inputs are not mutated
    assert original == {"symbol": "A", "last_price": 100.0, "current_price": 100.0, "price_refreshed_at": "T0", "rsi": 55.0, "volume": 10}


def test_repair_row_applies_a_price_repair_over_the_fresh_row():
    f = _repair_ns()["_repair_row_for_write"]
    original = {"symbol": "A", "rsi": 55.0}
    patched = {"symbol": "A", "rsi": 55.0, "price": 120.0, "close": 120.0}
    out = f(original, patched, {"symbol": "A", "rsi": 55.0, "sector": "X"})
    assert out == {"symbol": "A", "rsi": 55.0, "sector": "X", "price": 120.0, "close": 120.0}


def test_repair_row_removes_what_repair_removed():
    f = _repair_ns()["_repair_row_for_write"]
    original = {"symbol": "A", "rsi": 50.0, "rsi_seed": True}
    patched = {"symbol": "A", "rsi": 61.2}                               # a real RSI popped the stale seed flag
    out = f(original, patched, dict(original, last_price=9.0))
    assert out == {"symbol": "A", "rsi": 61.2, "last_price": 9.0}


def test_repair_row_falls_back_to_the_snapshot_when_the_fresh_read_is_empty():
    f = _repair_ns()["_repair_row_for_write"]
    patched = {"symbol": "A", "rsi": 61.2}
    for fresh in (None, {}, "junk", []):
        assert f({"symbol": "A"}, patched, fresh) is patched


def test_repair_impl_rereads_the_row_and_uses_the_helper_before_the_write():
    src = [n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_patch_single_stock_feed_impl"][0]
    text = ast.get_source_segment(_SRC, src)
    assert text.index("_repair_row_for_write(original_row, current, _fresh)") < text.index("store.put_symbol, base, current")
    assert "asyncio.to_thread(store.get_symbol, base)" in text.split("_repair_row_for_write(original_row")[0].rsplit("had_row", 1)[1]


def _impl_ns(store):
    """`_patch_single_stock_feed_impl` lifted with every network step switched off (no upstream URLs): a row that lacks only ROCE ends in a
    seeded ROCE, so the only thing under test is what is written back."""
    import data_feed
    ns = {"asyncio": asyncio, "math": math, "logger": logging.getLogger("e472-repair"), "datetime": __import__("datetime").datetime,
          "IST": __import__("zoneinfo").ZoneInfo("Asia/Kolkata"), "os": os, "httpx": types.SimpleNamespace(AsyncClient=object),
          "MAX_UNIVERSE_PRICE": 0.0, "REPAIR_COOLDOWN_SEC": 0.0, "DATA_FEED_TTL": 123, "MARKET_DATA_URL": "", "TECHNICAL_URL": "",
          "FUNDAMENTAL_URL": "", "NEWS_URL": "", "_feed_store": lambda: store, "_row_price_over_cap": lambda *a, **k: False,
          "_direct_yf_ok": lambda: False, "resolve_ns_ticker": lambda s: None, "ms_passthrough": types.SimpleNamespace(),
          "Optional": __import__("typing").Optional, "Dict": dict, "List": list}
    _lift(["_safe_float", "_feed_missing_fields", "_feed_resolved_price", "_repair_row_for_write", "_patch_single_stock_feed_impl"], ns)
    return ns


class _RaceStore:
    """Returns the row as stored at the first read; the second read (the one taken just before the write) sees a newer live price."""

    def __init__(self, first, later):
        self.first, self.later, self.reads, self.puts = first, later, 0, []

    def get_symbol(self, sym):
        self.reads += 1
        return dict(self.first if self.reads == 1 else self.later)

    def put_symbol(self, sym, row, ttl=None):
        self.puts.append((sym, row, ttl))


def test_repair_does_not_write_back_an_older_price_over_a_newer_live_price():
    row = {"symbol": "ABC", "price": 100.0, "last_price": 100.0, "current_price": 100.0, "price_refreshed_at": "T0", "rsi": 55.0,
           "pe_ratio": 20.0, "sentiment_score": 60.0, "volume": 10}                       # lacks ROCE only: repair seeds it, no network
    newer = dict(row, last_price=104.0, current_price=104.0, price_refreshed_at="T1", volume=900)
    store = _RaceStore(row, newer)
    ns = _impl_ns(store)
    out = run(ns["_patch_single_stock_feed_impl"]("ABC", types.SimpleNamespace()))
    assert len(store.puts) == 1
    written = store.puts[0][1]
    assert written["last_price"] == 104.0 and written["current_price"] == 104.0 and written["price_refreshed_at"] == "T1" and written["volume"] == 900
    assert written["roce"] == 15.0 and written["roce_seed"] is True and written["repair_patched"] == ["roce"]
    assert store.puts[0][2] == 123 and out["patched_fields"] == ["roce"] and out["complete"] is True


def test_repair_with_no_concurrent_change_writes_the_same_row_as_before():
    row = {"symbol": "ABC", "price": 100.0, "last_price": 100.0, "rsi": 55.0, "pe_ratio": 20.0, "sentiment_score": 60.0}
    store = _RaceStore(row, row)
    ns = _impl_ns(store)
    run(ns["_patch_single_stock_feed_impl"]("ABC", types.SimpleNamespace()))
    written = store.puts[0][1]
    assert written["price"] == 100.0 and written["last_price"] == 100.0 and written["roce"] == 15.0 and written["symbol"] == "ABC"
    assert written["repair_updated_at"].endswith("+05:30")


# ── 3. horizon boards ───────────────────────────────────────────────────────

def _horizon_ns():
    from pick_ranking import _board_rank_key
    ns = {"math": math, "_board_rank_key": _board_rank_key}

    def _hot_float(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    ns["_hot_float"] = _hot_float
    return _lift(["_score_sort_key", "_scan_num", "_scan_horizon_picks"], ns)


def _row(sym, decision="BUY NOW", combined=60, hz=None, fundamental=50, **kw):
    r = {"symbol": sym, "decision": decision, "combined_score": combined, "fundamental_score": fundamental, "close": 100.0}
    if hz is not None:
        r["horizons"] = hz
    r.update(kw)
    return r


def test_a_text_or_nan_score_no_longer_raises_in_any_board():
    ns = _horizon_ns()
    f = ns["_scan_horizon_picks"]
    rows = [
        _row("A", combined="61"),                                                    # numeric text
        _row("B", combined=float("nan")),
        _row("C", hz={"short": {"score": "n/a"}, "mid": {"score": "58"}}, combined=70),
        _row("D", fundamental="high", combined=65),
        _row("E", horizons="oops", combined=55),                                     # horizons is not a dict
        None,
    ]
    for h in ("short", "mid", "long"):
        out = f(rows, h)
        assert isinstance(out, list) and len(out) <= 5
    short = [r["symbol"] for r in f(rows, "short")]
    assert "A" in short and "C" in short                                             # "61" counts as 61; C's junk horizon score falls back to combined 70
    mid_c = [r for r in f(rows, "mid") if r["symbol"] == "C"][0]
    assert mid_c["_hz_score"] == 58.0 and mid_c["horizon_focus"] == "mid"


def test_board_rules_are_unchanged_for_normal_rows():
    f = _horizon_ns()["_scan_horizon_picks"]
    rows = [
        _row("BUY", "BUY NOW", 80),
        _row("PREP", "PREPARE TO BUY", 40),
        _row("HIGHHOLD", "HOLD", 57),
        _row("LOWHOLD", "HOLD", 40),
        _row("NOBUY", "DO NOT BUY", 99),
        _row("SELLX", "SELL", 99),
        _row("ERR", "ERROR", 99),
        _row("HZSKIP", "BUY NOW", 90, hz={"short": {"score": 10, "decision": "SELL"}}),
    ]
    short = [r["symbol"] for r in f(rows, "short")]
    assert set(short) == {"BUY", "PREP", "HIGHHOLD"}
    assert short[0] == "BUY"
    mid = {r["symbol"]: r["_hz_score"] for r in f(rows, "mid")}
    assert mid["BUY"] == 80 * 0.95 and "LOWHOLD" not in mid and "HIGHHOLD" not in mid   # 57 * 0.95 = 54.15 is under the mid bar (56)
    assert f([_row(str(i), combined=60 + i) for i in range(9)], "short", limit=3).__len__() == 3


def test_long_board_uses_fundamental_blend_and_a_zero_fundamental_falls_back():
    f = _horizon_ns()["_scan_horizon_picks"]
    a = _row("A", "BUY NOW", 60, fundamental=80)
    b = _row("B", "BUY NOW", 60, fundamental=0)
    got = {r["symbol"]: r["_hz_score"] for r in f([a, b], "long")}
    assert math.isclose(got["A"], 80 * 0.9 + 60 * 0.1) and math.isclose(got["B"], 60 * 0.9 + 60 * 0.1)


def test_a_real_zero_horizon_score_is_kept_not_replaced_by_the_combined_score():
    f = _horizon_ns()["_scan_horizon_picks"]
    r = _row("Z", "BUY NOW", 90, hz={"short": {"score": 0}})
    assert f([r], "short")[0]["_hz_score"] == 0


def test_the_fallback_ordering_uses_the_safe_sort_key():
    assert "key=_score_sort_key,   # E472" in _SRC
    assert "def _horizon_picks" not in _SRC
    sk = _horizon_ns()["_score_sort_key"]
    rows = [{"combined_score": "x"}, {"combined_score": 70}, {"combined_score": None}, {"combined_score": float("nan")}, {"combined_score": 80}]
    assert [r["combined_score"] for r in sorted(rows, key=sk, reverse=True)][:2] == [80, 70]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print(f"{len(tests)} passed")
