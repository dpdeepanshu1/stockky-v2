"""
Lightweight in-process metrics for free-tier Stockky (no Prometheus dependency).

Counters / gauges are process-local. Expose via GET /metrics (JSON + optional
Prometheus text) and use for internal alerting thresholds.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._timings: Dict[str, list] = defaultdict(list)  # last N samples ms
        self._max_samples = 50
        self._started = time.time()

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
        snap = self.snapshot()
        lines = [
            "# HELP stockky_uptime_seconds Process uptime",
            "# TYPE stockky_uptime_seconds gauge",
            f"stockky_uptime_seconds {snap['uptime_sec']}",
        ]
        for k, v in snap["counters"].items():
            metric, labels = self._split_key(k)
            lines.append(f"# TYPE {metric} counter")
            lines.append(f"{metric}{labels} {v}")
        for k, v in snap["gauges"].items():
            metric, labels = self._split_key(k)
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric}{labels} {v}")
        for k, stats in snap["timings"].items():
            metric, labels = self._split_key(k)
            lines.append(f"{metric}_avg_ms{labels} {stats['avg_ms']}")
            lines.append(f"{metric}_p95_ms{labels} {stats['p95_ms']}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: dict) -> str:
        if not labels:
            return name
        parts = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{parts}}}"

    @staticmethod
    def _split_key(key: str):
        if "{" not in key:
            return key, ""
        name, rest = key.split("{", 1)
        return name, "{" + rest


metrics = MetricsRegistry()
