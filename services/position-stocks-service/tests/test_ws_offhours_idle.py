"""position-stocks WS idles outside market hours instead of reconnecting every ~2 min (2026-10-04)."""
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from feed import ws_client  # noqa: E402


def _at(monkeypatch, y, mo, d, h, mi):
    """Freeze 'now' to an IST wall-clock time."""
    from datetime import timedelta
    ist = datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=5, minutes=30)))

    class _D(datetime):
        @classmethod
        def now(cls, tz=None):
            return ist.astimezone(tz) if tz else ist.replace(tzinfo=None)
    import datetime as _dtm
    monkeypatch.setattr(_dtm, "datetime", _D)


def test_weekday_in_window_connects(monkeypatch):
    monkeypatch.delenv("POSITION_WS_OFFHOURS_IDLE", raising=False)
    _at(monkeypatch, 2026, 10, 5, 10, 0)           # Monday 10:00 IST
    assert ws_client._offhours_idle() is False


def test_window_edges(monkeypatch):
    monkeypatch.delenv("POSITION_WS_OFFHOURS_IDLE", raising=False)
    _at(monkeypatch, 2026, 10, 5, 8, 54)
    assert ws_client._offhours_idle() is True
    _at(monkeypatch, 2026, 10, 5, 8, 55)
    assert ws_client._offhours_idle() is False
    _at(monkeypatch, 2026, 10, 5, 15, 45)
    assert ws_client._offhours_idle() is False
    _at(monkeypatch, 2026, 10, 5, 15, 46)
    assert ws_client._offhours_idle() is True


def test_night_and_weekend_idle(monkeypatch):
    monkeypatch.delenv("POSITION_WS_OFFHOURS_IDLE", raising=False)
    _at(monkeypatch, 2026, 10, 5, 22, 0)
    assert ws_client._offhours_idle() is True
    _at(monkeypatch, 2026, 10, 3, 11, 0)           # Saturday
    assert ws_client._offhours_idle() is True


def test_env_off_switch(monkeypatch):
    _at(monkeypatch, 2026, 10, 3, 11, 0)
    monkeypatch.setenv("POSITION_WS_OFFHOURS_IDLE", "0")
    assert ws_client._offhours_idle() is False


def test_loop_idles_without_connecting(monkeypatch):
    monkeypatch.delenv("POSITION_WS_OFFHOURS_IDLE", raising=False)
    monkeypatch.setattr(ws_client, "_offhours_idle", lambda: True)
    monkeypatch.setattr(ws_client, "_OFFHOURS_RECHECK_S", 0.01)
    called = {"session": 0}

    class _S:
        async def ensure_session(self):
            called["session"] += 1
    monkeypatch.setattr(ws_client, "get_session", lambda: _S())

    async def run():
        ws_client._running = True
        task = asyncio.create_task(ws_client._ws_loop())
        await asyncio.sleep(0.1)
        ws_client._running = False
        await asyncio.wait_for(task, 2)
    asyncio.run(run())
    assert called["session"] == 0 and ws_client._connected is False
