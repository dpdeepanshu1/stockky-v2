"""metrics.MetricsRegistry — process-local counters/gauges/timings + Prometheus text."""
import threading

import pytest

import metrics as metrics_mod
from metrics import MetricsRegistry


@pytest.fixture()
def reg():
    return MetricsRegistry()


class TestKeys:
    def test_key_without_labels(self):
        assert MetricsRegistry._key("hits", {}) == "hits"

    def test_key_labels_sorted_and_quoted(self):
        assert MetricsRegistry._key("hits", {"b": 2, "a": "x"}) == 'hits{a="x",b="2"}'

    def test_split_key_plain(self):
        assert MetricsRegistry._split_key("hits") == ("hits", "")

    def test_split_key_with_labels(self):
        assert MetricsRegistry._split_key('hits{a="x"}') == ("hits", '{a="x"}')


class TestCounters:
    def test_inc_default_and_value(self, reg):
        reg.inc("c")
        reg.inc("c")
        reg.inc("c", 2.5)
        assert reg.snapshot()["counters"]["c"] == 4.5

    def test_labels_make_separate_series(self, reg):
        reg.inc("c", dependency="a")
        reg.inc("c", dependency="b")
        reg.inc("c", dependency="a")
        c = reg.snapshot()["counters"]
        assert c['c{dependency="a"}'] == 2.0 and c['c{dependency="b"}'] == 1.0

    def test_thread_safety(self, reg):
        def work():
            for _ in range(500):
                reg.inc("c")
        ts = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert reg.snapshot()["counters"]["c"] == 4000.0


class TestGauges:
    def test_set_and_overwrite(self, reg):
        reg.set_gauge("g", 1)
        reg.set_gauge("g", 7.5)
        assert reg.snapshot()["gauges"]["g"] == 7.5

    def test_labelled_gauge(self, reg):
        reg.set_gauge("g", 3, svc="x")
        assert reg.snapshot()["gauges"]['g{svc="x"}'] == 3


class TestTimings:
    def test_stats_for_known_samples(self, reg):
        for v in (10, 20, 30, 40, 50):
            reg.observe_ms("t", v)
        t = reg.snapshot()["timings"]["t"]
        assert t == {"count": 5, "avg_ms": 30.0, "p50_ms": 30.0, "p95_ms": 50.0, "max_ms": 50.0}

    def test_single_sample(self, reg):
        reg.observe_ms("t", 12.34)
        t = reg.snapshot()["timings"]["t"]
        assert t["count"] == 1 and t["p50_ms"] == t["p95_ms"] == t["max_ms"] == 12.34

    def test_samples_capped_to_most_recent_50(self, reg):
        for v in range(120):
            reg.observe_ms("t", v)
        t = reg.snapshot()["timings"]["t"]
        assert t["count"] == 50 and t["max_ms"] == 119.0
        assert reg._timings["t"][0] == 70.0  # oldest kept sample

    def test_value_coerced_to_float(self, reg):
        reg.observe_ms("t", "5")
        assert reg._timings["t"] == [5.0]

    def test_empty_timing_series_skipped_in_snapshot(self, reg):
        reg._timings["ghost"]  # defaultdict creates an empty list
        assert "ghost" not in reg.snapshot()["timings"]


class TestSnapshot:
    def test_shape_and_uptime(self, reg):
        snap = reg.snapshot()
        assert set(snap) == {"uptime_sec", "counters", "gauges", "timings"}
        assert isinstance(snap["uptime_sec"], int) and snap["uptime_sec"] >= 0

    def test_snapshot_is_a_copy(self, reg):
        reg.inc("c")
        snap = reg.snapshot()
        snap["counters"]["c"] = 999
        assert reg.snapshot()["counters"]["c"] == 1.0

    def test_uptime_uses_started_time(self, reg, monkeypatch):
        monkeypatch.setattr(metrics_mod.time, "time", lambda: reg._started + 125.9)
        assert reg.snapshot()["uptime_sec"] == 125


class TestPrometheusText:
    def test_empty_registry(self, reg):
        text = reg.prometheus_text()
        assert text.startswith("# HELP stockky_uptime_seconds Process uptime\n# TYPE stockky_uptime_seconds gauge\n")
        assert text.endswith("\n")

    def test_all_series_types_rendered(self, reg):
        reg.inc("stockky_req_total", 3, route="scan")
        reg.set_gauge("stockky_queue", 2)
        reg.observe_ms("stockky_lat", 10, dep="db")
        reg.observe_ms("stockky_lat", 30, dep="db")
        lines = reg.prometheus_text().splitlines()
        assert "# TYPE stockky_req_total counter" in lines
        assert 'stockky_req_total{route="scan"} 3.0' in lines
        assert "# TYPE stockky_queue gauge" in lines
        assert "stockky_queue 2" in lines
        assert 'stockky_lat_avg_ms{dep="db"} 20.0' in lines
        assert 'stockky_lat_p95_ms{dep="db"} 30.0' in lines


def test_module_singleton_is_a_registry():
    assert isinstance(metrics_mod.metrics, MetricsRegistry)
