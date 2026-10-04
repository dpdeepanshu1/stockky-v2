"""
Regression tests for angelone_scrip_master.py's single-flight / backoff /
streaming-parse behaviour (2026-09-21 post-redeploy incident).

Run:  cd services/market-data-service && python -m pytest tests/test_angelone_scrip_master.py -q
"""
import http.server
import importlib
import json
import os
import random
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def sm(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELONE_SCRIP_MASTER_CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setenv("ANGELONE_SCRIP_MASTER_WAIT_S", "0.3")
    monkeypatch.setenv("ANGELONE_SCRIP_MASTER_FAIL_BACKOFF_S", "0.5")
    import angelone_scrip_master as mod
    mod = importlib.reload(mod)
    return mod


def _rows(n_eq=50, n_other=500):
    rows = []
    for i in range(n_eq):
        rows.append({"token": str(1000 + i), "symbol": f"SYM{i}-EQ", "name": f"Sym {i} “Ltd” é",
                     "expiry": "", "strike": "-1.000000", "lotsize": "1",
                     "instrumenttype": "", "exch_seg": "NSE", "tick_size": "5.000000"})
    for i in range(n_other):
        rows.append({"token": str(5000 + i), "symbol": f"FUT{i}", "name": "X",
                     "exch_seg": "NFO", "instrumenttype": "FUTSTK", "brace": "}{,]["})
    rows.append({"token": "1", "symbol": "SBIN-BE", "exch_seg": "NSE"})       # NSE but not -EQ
    rows.append({"token": "", "symbol": "NOTOKEN-EQ", "exch_seg": "NSE"})     # no token
    rows.append({"token": "9", "symbol": "BSEONLY-EQ", "exch_seg": "BSE"})    # wrong segment
    random.Random(7).shuffle(rows)
    return rows


def test_stream_parser_matches_json_loads_at_every_chunk_size(sm):
    rows = _rows()
    for dumps_kwargs in ({}, {"separators": (",", ":")}, {"indent": 2}, {"ensure_ascii": False}):
        text = json.dumps(rows, **dumps_kwargs)
        for chunk in (1, 2, 3, 7, 64, 1000, 65536, len(text)):
            parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
            assert list(sm._iter_json_array(parts)) == rows, (dumps_kwargs, chunk)
    # BOM tolerated
    assert list(sm._iter_json_array(["\ufeff", json.dumps(rows)])) == rows
    # empty array
    assert list(sm._iter_json_array(["[ ]"])) == []


def test_stream_parser_rejects_non_array_and_garbage(sm):
    with pytest.raises(ValueError):
        list(sm._iter_json_array(['{"a": 1}']))


def test_rows_to_map_filters_like_the_old_implementation(sm):
    rows = _rows()
    got = sm._rows_to_map(rows)
    expected = {r["symbol"][:-3].upper(): str(r["token"]) for r in rows
                if r.get("exch_seg") == "NSE" and r["symbol"].endswith("-EQ") and r.get("token")}
    assert got == expected and len(got) == 50


def test_cold_start_herd_triggers_exactly_one_fetch(sm, monkeypatch):
    calls = []

    def slow_fetch():
        calls.append(time.time())
        time.sleep(1.0)
        return {"SBIN": "3045", "TCS": "11536"}

    monkeypatch.setattr(sm, "_fetch_map", slow_fetch)
    results = []

    def caller():
        results.append(sm.get_token("SBIN"))

    threads = [threading.Thread(target=caller) for _ in range(150)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1, f"expected ONE fetch for 150 concurrent callers, got {len(calls)}"
    # the loader thread got the token; short-wait callers returned promptly (None) instead of piling up
    assert "3045" in results
    assert time.time() - t0 < 5
    # once loaded, everyone gets it
    assert sm.get_token("SBIN.NS") == "3045"
    assert sm.get_tokens_bulk(["tcs", "NOPE"]) == {"TCS": "11536"}


def test_failed_fetch_backs_off_instead_of_retrying_every_call(sm, monkeypatch):
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("The read operation timed out")

    monkeypatch.setattr(sm, "_fetch_map", boom)
    for _ in range(50):
        assert sm.get_token("SBIN") is None
    assert len(calls) == 1
    assert sm.status()["consecutive_failures"] == 1
    time.sleep(0.6)               # backoff (0.5s) elapsed → one more attempt allowed
    sm.get_token("SBIN")
    assert len(calls) == 2
    # recovery resets the failure state
    monkeypatch.setattr(sm, "_fetch_map", lambda: {"SBIN": "3045"})
    time.sleep(1.1)
    assert sm.get_token("SBIN") == "3045"
    assert sm.status()["consecutive_failures"] == 0


def test_background_caller_can_wait_longer_for_cold_load(sm, monkeypatch):
    monkeypatch.setattr(sm, "_fetch_map", lambda: (time.sleep(0.8), {"SBIN": "3045"})[1])
    loader = threading.Thread(target=sm.ensure_loaded)
    loader.start()
    time.sleep(0.1)
    # short-wait default (0.3s) gives up...
    assert sm.get_tokens_bulk(["SBIN"]) == {}
    # ...but the WS feed's generous wait gets the map
    assert sm.get_tokens_bulk(["SBIN"], wait_s=5) == {"SBIN": "3045"}
    loader.join()


def test_stale_map_keeps_serving_while_one_background_refresh_runs(sm, monkeypatch):
    sm._token_map = {"OLD": "1"}
    sm._loaded_at = time.time() - sm.REFRESH_INTERVAL_S - 10
    sm._disk_tried = True
    gate = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        gate.wait(5)
        return {"NEW": "2"}

    monkeypatch.setattr(sm, "_fetch_map", fetch)
    t0 = time.time()
    for _ in range(100):
        assert sm.get_token("OLD") == "1"       # never blocked, still served
    assert time.time() - t0 < 1.0
    time.sleep(0.2)
    assert len(calls) == 1                      # exactly one background refresh
    gate.set()
    for _ in range(50):
        if sm.get_token("NEW"):
            break
        time.sleep(0.05)
    assert sm.get_token("NEW") == "2"


def test_disk_snapshot_warm_start(sm, monkeypatch):
    monkeypatch.setattr(sm, "_fetch_map", lambda: {"SBIN": "3045"})
    assert sm.get_token("SBIN") == "3045"       # loads + saves snapshot
    assert os.path.exists(sm._CACHE_PATH)
    mod = importlib.reload(sm)                  # simulate in-place process restart (same container fs)
    monkeypatch.setattr(mod, "_fetch_map", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert mod.get_token("SBIN") == "3045"      # served from snapshot, zero network


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        step = 256 * 1024
        for i in range(0, len(self.payload), step):
            self.wfile.write(self.payload[i:i + step])

    def log_message(self, *a):
        pass


def test_real_http_stream_parse_is_correct_and_memory_light(sm, monkeypatch):
    import tracemalloc
    rows = _rows(n_eq=2700, n_other=250000)     # ~ tens of MB, like the real file
    payload = json.dumps(rows, separators=(",", ":")).encode()
    assert len(payload) > 25 * 1024 * 1024
    _SlowHandler.payload = payload
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(sm, "SCRIP_MASTER_URL", f"http://127.0.0.1:{srv.server_address[1]}/f.json")
        tracemalloc.start()
        got = sm._fetch_map()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert got == sm._rows_to_maps(rows)
        assert len(got[0]) == 2700
        # the old resp.json() path needs several x the payload size; streaming stays far below it
        assert peak < len(payload) * 0.5, f"peak {peak/1e6:.1f}MB vs payload {len(payload)/1e6:.1f}MB"
    finally:
        srv.shutdown()


# ── group 135: NSE "-BE" (trade-to-trade) fallback ───────────────────────────
def _be_rows():
    return [
        {"token": "3045", "symbol": "SBIN-EQ", "exch_seg": "NSE"},
        {"token": "21954", "symbol": "HFCL-BE", "exch_seg": "NSE"},            # BE only -> fallback
        {"token": "500183", "symbol": "HFCL", "exch_seg": "BSE"},              # BSE row must never be used
        {"token": "2715", "symbol": "MTARTECH-BE", "exch_seg": "NSE"},
        {"token": "7777", "symbol": "BOTH-EQ", "exch_seg": "NSE"},
        {"token": "8888", "symbol": "BOTH-BE", "exch_seg": "NSE"},             # has EQ too -> EQ wins, BE dropped
        {"token": "", "symbol": "NOTOK-BE", "exch_seg": "NSE"},                # no token -> ignored
        {"token": "9", "symbol": "XYZ-BE", "exch_seg": "BSE"},                 # wrong segment -> ignored
        {"token": "10", "symbol": "ABC-SM", "exch_seg": "NSE"},                # other series -> ignored
        "not-a-dict",
    ]


def test_rows_to_maps_splits_eq_and_be_and_eq_wins(sm):
    eq, be = sm._rows_to_maps(_be_rows())
    assert eq == {"SBIN": "3045", "BOTH": "7777"}
    assert be == {"HFCL": "21954", "MTARTECH": "2715"}
    assert sm._rows_to_map(_be_rows()) == eq


def test_be_only_names_resolve_via_get_token_and_bulk_but_not_get_all_symbols(sm, monkeypatch):
    monkeypatch.setattr(sm, "_fetch_map", lambda: sm._rows_to_maps(_be_rows()))
    assert sm.get_token("SBIN") == "3045"
    assert sm.get_token("HFCL") == "21954"          # BE fallback
    assert sm.get_token("hfcl.ns") == "21954"
    assert sm.get_token("BOTH") == "7777"           # EQ preferred
    assert sm.get_token("ABC") is None and sm.get_token("NOPE") is None
    assert sm.get_tokens_bulk(["SBIN", "HFCL", "MTARTECH", "NOPE"]) == {
        "SBIN": "3045", "HFCL": "21954", "MTARTECH": "2715"}
    assert "HFCL" not in sm.get_all_symbols()        # movers universe stays EQ-only
    st = sm.status()
    assert st["loaded_symbols"] == 2 and st["be_fallback_symbols"] == 2


def test_be_map_survives_disk_snapshot_warm_start(sm, monkeypatch):
    monkeypatch.setattr(sm, "_fetch_map", lambda: sm._rows_to_maps(_be_rows()))
    assert sm.get_token("HFCL") == "21954"          # loads + saves snapshot incl. be_map
    mod = importlib.reload(sm)
    monkeypatch.setattr(mod, "_fetch_map", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert mod.get_token("HFCL") == "21954"
    assert mod.get_token("SBIN") == "3045"


def test_old_snapshot_without_be_map_still_warm_starts(sm, monkeypatch):
    with open(sm._CACHE_PATH, "w") as f:
        json.dump({"saved_at": time.time(), "map": {"SBIN": "3045"}}, f)    # pre-group-135 format
    monkeypatch.setattr(sm, "_fetch_map", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert sm.get_token("SBIN") == "3045"
    assert sm.get_token("HFCL") is None
    assert sm.status()["be_fallback_symbols"] == 0


def test_garbage_be_map_in_snapshot_is_ignored(sm, monkeypatch):
    with open(sm._CACHE_PATH, "w") as f:
        json.dump({"saved_at": time.time(), "map": {"SBIN": "3045"}, "be_map": ["bad"]}, f)
    monkeypatch.setattr(sm, "_fetch_map", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")))
    assert sm.get_token("SBIN") == "3045"
    assert sm.status()["be_fallback_symbols"] == 0


# ── group 136: NSE "-BZ" last-resort tier ────────────────────────────────────
def _bz_rows():
    return [
        {"token": "3045", "symbol": "SBIN-EQ", "exch_seg": "NSE"},
        {"token": "766360", "symbol": "WARDINMOBI-BZ", "exch_seg": "NSE"},     # BZ only -> fallback
        {"token": "538970", "symbol": "WARDINMOBI", "exch_seg": "BSE"},         # BSE never used
        {"token": "1", "symbol": "ALL3-EQ", "exch_seg": "NSE"},
        {"token": "2", "symbol": "ALL3-BE", "exch_seg": "NSE"},
        {"token": "3", "symbol": "ALL3-BZ", "exch_seg": "NSE"},                 # EQ wins over BE and BZ
        {"token": "4", "symbol": "BEBZ-BZ", "exch_seg": "NSE"},                 # listed first on purpose
        {"token": "5", "symbol": "BEBZ-BE", "exch_seg": "NSE"},                 # BE beats BZ regardless of row order
        {"token": "6", "symbol": "X-BZ", "exch_seg": "BSE"},                    # wrong segment -> ignored
    ]


def test_bz_is_last_resort_behind_eq_and_be(sm):
    eq, be = sm._rows_to_maps(_bz_rows())
    assert eq == {"SBIN": "3045", "ALL3": "1"}
    assert be == {"WARDINMOBI": "766360", "BEBZ": "5"}


def test_bz_only_name_resolves_via_lookups_but_not_get_all_symbols(sm, monkeypatch):
    monkeypatch.setattr(sm, "_fetch_map", lambda: sm._rows_to_maps(_bz_rows()))
    assert sm.get_token("WARDINMOBI") == "766360"
    assert sm.get_token("BEBZ") == "5" and sm.get_token("ALL3") == "1"
    assert sm.get_tokens_bulk(["WARDINMOBI", "SBIN", "NOPE"]) == {"WARDINMOBI": "766360", "SBIN": "3045"}
    assert "WARDINMOBI" not in sm.get_all_symbols()
