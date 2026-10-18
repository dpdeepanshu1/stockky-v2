import json
import os
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

import backends
import main
from test_engine_backends import _candles


def _recent_candles(n=120):
    d = main._ist_today() - timedelta(days=1)          # end on the last weekday before today (IST) so cached records count as fresh
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return _candles(n, last_day=d)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CACHE_PATH", str(tmp_path / "cache.json"))
    monkeypatch.setattr(main, "_records", {})
    monkeypatch.setattr(main, "_backend", backends.FakeBackend())
    monkeypatch.setattr(main, "_backend_error", None)
    calls = []
    cs = _recent_candles()

    def fetch(sym):
        calls.append(sym)
        if sym == "NOHIST":
            raise main.HTTPException(status_code=404, detail="no history for symbol")
        return cs
    monkeypatch.setattr(main, "_fetch_candles", fetch)
    c = TestClient(main.app)
    c.calls = calls
    return c


def test_features_cached_only_is_404_until_a_batch_ran(client):
    assert client.get("/features/GOODCO").status_code == 404
    assert client.calls == []                                 # the hot path never fetched history or ran a model
    r = client.post("/batch", json={"symbols": ["GOODCO", "nohist", "GOODCO"]})
    j = r.json()
    assert r.status_code == 200 and "GOODCO" in j["done"] and j["failed"][0]["symbol"] == "NOHIST" and j["failed"][0]["status"] == 404
    f = client.get("/features/GOODCO", params={"stop_pct": 3, "target_pct": 6}).json()
    assert f["symbol"] == "GOODCO" and f["p_stop_close_max"] is not None and f["provenance"]["backend"] == "fake"


def test_batch_skips_a_symbol_already_current_and_force_recomputes(client):
    client.post("/batch", json={"symbols": ["GOODCO"]})
    j = client.post("/batch", json={"symbols": ["GOODCO"]}).json()
    assert j["skipped"] == ["GOODCO"] and j["done"] == []
    assert client.post("/batch", json={"symbols": ["GOODCO"], "force": True}).json()["done"] == ["GOODCO"]


def test_cache_is_saved_atomically_and_reloaded(client, tmp_path, monkeypatch):
    client.post("/batch", json={"symbols": ["GOODCO"]})
    doc = json.loads((tmp_path / "cache.json").read_text())
    assert "GOODCO" in doc["records"] and not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    monkeypatch.setattr(main, "_records", {})
    main._load_cache()
    assert client.get("/features/GOODCO").status_code == 200


def test_stale_record_is_404_on_the_hot_path(client):
    client.post("/batch", json={"symbols": ["GOODCO"]})
    main._records["GOODCO"]["as_of"] = (date.today() - timedelta(days=30)).isoformat()
    r = client.get("/features/GOODCO")
    assert r.status_code == 404 and "stale" in r.json()["detail"]


def test_cached_only_false_computes_on_demand(client):
    r = client.get("/features/NEWCO", params={"cached_only": "false"})
    assert r.status_code == 200 and client.calls == ["NEWCO"]


def test_backend_unavailable_is_503_and_health_says_why(client, monkeypatch):
    monkeypatch.setattr(main, "_backend", None)
    monkeypatch.setattr(main, "_backend_error", "research-3.0 weights are not licensed for production use")
    assert client.get("/features/X", params={"cached_only": "false"}).status_code == 503
    h = client.get("/health").json()
    assert h["status"] == "ok" and "licensed" in h["backend_error"]


def test_batch_limit_is_applied(client, monkeypatch):
    monkeypatch.setattr(main, "BATCH_MAX", 2)
    j = client.post("/batch", json={"symbols": ["A", "B", "C", "D"]}).json()
    assert j["processed"] == 2 and j["truncated_to"] == 2


def test_stateless_forecast_uses_only_the_candles_given(client):
    cs = _candles(130)
    full = client.post("/forecast", json={"symbol": "X", "candles": cs, "stop_pct": 3, "target_pct": 6}).json()
    cut = client.post("/forecast", json={"symbol": "X", "candles": cs[:120]}).json()
    assert full["as_of"] == cs[129]["date"] and cut["as_of"] == cs[119]["date"]
    assert cut["p_stop_close_max"] is None and full["volume_forecast"] is not None
    assert client.calls == [] and main._records == {}          # nothing fetched, nothing stored
    assert client.post("/forecast", json={"symbol": "X", "candles": cs[:10]}).status_code == 422
