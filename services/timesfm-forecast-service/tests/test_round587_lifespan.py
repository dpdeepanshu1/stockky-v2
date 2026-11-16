"""round 587: the startup hook runs from the app's lifespan (FastAPI deprecated @app.on_event), and nothing is left on the deprecated lists."""
from fastapi.testclient import TestClient

import main


def test_startup_runs_from_the_lifespan_not_on_event(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_load_cache", lambda: calls.append("cache"))
    monkeypatch.delenv("TSFM_PRELOAD", raising=False)
    assert not main.app.router.on_startup and not main.app.router.on_shutdown
    assert main._startup in main._STARTUP_HOOKS
    with TestClient(main.app):
        assert calls == ["cache"]                       # ran on entering the app, once
    assert calls == ["cache"]


def test_startup_function_is_still_callable_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_load_cache", lambda: calls.append(1))
    monkeypatch.delenv("TSFM_PRELOAD", raising=False)
    main._startup()
    assert calls == [1]
