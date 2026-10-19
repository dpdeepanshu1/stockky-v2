"""scripts/_progress.py - run a list of jobs (optionally in a process pool), tell the operator it is alive (group 427) and save each finished job (group 466).

The backtests used to print nothing for many minutes, which looks exactly like a hang. This prints one line to stderr every `every` jobs:
done / total, elapsed, estimated time left. Results come back in the SAME ORDER as the jobs, so the reports are unchanged.

group 466: with `ckpt` (a folder) every finished job is saved there as it completes. A run that hit its time limit or was killed can then be started
again with resume=True and computes only the jobs that are missing (or whose input / settings changed - see `stamp`). Without resume the folder is emptied first.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import re
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence


def _line(label: str, done: int, total: int, t0: float) -> str:
    el = time.monotonic() - t0
    eta = el / done * (total - done) if done else 0.0
    return f"[{label}] {done}/{total} symbols  elapsed {el / 60:.1f} min  about {eta / 60:.1f} min left"


def file_stamp(path: Any, *extra: Any) -> tuple:
    """(size, mtime, *extra) of a file, or (None, *extra) when it cannot be read. Used to notice a changed history file."""
    try:
        st = os.stat(str(path))
        return (st.st_size, int(st.st_mtime), *extra)
    except OSError:
        return (None, *extra)


def settings_stamp(args: Any, folder: str) -> str:
    """JSON of every real setting of a run (output paths / worker count / checkpoint flags ignored) plus the benchmark file stamp."""
    skip = {"out", "jobs", "checkpoint", "resume", "dir", "no_save"}
    keep = {k: v for k, v in sorted(vars(args).items()) if k not in skip}
    bench = [file_stamp(os.path.join(folder, n)) for n in ("^NSEI.json", "NSEI.json")]
    return json.dumps([keep, bench], sort_keys=True, default=str)


def _ck_path(ckpt: str, key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:60]
    return os.path.join(ckpt, f"{safe}-{hashlib.md5(key.encode('utf-8', 'replace')).hexdigest()[:8]}.pkl")


def _ck_load(ckpt: str, key: str, stamp: Any) -> Any:
    """The saved result of `key`, or None when missing, unreadable (half written) or saved for a different stamp."""
    try:
        with open(_ck_path(ckpt, key), "rb") as fh:
            doc = pickle.load(fh)
        if isinstance(doc, dict) and doc.get("stamp") == stamp and "res" in doc:
            return doc
    except Exception:  # noqa: BLE001 - corrupt / missing file: just recompute
        pass
    return None


def _ck_save(ckpt: str, key: str, stamp: Any, res: Any) -> None:
    p = _ck_path(ckpt, key)
    try:
        os.makedirs(ckpt, exist_ok=True)
        tmp = p + f".tmp{os.getpid()}"
        with open(tmp, "wb") as fh:
            pickle.dump({"stamp": stamp, "res": res}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)                                  # atomic: a kill never leaves a half file under the real name
    except (OSError, pickle.PickleError) as e:
        print(f"[checkpoint] could not save {key}: {e}", file=sys.stderr, flush=True)


def _default_key(j: Any) -> str:
    return str(j[1]) if isinstance(j, (tuple, list)) and len(j) > 1 else str(j)


def run_jobs(fn: Callable[[Any], Any], jobs: Sequence[Any], workers: int = 1, label: str = "backtest", every: int = 0, chunksize: int = 4,
             ckpt: str = "", resume: bool = False, key: Optional[Callable[[Any], str]] = None, stamp: Optional[Callable[[Any], Any]] = None,
             initializer: Optional[Callable[..., Any]] = None, initargs: tuple = ()) -> List[Any]:
    total = len(jobs)
    every = every if every > 0 else max(1, total // 20)
    key = key or _default_key
    stamp = stamp or (lambda j: None)
    done: Dict[int, Any] = {}
    if ckpt:
        if resume and os.path.isdir(ckpt):
            for i, j in enumerate(jobs):
                doc = _ck_load(ckpt, key(j), stamp(j))
                if doc is not None:
                    done[i] = doc["res"]
        elif os.path.isdir(ckpt):
            shutil.rmtree(ckpt, ignore_errors=True)
    todo = [i for i in range(total) if i not in done]
    t0 = time.monotonic()
    w = max(1, workers)
    print(f"[{label}] starting {total} symbols with {w} worker(s); a progress line follows every {every}", file=sys.stderr, flush=True)
    if resume and ckpt:
        if done:
            print(f"[{label}] resuming: {len(done)} already done, {len(todo)} to go", file=sys.stderr, flush=True)
        else:
            print(f"[{label}] resume asked but nothing saved yet (or the input changed): computing everything", file=sys.stderr, flush=True)
    n = len(done)

    def _got(i: int, res: Any) -> None:
        nonlocal n
        done[i] = res
        if ckpt:
            _ck_save(ckpt, key(jobs[i]), stamp(jobs[i]), res)
        n += 1
        if n % every == 0 or n == total:
            print(_line(label, n, total, t0), file=sys.stderr, flush=True)

    if todo and w > 1 and len(todo) > 1:
        from concurrent.futures import ProcessPoolExecutor
        kw: Dict[str, Any] = {"max_workers": w}
        if initializer is not None:
            kw.update(initializer=initializer, initargs=initargs)
        with ProcessPoolExecutor(**kw) as ex:
            for i, res in zip(todo, ex.map(fn, [jobs[i] for i in todo], chunksize=chunksize)):
                _got(i, res)
    else:
        if initializer is not None and todo:
            initializer(*initargs)
        for i in todo:
            _got(i, fn(jobs[i]))
    return [done[i] for i in range(total)]
