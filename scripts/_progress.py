"""scripts/_progress.py - run a list of jobs (optionally in a process pool) and tell the operator it is alive (group 427).

The backtests used to print nothing for many minutes, which looks exactly like a hang (the 18 Oct run was Ctrl-C'd three times). This prints one line to
stderr every `every` jobs: done / total, elapsed, estimated time left. Results come back in the SAME ORDER as the jobs, so the reports are unchanged.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, List, Sequence


def _line(label: str, done: int, total: int, t0: float) -> str:
    el = time.monotonic() - t0
    eta = el / done * (total - done) if done else 0.0
    return f"[{label}] {done}/{total} symbols  elapsed {el / 60:.1f} min  about {eta / 60:.1f} min left"


def run_jobs(fn: Callable[[Any], Any], jobs: Sequence[Any], workers: int = 1, label: str = "backtest", every: int = 0, chunksize: int = 4) -> List[Any]:
    total = len(jobs)
    every = every if every > 0 else max(1, total // 20)
    t0 = time.monotonic()
    out: List[Any] = []
    print(f"[{label}] starting {total} symbols with {max(1, workers)} worker(s); a progress line follows every {every}", file=sys.stderr, flush=True)
    if workers > 1 and total > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for n, res in enumerate(ex.map(fn, jobs, chunksize=chunksize), 1):
                out.append(res)
                if n % every == 0 or n == total:
                    print(_line(label, n, total, t0), file=sys.stderr, flush=True)
    else:
        for n, j in enumerate(jobs, 1):
            out.append(fn(j))
            if n % every == 0 or n == total:
                print(_line(label, n, total, t0), file=sys.stderr, flush=True)
    return out
