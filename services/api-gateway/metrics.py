"""
Lightweight in-process metrics for free-tier Stockky (no Prometheus dependency).

Counters / gauges are process-local. Expose via GET /metrics (JSON + optional
Prometheus text) and use for internal alerting thresholds.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict, Set

logger = logging.getLogger("metrics")


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._timings: Dict[str, list] = defaultdict(list)  # last N samples ms
        self._max_samples = 50
        self._started = time.time()
        self._type_conflicts_warned: Set[str] = set()  # metric names already warned about in prometheus_text

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe_ms(self, name: str, duration_ms: float, **labels) -> None:
        key = self._key(name, labels)
        with self._lock:
            arr = self._timings[key]
            arr.append(float(duration_ms))
            if len(arr) > self._max_samples:
                del arr[: len(arr) - self._max_samples]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            timings = {}
            for k, arr in self._timings.items():
                if not arr:
                    continue
                s = sorted(arr)
                timings[k] = {
                    "count": len(s),
                    "avg_ms": round(sum(s) / len(s), 1),
                    "p50_ms": s[len(s) // 2],
                    "p95_ms": s[min(len(s) - 1, int(len(s) * 0.95))],
                    "max_ms": s[-1],
                }
            return {
                "uptime_sec": int(time.time() - self._started),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "timings": timings,
            }

    def prometheus_text(self) -> str:
        """Prometheus text exposition (v0.0.4).

        The format allows ONE `# TYPE` line per metric name and requires all samples of a name to sit in
        a single contiguous group after it. Series are therefore collected per metric name first and
        written family by family; a labelled counter with several label sets used to repeat its
        `# TYPE` line once per series, which strict parsers reject (the whole scrape fails).
        """
        snap = self.snapshot()
        # metric name -> [type, [sample lines]]  (insertion-ordered; uptime first, as before)
        families: Dict[str, list] = {
            "stockky_uptime_seconds": ["gauge", [f"stockky_uptime_seconds {snap['uptime_sec']}"]],
        }

        def add(metric: str, mtype: str, line: str) -> None:
            fam = families.get(metric)
            if fam is None:
                families[metric] = [mtype, [line]]
                return
            if fam[0] != mtype and metric not in self._type_conflicts_warned:
                # Same name registered as two kinds. Keep the first type and still emit the sample
                # (valid exposition, no data loss) rather than writing a second, illegal TYPE line.
                self._type_conflicts_warned.add(metric)
                logger.warning("metric %r is both %s and %s; exposing it as %s", metric, fam[0], mtype, fam[0])
            fam[1].append(line)

        for k, v in snap["counters"].items():
            metric, labels = self._split_key(k)
            add(metric, "counter", f"{metric}{labels} {v}")
        for k, v in snap["gauges"].items():
            metric, labels = self._split_key(k)
            add(metric, "gauge", f"{metric}{labels} {v}")
        for k, stats in snap["timings"].items():
            metric, labels = self._split_key(k)
            add(f"{metric}_avg_ms", "gauge", f"{metric}_avg_ms{labels} {stats['avg_ms']}")
            add(f"{metric}_p95_ms", "gauge", f"{metric}_p95_ms{labels} {stats['p95_ms']}")

        lines = ["# HELP stockky_uptime_seconds Process uptime"]
        for name, (mtype, samples) in families.items():
            lines.append(f"# TYPE {name} {mtype}")
            lines.extend(samples)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _escape_label_value(value: Any) -> str:
        """Escape a label value per the Prometheus text format (backslash, double quote, newline).

        Backslash must be escaped FIRST or the escapes added for the other two would be doubled.
        Values reach here from request payloads (e.g. /ops/rate-limits/event's `source`), so an
        unescaped quote or newline would corrupt the whole /metrics?format=prom exposition (scrape
        fails) or inject a forged series line. Escaping here, at key-build time, also keeps distinct
        label sets from colliding on the same registry key.
        """
        try:
            text = str(value)
        except Exception:
            text = "<unprintable>"
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        if not labels:
            return name
        esc = MetricsRegistry._escape_label_value
        parts = ",".join(f'{k}="{esc(v)}"' for k, v in sorted(labels.items()))
        return f"{name}{{{parts}}}"

    @staticmethod
    def _split_key(key: str):
        if "{" not in key:
            return key, ""
        name, rest = key.split("{", 1)
        return name, "{" + rest


class LabelLimiter:
    """Bound how many distinct values a request-supplied label can ever create.

    Every distinct label value is a separate series held in the registry for the life of the process
    (and re-rendered on every scrape). A label fed from a request payload — /ops/rate-limits/event's
    `source` — therefore let any caller grow the registry without limit. Per namespace, the first
    `max_distinct` values seen are admitted as-is; every later NEW value collapses to `overflow`
    ("other"). Admitted values keep their own series, so legitimate sources are unaffected.

    Values are stripped and cut to `max_len`; an empty value becomes "unknown". State is process-local
    (like the registry itself) and resets on restart.
    """

    def __init__(self, max_distinct: int = 32, max_len: int = 64, overflow: str = "other"):
        self._lock = threading.Lock()
        self._seen: Dict[str, Set[str]] = {}
        self._warned: Set[str] = set()
        self.max_distinct = max_distinct
        self.max_len = max_len
        self.overflow = overflow

    def clean(self, namespace: str, value: Any, max_distinct: int = 0) -> str:
        try:
            text = str(value).strip()
        except Exception:
            text = ""
        text = text[: self.max_len] or "unknown"
        if text == self.overflow:
            return text
        limit = max_distinct or self.max_distinct
        with self._lock:
            seen = self._seen.setdefault(namespace, set())
            if text in seen:
                return text
            if len(seen) < limit:
                seen.add(text)
                return text
            first = namespace not in self._warned
            self._warned.add(namespace)
        if first:
            logger.warning(
                "label %r reached %d distinct values; further new values are reported as %r",
                namespace, limit, self.overflow,
            )
        return self.overflow

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._warned.clear()


metrics = MetricsRegistry()
label_limiter = LabelLimiter()
