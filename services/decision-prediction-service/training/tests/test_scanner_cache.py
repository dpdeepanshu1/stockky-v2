"""group 80: /training-score reuses one TrainingScanner instead of loading the model per request.

Run: cd services/decision-prediction-service/training && python3 -m pytest tests/test_scanner_cache.py -q
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

app = pytest.importorskip("app")
scanner_mod = pytest.importorskip("scanner")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    app._scanner_cache.update(obj=None, ts=0.0)
    built = []

    class FakeScanner:
        def __init__(self, *a, **kw):
            built.append(self)

    monkeypatch.setattr(scanner_mod, "TrainingScanner", FakeScanner)
    yield built
    app._scanner_cache.update(obj=None, ts=0.0)


def test_one_scanner_serves_many_requests(_fresh):
    first = app._get_training_scanner()
    for _ in range(10):
        assert app._get_training_scanner() is first
    assert len(_fresh) == 1


def test_scanner_is_rebuilt_after_ttl(_fresh, monkeypatch):
    clock = [500.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    a = app._get_training_scanner()
    clock[0] += app._SCANNER_TTL_SEC + 1
    b = app._get_training_scanner()
    assert a is not b and len(_fresh) == 2


def test_promote_invalidates_the_cached_scanner(_fresh, monkeypatch):
    class Reg:
        def __init__(self, *a):
            pass

        def promote_model(self, v):
            return True

    monkeypatch.setattr(app, "HAS_MODEL_REGISTRY", True)
    monkeypatch.setattr(app, "ModelRegistry", Reg)
    a = app._get_training_scanner()
    assert app.promote_model("v9")["status"] == "success"
    b = app._get_training_scanner()
    assert a is not b


def test_failed_promote_keeps_the_cache(_fresh, monkeypatch):
    class Reg:
        def __init__(self, *a):
            pass

        def promote_model(self, v):
            return False

    monkeypatch.setattr(app, "HAS_MODEL_REGISTRY", True)
    monkeypatch.setattr(app, "ModelRegistry", Reg)
    a = app._get_training_scanner()
    with pytest.raises(app.HTTPException):
        app.promote_model("nope")
    assert app._get_training_scanner() is a
