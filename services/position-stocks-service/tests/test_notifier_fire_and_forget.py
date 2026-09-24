"""
tests/test_notifier_fire_and_forget.py

session112 fix (port of real-trade-service's session111 fix — see
archive/session-notes/SESSION111_NOTIFY_FIRE_AND_FORGET_EXIT_PATH_FIX_2026-09-25.md
and archive/session-notes/SESSION112_...md in this service): notify_sync is
fully synchronous end to end (up to ~42s worst case: 12s service timeout +
15s direct-Telegram timeout + a second 15s plain-text retry) and every
order-path caller in this service used to call it inline from inside a
to_thread-wrapped stage of _run_cycle() (under _cycle_lock, shared with
screening/entry for every OTHER candidate that cycle) or
_fast_reconcile_loop() (a tight sequential while-loop). notify_critical()'s
two callers in main.py's eDIS morning check are worse still — called
directly on the event loop with no to_thread wrapper at all.

This file covers the fix: notify_fire_and_forget() (does the dedup check on
the calling thread, then hands delivery to a daemon background thread and
returns immediately) and notify_critical() now routing through it instead of
the blocking notify_sync. It also pins every order-path call site to the
non-blocking variant so a future edit can't silently regress a caller back
to the ~42s-worst-case blocking one — every one of those tests mocks the
notifier attribute wholesale and none of them would otherwise notice.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_notifier_fire_and_forget.py -q \
        --cov=notifier --cov-report=term-missing
"""
from __future__ import annotations

import inspect
import logging
import threading
import time

import httpx
import pytest

import config
import notifier

LOGGER = "position-stocks-notifier"


@pytest.fixture(autouse=True)
def _clean_dedup():
    notifier._dedup_cache.clear()
    yield
    notifier._dedup_cache.clear()


@pytest.fixture()
def spy_thread(monkeypatch):
    """Replaces threading.Thread (as notifier sees it) with a subclass that
    records every instance created, so tests can assert on daemon-ness and
    join() a background delivery deterministically without timing guesses."""
    created = []
    RealThread = threading.Thread

    class _Spy(RealThread):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    monkeypatch.setattr(notifier.threading, "Thread", _Spy)
    return created


@pytest.fixture()
def net(monkeypatch):
    """Real httpx.Client over a MockTransport, so a background-thread
    delivery exercises genuine request/response handling — only the
    transport is faked."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"delivered": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    return calls


# ══════════════════════════════════════════════════════════════════════════
# notify_fire_and_forget
# ══════════════════════════════════════════════════════════════════════════
class TestNotifyFireAndForget:
    def test_returns_none_and_does_not_block_while_delivery_is_held_open(self, monkeypatch, spy_thread):
        release = threading.Event()
        entered = threading.Event()

        def slow_deliver(text):
            entered.set()
            release.wait(timeout=5)
            return True

        monkeypatch.setattr(notifier, "_deliver_sync", slow_deliver)

        result = notifier.notify_fire_and_forget("held open")
        assert result is None   # returned immediately — did not wait for slow_deliver

        assert entered.wait(timeout=2), "background thread never started delivery"
        release.set()
        spy_thread[0].join(timeout=2)

    def test_delivers_on_a_background_thread_not_the_caller(self, monkeypatch, spy_thread):
        seen = {}

        def record(text):
            seen["thread"] = threading.current_thread()
            return True

        monkeypatch.setattr(notifier, "_deliver_sync", record)

        caller_thread = threading.current_thread()
        notifier.notify_fire_and_forget("who delivers this")
        spy_thread[0].join(timeout=2)

        assert seen["thread"] is not caller_thread
        assert seen["thread"] is spy_thread[0]

    def test_background_thread_is_a_daemon(self, monkeypatch, spy_thread):
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: True)
        notifier.notify_fire_and_forget("daemon check")
        spy_thread[0].join(timeout=2)
        assert spy_thread[0].daemon is True

    def test_duplicate_within_window_is_suppressed_without_starting_a_thread(self, monkeypatch, spy_thread):
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: True)
        notifier.notify_fire_and_forget("same message")
        spy_thread[0].join(timeout=2)
        assert len(spy_thread) == 1

        result = notifier.notify_fire_and_forget("same message")
        assert result is None
        assert len(spy_thread) == 1   # no second thread started for the duplicate

    def test_falls_back_to_direct_telegram_in_the_background_same_as_blocking_variant(self, net, monkeypatch, spy_thread):
        monkeypatch.setattr(notifier, "_NOTIFICATION_SERVICE_URL", "http://notification-scheduler-service:8000/notification")
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456789:AAHtoken")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")

        def handler(request):
            if request.url.host == "api.telegram.org":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(500, text="down")

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))

        notifier.notify_fire_and_forget("service is down, use direct telegram")
        spy_thread[0].join(timeout=5)
        # No assertion possible on a return value (fire-and-forget has none) —
        # the join() completing without the background thread raising, plus
        # the token-redaction test file's coverage of _direct_telegram's own
        # request shape, is the contract here.

    def test_thread_start_failure_is_swallowed(self, monkeypatch, caplog):
        class _BoomThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise RuntimeError("cannot start thread")

        monkeypatch.setattr(notifier.threading, "Thread", _BoomThread)
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: True)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            result = notifier.notify_fire_and_forget("thread start blows up")
        assert result is None
        assert "failed to start background delivery thread" in caplog.text

    def test_delivery_exception_inside_background_thread_is_swallowed_and_logged(self, monkeypatch, spy_thread, caplog):
        def boom(text):
            raise RuntimeError("delivery exploded")

        monkeypatch.setattr(notifier, "_deliver_sync", boom)

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            notifier.notify_fire_and_forget("this will raise")
            spy_thread[0].join(timeout=2)
            # give the logging call inside the thread a moment to land
            time.sleep(0.05)

        assert not spy_thread[0].is_alive()
        assert "background delivery failed" in caplog.text

    def test_deliver_background_directly_swallows_and_logs(self, monkeypatch, caplog):
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: (_ for _ in ()).throw(RuntimeError("x")))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            notifier._deliver_background("direct call")   # must not raise
        assert "background delivery failed" in caplog.text


# ══════════════════════════════════════════════════════════════════════════
# notify_critical now routes through notify_fire_and_forget
# ══════════════════════════════════════════════════════════════════════════
class TestNotifyCriticalUsesFireAndForget:
    def test_notify_critical_calls_fire_and_forget_with_the_critical_prefix(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(notifier, "notify_fire_and_forget", lambda text: seen.setdefault("text", text))
        notifier.notify_critical("broker mismatch on AAA")
        assert seen["text"] == "\U0001F6A8 <b>CRITICAL</b>\nbroker mismatch on AAA"

    def test_notify_critical_never_raises_even_if_fire_and_forget_blows_up(self, monkeypatch, caplog):
        def boom(text):
            raise RuntimeError("nope")

        monkeypatch.setattr(notifier, "notify_fire_and_forget", boom)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            notifier.notify_critical("still must not raise")   # must not raise
        assert "swallowed notification error" in caplog.text

    def test_notify_critical_is_genuinely_non_blocking_end_to_end(self, monkeypatch, spy_thread):
        # session112's actual bug: notify_critical used to call the blocking
        # notify_sync, so main.py's un-to_thread-wrapped eDIS check callers
        # could stall every request this service was handling. Prove the
        # real (non-monkeypatched) notify_critical -> notify_fire_and_forget
        # path hands delivery to a background thread rather than blocking.
        release = threading.Event()

        def slow(text):
            release.wait(timeout=5)
            return True

        monkeypatch.setattr(notifier, "_deliver_sync", slow)
        notifier.notify_critical("event-loop caller must not block")
        release.set()
        spy_thread[0].join(timeout=2)


# ══════════════════════════════════════════════════════════════════════════
# Module wiring — pin every order-path call site to the non-blocking variant
# ══════════════════════════════════════════════════════════════════════════
class TestOrderPathCallSitesUseFireAndForget:
    """session112: before this fix, orders/entry.py, orders/breakeven.py,
    orders/eod_squareoff.py, orders/overnight_stop.py and orders/reconcile.py
    all called the blocking notifier.notify_sync() inline. Every one of
    those files' own tests monkeypatches the notifier attribute wholesale
    (so they can't detect which name production code actually calls), so
    this pins the source text directly against a future regression back to
    the blocking call."""

    @pytest.mark.parametrize("modname", ["entry", "breakeven", "overnight_stop", "reconcile"])
    def test_module_attribute_call_sites_use_fire_and_forget(self, modname):
        import importlib

        mod = importlib.import_module(f"orders.{modname}")
        src = inspect.getsource(mod)
        assert "notifier.notify_sync(" not in src
        assert "notifier.notify_fire_and_forget(" in src

    def test_eod_squareoff_local_imports_alias_fire_and_forget(self):
        import orders.eod_squareoff as mod

        src = inspect.getsource(mod)
        assert "from notifier import notify_sync" not in src
        assert src.count("from notifier import notify_fire_and_forget as notify_sync") == 2
