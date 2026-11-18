"""Round 633 (E659): a failed model preload must release the forecast lock.

Before, the preload thread ran `_lock.acquire(); _get_forecaster(); _lock.release()`. When the load raised (no weights, torch missing,
out of memory) the release never ran and every /forecast waited on the lock for ever. main.py needs fastapi to import, so the real
`_preload` source is cut out of main.py and run against stand-ins. Plain functions; run_plain.py or pytest."""
import ast
import os
import threading
import types

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _load(get_forecaster):
    fn = next(n for n in ast.parse(open(MAIN, encoding="utf-8").read()).body if isinstance(n, ast.FunctionDef) and n.name == "_preload")
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    lock, logged = threading.Lock(), []
    g = {"_lock": lock, "_get_forecaster": get_forecaster, "logger": types.SimpleNamespace(warning=lambda *a, **k: logged.append(a))}
    exec(compile(mod, MAIN, "exec"), g)
    return g["_preload"], lock, logged


def test_a_failed_load_releases_the_lock_and_is_logged():
    def boom():
        raise RuntimeError("no weights")
    preload, lock, logged = _load(boom)
    preload()
    assert lock.acquire(timeout=1), "the forecast lock is still held after a failed preload"
    lock.release()
    assert len(logged) == 1


def test_a_good_load_releases_the_lock():
    calls = []
    preload, lock, logged = _load(lambda: calls.append(1))
    preload()
    assert calls == [1] and not logged
    assert lock.acquire(timeout=1)
    lock.release()


def test_the_load_runs_while_holding_the_lock():
    seen = []
    holder = {}

    def load():
        seen.append(holder["lock"].locked())
    preload, lock, _ = _load(load)
    holder["lock"] = lock
    preload()
    assert seen == [True]
