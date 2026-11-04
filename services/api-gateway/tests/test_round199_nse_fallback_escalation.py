"""Round 199: the bhavcopy fallback window doubles while NSE keeps answering empty; a real answer resets it."""
import time
import main as gw


def _reset():
    gw._nse_securities_fallback.update({"symbols": [], "ts": 0.0, "streak": 0})


def test_ttl_doubles_with_streak_and_caps(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_MAX_S", raising=False)
    _reset()
    got = []
    for st in (0, 1, 2, 3, 4, 5, 9):
        gw._nse_securities_fallback["streak"] = st
        got.append(gw._nse_securities_fallback_ttl())
    assert got == [120, 120, 240, 480, 960, 1800, 1800]


def test_ttl_zero_stays_zero_whatever_the_streak(monkeypatch):
    monkeypatch.setenv("NSE_SECURITIES_FALLBACK_TTL_S", "0")
    gw._nse_securities_fallback["streak"] = 5
    assert gw._nse_securities_fallback_ttl() == 0.0
    _reset()


def test_streak_grows_per_fallback_and_resets_on_real_answer(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    _reset()
    resp = type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"symbols": ["ABC", "DEF"]}})()
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: resp)
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: {"data": []})
    gw._get_all_nse_securities()
    assert gw._nse_securities_fallback["streak"] == 1
    gw._nse_securities_fallback["ts"] = time.time() - 10_000          # window over
    gw._get_all_nse_securities()
    assert gw._nse_securities_fallback["streak"] == 2
    gw._nse_securities_fallback["ts"] = time.time() - 10_000
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: {"data": [{"symbol": "xyz"}]})
    assert gw._get_all_nse_securities() == ["XYZ"]
    assert gw._nse_securities_fallback["streak"] == 0
    _reset()


# ── Round 200 ────────────────────────────────────────────────────────────────

def test_circuit_skip_logs_one_warning_per_window_then_debug(monkeypatch):
    monkeypatch.delenv("NEWS_CIRCUIT_LOG_EVERY_S", raising=False)
    gw._circuit_skip_log.clear()
    seen = {"warning": [], "debug": []}
    monkeypatch.setattr(gw.logger, "warning", lambda *a, **k: seen["warning"].append(a))
    monkeypatch.setattr(gw.logger, "debug", lambda *a, **k: seen["debug"].append(a))
    err = gw.CircuitOpenError("news", 30)
    for sym in ("A", "B", "C", "D"):
        gw._log_circuit_skip("News", sym, err)
    assert len(seen["warning"]) == 1 and len(seen["debug"]) == 3


def test_circuit_skip_every_zero_logs_each_time(monkeypatch):
    monkeypatch.setenv("NEWS_CIRCUIT_LOG_EVERY_S", "0")
    gw._circuit_skip_log.clear()
    seen = []
    monkeypatch.setattr(gw.logger, "warning", lambda *a, **k: seen.append(a))
    err = gw.CircuitOpenError("news", 30)
    gw._log_circuit_skip("News", "A", err); gw._log_circuit_skip("News", "B", err)
    assert len(seen) == 2


def test_securities_index_env_switch(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    _reset()
    calls = []
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda ep, key, *a, **k: calls.append((ep, key)) or {"data": [{"symbol": "abc"}]})
    monkeypatch.setenv("NSE_SECURITIES_INDEX", "NIFTY TOTAL MARKET")
    assert gw._get_all_nse_securities() == ["ABC"]
    assert calls == [("equity-stock-indices?index=NIFTY%20TOTAL%20MARKET", "nse:all_securities:NIFTY TOTAL MARKET")]
    monkeypatch.delenv("NSE_SECURITIES_INDEX")
    calls.clear()
    gw._get_all_nse_securities()
    assert calls[0][0] == "equity-stock-indices?index=SECURITIES%20IN%20NSE" and calls[0][1] == "nse:all_securities"


def test_securities_index_off_skips_nse_and_uses_bhavcopy(monkeypatch):
    monkeypatch.delenv("NSE_SECURITIES_FALLBACK_TTL_S", raising=False)
    _reset()
    monkeypatch.setenv("NSE_SECURITIES_INDEX", "off")
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("NSE must not be asked")))
    resp = type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"symbols": ["ABC"]}})()
    monkeypatch.setattr(gw.httpx, "get", lambda *a, **k: resp)
    assert gw._get_all_nse_securities() == ["ABC"]
    _reset()
