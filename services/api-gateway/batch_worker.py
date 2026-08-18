"""
Reusable async batch processor for free-tier Stockky scans.

Design goals (512MB Render dynos, separate service accounts):
  - Never shrink the work list (full universe / full feed list)
  - Only limit *in-flight* concurrency via batch size + semaphore inside workers
  - Avoid creating thousands of Tasks at once (OOM)
  - Support cancel checks, progress callbacks, warm hooks, and GC between batches
  - Optional per-item result cache (Redis-backed via callbacks) so re-scans
    skip upstream for symbols still within TTL
"""
from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Generic, List, Optional, Sequence, TypeVar

logger = logging.getLogger("batch-worker")

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class BatchProgress:
    total: int
    processed: int
    batch_index: int
    batch_count: int
    elapsed_sec: float
    cancelled: bool = False
    cache_hits: int = 0
    cache_misses: int = 0


@dataclass
class BatchResult(Generic[R]):
    results: List[R] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    processed: int = 0
    cancelled: bool = False
    cache_hits: int = 0
    cache_misses: int = 0


async def _cancel_tasks(tasks: Sequence[asyncio.Task], timeout: float = 2.0) -> None:
    for t in tasks:
        if not t.done():
            t.cancel()
    if tasks:
        try:
            await asyncio.wait(list(tasks), timeout=timeout)
        except Exception:
            pass


async def run_in_batches(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    batch_size: int = 12,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[BatchProgress], Awaitable[None]]] = None,
    on_batch_end: Optional[Callable[[BatchProgress], Awaitable[None]]] = None,
    classify_result: Optional[Callable[[R], Optional[Dict[str, Any]]]] = None,
    collect_errors_from_exceptions: bool = True,
    gc_each_batch: bool = True,
    start_time: Optional[float] = None,
    # ── Result cache (optional) ──
    cache_get: Optional[Callable[[T], Optional[R]]] = None,
    cache_set: Optional[Callable[[T, R], None]] = None,
    cache_key_fn: Optional[Callable[[T], str]] = None,
) -> BatchResult[R]:
    """
    Process *all* items in ordered chunks of batch_size.

    cache_get(item) -> cached result or None
    cache_set(item, result) -> persist successful result
    Cached items still count toward processed/total (universe unchanged);
    only upstream work is skipped.
    """
    import time

    total = len(items)
    batch_size = max(1, int(batch_size))
    batch_count = (total + batch_size - 1) // batch_size if total else 0
    t0 = start_time if start_time is not None else time.time()

    out = BatchResult()
    if total == 0:
        return out

    logger.info(
        "batch_worker start items=%s batch_size=%s batches=%s cache=%s",
        total, batch_size, batch_count, bool(cache_get),
    )

    def _accept(item: T, result: R) -> None:
        err = classify_result(result) if classify_result else None
        if err is not None:
            out.errors.append(err)
        else:
            out.results.append(result)
            if cache_set is not None:
                try:
                    cache_set(item, result)
                except Exception as e:
                    logger.debug("cache_set failed: %s", e)

    for batch_index, offset in enumerate(range(0, total, batch_size)):
        if should_cancel and should_cancel():
            out.cancelled = True
            break

        chunk = list(items[offset : offset + batch_size])
        cached_pairs: List[tuple] = []
        to_fetch: List[T] = []

        # ── Split batch: cache hit vs miss ──
        for item in chunk:
            hit = None
            if cache_get is not None:
                try:
                    hit = cache_get(item)
                except Exception as e:
                    logger.debug("cache_get failed: %s", e)
                    hit = None
            if hit is not None:
                cached_pairs.append((item, hit))
                out.cache_hits += 1
            else:
                to_fetch.append(item)
                out.cache_misses += 1

        # Apply cached results first (preserve progress feedback)
        for item, result in cached_pairs:
            out.processed += 1
            _accept(item, result)

        tasks: List[asyncio.Task] = []
        if to_fetch:
            tasks = [asyncio.create_task(worker(item)) for item in to_fetch]
            try:
                raw = await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error("batch gather failed: %s", e)
                raw = []
                await _cancel_tasks(tasks)

            for item, result in zip(to_fetch, raw):
                out.processed += 1
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    if collect_errors_from_exceptions:
                        out.errors.append({
                            "item": str(item),
                            "error": f"{type(result).__name__}: {str(result)[:160]}",
                        })
                    logger.error("batch item failed %s: %s", item, result)
                    continue
                _accept(item, result)

        elapsed = time.time() - t0
        progress = BatchProgress(
            total=total,
            processed=out.processed,
            batch_index=batch_index,
            batch_count=batch_count,
            elapsed_sec=round(elapsed, 1),
            cancelled=False,
            cache_hits=out.cache_hits,
            cache_misses=out.cache_misses,
        )

        if should_cancel and should_cancel():
            out.cancelled = True
            progress.cancelled = True
            await _cancel_tasks(tasks)

        if on_progress:
            try:
                await on_progress(progress)
            except Exception as e:
                logger.debug("on_progress: %s", e)

        if on_batch_end:
            try:
                await on_batch_end(progress)
            except Exception as e:
                logger.debug("on_batch_end: %s", e)

        if gc_each_batch:
            gc.collect()

        if out.cancelled:
            logger.info(
                "batch_worker cancelled after %s/%s hits=%s misses=%s",
                out.processed, total, out.cache_hits, out.cache_misses,
            )
            break

    if gc_each_batch:
        gc.collect()
    logger.info(
        "batch_worker done processed=%s results=%s errors=%s hits=%s misses=%s cancelled=%s",
        out.processed, len(out.results), len(out.errors),
        out.cache_hits, out.cache_misses, out.cancelled,
    )
    return out


def default_batch_size(workers: int, minimum: int = 6) -> int:
    """In-flight batch sizing for scans: ~2x worker pool, never reduces item list."""
    return max(int(workers) * 2, int(minimum))
