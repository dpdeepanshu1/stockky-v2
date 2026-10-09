"""group270: optional market-data-service source for training candles (off by default)."""
import io, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import md_history as mh


class _Resp(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(json.dumps(body).encode()); self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _candles(n):
    return [{"date": f"2026-09-{i + 1:02d} 00:00", "open": 1, "high": 2, "low": 1, "close": 1.5 + i, "volume": 10} for i in range(n)]


def test_off_by_default(monkeypatch):
    monkeypatch.delenv("TRAINING_DATA_VIA_MARKET_DATA", raising=False)
    assert mh.enabled() is False
    monkeypatch.setenv("TRAINING_DATA_VIA_MARKET_DATA", "")
    assert mh.enabled() is False
    monkeypatch.setenv("TRAINING_DATA_VIA_MARKET_DATA", "1")
    assert mh.enabled() is True


def test_frame_shape_matches_yfinance(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_URL", "http://md.test/")
    seen = []
    monkeypatch.setattr(mh.urllib.request, "urlopen", lambda url, timeout=0: seen.append(url) or _Resp({"candles": _candles(5)}))
    df = mh.fetch_daily_df("tcs", "2y")
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"] and len(df) == 5
    assert str(df.index.dtype).startswith("datetime64") and df["Close"].iloc[-1] == 5.5
    assert seen == ["http://md.test/history/TCS?period=2y&interval=1d"]


def test_failures_give_empty_frame(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_URL", "http://md.test")
    monkeypatch.setattr(mh.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert mh.fetch_daily_df("TCS").empty
    monkeypatch.setattr(mh.urllib.request, "urlopen", lambda *a, **k: _Resp({"candles": []}))
    assert mh.fetch_daily_df("TCS").empty
    monkeypatch.setattr(mh.urllib.request, "urlopen", lambda *a, **k: _Resp({}, status=503))
    assert mh.fetch_daily_df("TCS").empty
    monkeypatch.delenv("MARKET_DATA_URL")
    assert mh.fetch_daily_df("TCS").empty and mh.fetch_daily_df("").empty
