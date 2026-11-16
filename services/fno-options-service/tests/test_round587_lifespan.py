"""F&O round 587: startup and shutdown run from the app's lifespan (FastAPI deprecated @app.on_event): the runner is started once on boot (unless FNO_RUNNER=0)
and stopped once on shutdown, the unmanaged-REAL warning still runs first, and nothing is left on the deprecated lists."""
import pytest


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "1")
    import main
    order = []
    monkeypatch.setattr(main.engine, "warn_unmanaged_real", lambda force=False: order.append("warn"))
    monkeypatch.setattr(main.runner, "start", lambda: order.append("start"))
    monkeypatch.setattr(main.runner, "stop", lambda: order.append("stop"))
    return TestClient, main, order


def test_boot_and_shutdown_run_from_the_lifespan_in_order(fx):
    TestClient, main, order = fx
    assert not main.app.router.on_startup and not main.app.router.on_shutdown
    with TestClient(main.app):
        assert order == ["warn", "start"]               # the warning first, then the runner
    assert order == ["warn", "start", "stop"]


def test_runner_off_still_warns_and_stops_cleanly(fx, monkeypatch):
    TestClient, main, order = fx
    monkeypatch.setenv("FNO_RUNNER", "0")
    with TestClient(main.app):
        pass
    assert order == ["warn", "stop"]


def test_a_failing_unmanaged_check_does_not_stop_the_boot(fx, monkeypatch):
    TestClient, main, order = fx
    monkeypatch.setattr(main.engine, "warn_unmanaged_real", lambda force=False: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(main.app):
        assert order == ["start"]
