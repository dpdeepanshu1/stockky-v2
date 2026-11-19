"""Round 638 (E664): a volume-profile build thread that cannot start no longer leaves `building` True for ever.

`_profile` set `_state["building"] = True` and then started the build thread; only the thread clears the flag. If
`Thread.start()` raised (for example "can't start new thread") the flag stayed True, no later call started a build and the
learned volume curve never loaded (the liquidity gate quietly kept the old guessed curve until a restart).

Plain functions; no network, no real thread is started in the failure cases.
"""
import threading

from orders import volume_profile as vp


class _FailThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


class _InlineThread:
    """Records the target; `run_pending()` runs it after `_profile` has let go of its lock (the real thread would wait for it too)."""
    pending = []

    def __init__(self, target=None, **k):
        self._t = target

    def start(self):
        _InlineThread.pending.append(self._t)


def run_pending():
    todo, _InlineThread.pending = _InlineThread.pending, []
    for t in todo:
        t()


def _fresh():
    vp._state.clear()
    vp._state.update({"profile": None, "built_at": 0.0, "building": False, "error": None})


def test_failed_start_frees_the_claim_and_notes_the_error():
    _fresh()
    real = threading.Thread
    threading.Thread = _FailThread
    try:
        assert vp._profile() is None          # never raises
    finally:
        threading.Thread = real
    assert vp._state["building"] is False
    assert "could not start" in (vp._state["error"] or "")
    assert vp.snapshot()["building"] is False


def test_next_call_starts_a_build_and_loads_it(monkeypatch):
    _fresh()
    real = threading.Thread
    threading.Thread = _FailThread
    try:
        vp._profile()
    finally:
        threading.Thread = real
    monkeypatch.setattr(vp, "build", lambda root: {"curve": [0.5] * 375, "days": 5, "symbol_days": 400, "files": 0})
    threading.Thread = _InlineThread
    try:
        vp._profile()
    finally:
        threading.Thread = real
    assert vp._state["building"] is True          # claimed until the build thread has run
    run_pending()
    assert vp._state["building"] is False
    assert vp._state["error"] is None
    assert vp._state["profile"]["days"] == 5


def test_warning_is_logged_once_per_window():
    import logging
    _fresh()
    real = threading.Thread
    threading.Thread = _FailThread
    lg = logging.getLogger("position-stocks-volume-profile")
    msgs = []

    class _H(logging.Handler):
        def emit(self, record):
            msgs.append(record.getMessage())
    h = _H(level=logging.WARNING)
    # Round 645: own handler instead of caplog (caplog.at_level does not lift logging.disable, and an earlier test in a
    # full run can leave logging disabled); set the state here and restore it afterwards.
    saved = (lg.level, lg.disabled, logging.root.manager.disable)
    lg.setLevel(logging.WARNING)
    lg.disabled = False
    logging.disable(logging.NOTSET)
    lg.addHandler(h)
    try:
        for _ in range(5):
            vp._profile()
    finally:
        threading.Thread = real
        lg.removeHandler(h)
        lg.setLevel(saved[0])
        lg.disabled = saved[1]
        logging.disable(saved[2])
    msgs = [m for m in msgs if "could not start the build thread" in m]
    assert len(msgs) == 1


def test_normal_start_still_claims_builds_and_releases(monkeypatch):
    _fresh()
    monkeypatch.setattr(vp, "build", lambda root: {"curve": [0.1] * 375, "days": 6, "symbol_days": 500, "files": 0})
    real = threading.Thread
    seen = []

    class _Rec(_InlineThread):
        def start(self):
            seen.append(vp._state["building"])      # the claim is held while the thread starts
            super().start()
    threading.Thread = _Rec
    try:
        vp._profile()
        run_pending()
        vp._profile()                                 # fresh now: no second build
        run_pending()
    finally:
        threading.Thread = real
    assert seen == [True]
    assert vp._state["building"] is False
    assert vp._state["profile"]["days"] == 6


if __name__ == "__main__":
    import sys
    import types
    sys.path.insert(0, __file__.rsplit("/tests/", 1)[0])

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)

    class _CL:
        records = []

        def at_level(self, *a, **k):
            import contextlib, logging
            h = logging.Handler()
            h.emit = lambda r: _CL.records.append(r)
            lg = logging.getLogger("position-stocks-volume-profile")

            @contextlib.contextmanager
            def cm():
                lg.addHandler(h)
                try:
                    yield
                finally:
                    lg.removeHandler(h)
            return cm()
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            _CL.records = []
            args = f.__code__.co_varnames[:f.__code__.co_argcount]
            f(*[_MP() if a == "monkeypatch" else _CL() if a == "caplog" else None for a in args])
            print("ok", n)
