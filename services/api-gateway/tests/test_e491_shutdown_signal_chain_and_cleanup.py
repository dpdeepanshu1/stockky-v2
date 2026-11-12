"""E491: three start-up / shut-down defects in gateway `main.py`.

1. `_install_signal_handlers` REPLACED the handler installed before it. When uvicorn imports the app while it is already capturing signals
   (the order of uvicorn 0.30.x, the version in requirements.txt) the gateway handler overwrote uvicorn's `Server.handle_exit`: a SIGTERM
   committed the checkpoint but never told uvicorn to stop, so the container hung until SIGKILL. The handler now calls the one it replaced
   after the commit. SIG_DFL, SIG_IGN and Python's default SIGINT handler are still not chained (as before).
2. `_cleanup_scan_resources` parsed `WATCHLIST_SCAN_GRACE_SECONDS` outside any try: a typo raised out of the cleanup thread before the pool
   and the HTTP client were released. A bad value now means the 30 s default.
3. `_start_structure_refresh_locked` marked the job `running` and then started the worker thread; if the thread could not start, the job
   stayed `running` for good and every later start (and the catch-up loop) backed off. It is now set to `error` before the error is raised.

No fastapi / httpx needed (functions lifted from main.py by AST). Runs with pytest or `python3 tests/test_e491_...py`."""
from __future__ import annotations

import ast
import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_SERVICE, "main.py")
_TREE = ast.parse(open(_MAIN, encoding="utf-8").read())


def _lift(name, ns):
    fn = [n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(fn) == 1, name
    exec(compile(ast.Module(body=fn, type_ignores=[]), "main.py", "exec"), ns)
    return ns[name]


# ── 1. signal handler chaining ───────────────────────────────────────────────

def _install_with(previous_by_sig, seq, commit_raises=None, getsignal_raises=False):
    """Lift _install_signal_handlers and run it against a fake signal table that starts with `previous_by_sig`.
    The fake commit appends ("commit", reason) to `seq`. Returns the table of handlers installed."""

    def commit(reason="shutdown"):
        seq.append(("commit", reason))
        if commit_raises:
            raise commit_raises
        return []

    ns = {"logger": logging.getLogger("e491"), "_graceful_shutdown_commit": commit}
    install = _lift("_install_signal_handlers", ns)
    table = dict(previous_by_sig)

    def fake_get(num):
        if getsignal_raises:
            raise OSError("no")
        return table.get(num)

    real_signal, real_get = signal.signal, signal.getsignal
    signal.signal = lambda num, h: table.__setitem__(num, h)
    signal.getsignal = fake_get
    try:
        install()
    finally:
        signal.signal, signal.getsignal = real_signal, real_get
    return table


def test_the_previous_handler_is_called_after_the_commit():
    seq = []
    prev = lambda num, frame: seq.append(("prev", num))   # noqa: E731  (stands in for uvicorn's Server.handle_exit)
    table = _install_with({signal.SIGTERM: prev, signal.SIGINT: prev}, seq)
    table[signal.SIGTERM](signal.SIGTERM, None)
    assert seq == [("commit", "signal_SIGTERM"), ("prev", signal.SIGTERM)]
    table[signal.SIGINT](signal.SIGINT, None)
    assert seq[2:] == [("commit", "signal_SIGINT"), ("prev", signal.SIGINT)]


def test_a_failing_commit_still_chains_to_the_previous_handler():
    seq = []
    prev = lambda num, frame: seq.append(("prev", num))   # noqa: E731
    table = _install_with({signal.SIGTERM: prev, signal.SIGINT: prev}, seq, commit_raises=RuntimeError("commit failed"))
    table[signal.SIGTERM](signal.SIGTERM, None)
    assert seq == [("commit", "signal_SIGTERM"), ("prev", signal.SIGTERM)]


def test_default_ignore_and_the_python_sigint_handler_are_not_chained():
    for prev_term, prev_int in ((signal.SIG_DFL, signal.default_int_handler), (signal.SIG_IGN, signal.SIG_DFL)):
        seq = []
        table = _install_with({signal.SIGTERM: prev_term, signal.SIGINT: prev_int}, seq)
        table[signal.SIGTERM](signal.SIGTERM, None)
        table[signal.SIGINT](signal.SIGINT, None)      # would raise KeyboardInterrupt if default_int_handler were chained
        assert [c[1] for c in seq] == ["signal_SIGTERM", "signal_SIGINT"]


def test_a_failing_getsignal_still_installs_the_handler():
    seq = []
    table = _install_with({}, seq, getsignal_raises=True)
    assert set(table) == {signal.SIGTERM, signal.SIGINT}
    table[signal.SIGTERM](signal.SIGTERM, None)       # nothing to chain, nothing raised
    assert seq == [("commit", "signal_SIGTERM")]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_uvicorn_still_shuts_down_when_the_app_is_imported_inside_its_signal_capture(tmp_path):
    """The real failure: the gateway handler overwrote uvicorn's, so SIGTERM committed and the process then hung until SIGKILL."""
    try:
        import uvicorn  # noqa: F401
    except Exception:
        import pytest
        pytest.skip("uvicorn not installed")
    app_py = tmp_path / "e491app.py"
    app_py.write_text(textwrap.dedent(f'''
        import ast, logging, os
        tree = ast.parse(open({_MAIN!r}, encoding="utf-8").read())
        fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_install_signal_handlers"]
        def _graceful_shutdown_commit(reason="shutdown"):
            print("COMMIT", reason, flush=True)
            return []
        ns = dict(logger=logging.getLogger("gw"), _graceful_shutdown_commit=_graceful_shutdown_commit)
        exec(compile(ast.Module(body=fn, type_ignores=[]), "main.py", "exec"), ns)
        ns["_install_signal_handlers"]()
        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    m = await receive()
                    if m["type"] == "lifespan.startup":
                        await send({{"type": "lifespan.startup.complete"}})
                    elif m["type"] == "lifespan.shutdown":
                        print("LIFESPAN SHUTDOWN RAN", flush=True)
                        await send({{"type": "lifespan.shutdown.complete"}})
                        return
    '''))
    # Server driven directly: the app string is imported inside Server.serve() -> capture_signals() (uvicorn 0.30.x order).
    runner = tmp_path / "e491run.py"
    runner.write_text(textwrap.dedent(f'''
        import sys, uvicorn
        sys.path.insert(0, {str(tmp_path)!r})
        try:
            uvicorn.Server(uvicorn.Config("e491app:app", port={_free_port()}, log_level="info")).run()
        except KeyboardInterrupt:
            pass
    '''))
    p = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, ready = [], threading.Event()

    def _read():
        for line in p.stdout:
            lines.append(line)
            if "Application startup complete" in line:
                ready.set()

    threading.Thread(target=_read, daemon=True).start()
    try:
        assert ready.wait(20), "server never started:\n" + "".join(lines)
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(8)
        except subprocess.TimeoutExpired:
            raise AssertionError("process still running 8 s after SIGTERM (needs SIGKILL):\n" + "".join(lines))
        time.sleep(0.2)
        out = "".join(lines)
        assert "COMMIT signal_SIGTERM" in out and "LIFESPAN SHUTDOWN RAN" in out
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# ── 2. scan cleanup grace period ─────────────────────────────────────────────

class _Pool:
    def __init__(self):
        self.calls = []

    def shutdown(self, wait=True, cancel_futures=False):
        self.calls.append((wait, cancel_futures))


class _Closer:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _cleanup(env_value):
    waits = []
    ns = {"os": os, "wait": lambda fs, timeout=None: waits.append((fs, timeout))}
    fn = _lift("_cleanup_scan_resources", ns)
    old = os.environ.get("WATCHLIST_SCAN_GRACE_SECONDS")
    if env_value is None:
        os.environ.pop("WATCHLIST_SCAN_GRACE_SECONDS", None)
    else:
        os.environ["WATCHLIST_SCAN_GRACE_SECONDS"] = env_value
    pool, client = _Pool(), _Closer()
    try:
        fn(pool, client, {"f1"}, 10)
    finally:
        if old is None:
            os.environ.pop("WATCHLIST_SCAN_GRACE_SECONDS", None)
        else:
            os.environ["WATCHLIST_SCAN_GRACE_SECONDS"] = old
    return waits, pool, client


def test_a_bad_grace_value_falls_back_to_30_and_still_releases_everything():
    for bad in ("abc", "30s", "1,5"):
        waits, pool, client = _cleanup(bad)
        assert waits == [({"f1"}, 30.0)], bad
        assert pool.calls == [(False, True)] and client.closed == 1, bad


def test_good_grace_values_are_unchanged():
    assert _cleanup(None)[0] == [({"f1"}, 30.0)]
    assert _cleanup("2.5")[0] == [({"f1"}, 2.5)]


# ── 3. structure refresh: the worker thread cannot start ─────────────────────

class _FakeSR:
    def __init__(self):
        self._STOP = threading.Event()
        self.jobs = []

    def enabled(self):
        return True

    def get_job(self):
        return {"status": "idle"}

    def _set_job(self, **kw):
        self.jobs.append(kw)

    def plan(self, *a, **k):
        return [], {}, "2026-11-10"


_LAST_SR = None


def _start(thread_cls):
    global _LAST_SR
    sr = _LAST_SR = _FakeSR()
    ns = {"threading": types.SimpleNamespace(Thread=thread_cls), "_feed_store": lambda: object(), "httpx": types.SimpleNamespace(),
          "logger": logging.getLogger("e491"), "datetime": __import__("datetime").datetime, "IST": None,
          "Optional": __import__("typing").Optional, "Dict": dict, "Any": object}
    fn = _lift("_start_structure_refresh_locked", ns)
    sys.modules["structure_refresh"] = sr
    try:
        return sr, fn(False, False, None)
    finally:
        sys.modules.pop("structure_refresh", None)


def test_a_worker_thread_that_cannot_start_does_not_leave_the_job_running():
    class _NoThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    raised = None
    try:
        _start(_NoThread)
    except RuntimeError as e:
        raised = e
    assert raised is not None                      # _start_structure_refresh turns this into {"ok": False, "status": "error"}
    assert _LAST_SR.jobs[0]["status"] == "running" and _LAST_SR.jobs[-1]["status"] == "error" and "thread" in _LAST_SR.jobs[-1]["message"]


def test_a_thread_that_starts_still_answers_running():
    started = []

    class _T:
        def __init__(self, target=None, **kw):
            self.target = target

        def start(self):
            started.append(self.target)

    sr, out = _start(_T)
    assert out["status"] == "running" and len(started) == 1
    assert [j["status"] for j in sr.jobs] == ["running"]


if __name__ == "__main__":   # pragma: no cover
    import tempfile
    import pathlib
    failed = 0
    for _name, _f in sorted(globals().items()):
        if _name.startswith("test_") and callable(_f):
            try:
                _f(pathlib.Path(tempfile.mkdtemp())) if _f.__code__.co_argcount else _f()
                print("ok  ", _name)
            except BaseException as _e:   # noqa: BLE001
                failed += 1
                print("FAIL", _name, type(_e).__name__, _e)
    sys.exit(1 if failed else 0)
