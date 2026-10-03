"""metrics.MetricsRegistry — process-local counters/gauges/timings + Prometheus text."""
import logging
import threading

import pytest

import metrics as metrics_mod
from metrics import LabelLimiter, MetricsRegistry


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


import re

# Strict-enough Prometheus text-format line check: name{label="value",...} number, where a label value
# may contain only non-quote/non-backslash/non-newline characters or the escapes \\, \" and \n.
_LV = r'"(?:[^"\\\n]|\\[\\"n])*"'
_SAMPLE = re.compile(
    r'^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{[a-zA-Z_][a-zA-Z0-9_]*=' + _LV + r'(?:,[a-zA-Z_][a-zA-Z0-9_]*=' + _LV + r')*\})? \S+$'
)


def _unescape(v):
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\":
            out.append({"\\": "\\", '"': '"', "n": "\n"}[v[i + 1]])
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def _assert_valid_exposition(text):
    """Structural rules of the Prometheus text format that strict parsers enforce:
    at most one TYPE line per metric name, TYPE before that metric's first sample, and all samples of a
    metric name in one contiguous group."""
    typed, started, closed, current = set(), set(), set(), None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("# TYPE "):
            name = line.split()[2]
            assert name not in typed, f"second TYPE line for {name}"
            assert name not in started, f"TYPE for {name} after its samples"
            typed.add(name)
            current = None
            continue
        if line.startswith("#"):
            continue
        name = re.split(r"[{ ]", line, maxsplit=1)[0]
        assert name in typed, f"sample for {name} before/without its TYPE line"
        if name != current:
            assert name not in closed, f"samples of {name} are not contiguous"
            if current is not None:
                closed.add(current)
            current = name
        started.add(name)
    assert text.endswith("\n")


class TestPrometheusExposition:
    def test_labelled_series_share_one_type_line(self, reg):
        reg.inc("dep_total", dependency="a")
        reg.inc("dep_total", dependency="b")
        lines = reg.prometheus_text().splitlines()
        assert lines.count("# TYPE dep_total counter") == 1
        assert 'dep_total{dependency="a"} 1.0' in lines and 'dep_total{dependency="b"} 1.0' in lines

    def test_interleaved_registration_still_groups_each_metric(self, reg):
        reg.inc("a_total", x="1")
        reg.inc("b_total")
        reg.inc("a_total", x="2")
        lines = reg.prometheus_text().splitlines()
        i = lines.index("# TYPE a_total counter")
        assert lines[i + 1:i + 3] == ['a_total{x="1"} 1.0', 'a_total{x="2"} 1.0']
        _assert_valid_exposition("\n".join(lines) + "\n")

    def test_exact_output_for_mixed_registry(self, reg, monkeypatch):
        monkeypatch.setattr(metrics_mod.time, "time", lambda: reg._started + 7)
        reg.inc("dep_total", dependency="a")
        reg.inc("other_total")
        reg.inc("dep_total", dependency="b")
        reg.set_gauge("q", 2)
        reg.observe_ms("lat", 10, dep="x")
        reg.observe_ms("lat", 30, dep="y")
        assert reg.prometheus_text() == (
            "# HELP stockky_uptime_seconds Process uptime\n"
            "# TYPE stockky_uptime_seconds gauge\n"
            "stockky_uptime_seconds 7\n"
            "# TYPE dep_total counter\n"
            'dep_total{dependency="a"} 1.0\n'
            'dep_total{dependency="b"} 1.0\n'
            "# TYPE other_total counter\n"
            "other_total 1.0\n"
            "# TYPE q gauge\n"
            "q 2\n"
            "# TYPE lat_avg_ms gauge\n"
            'lat_avg_ms{dep="x"} 10.0\n'
            'lat_avg_ms{dep="y"} 30.0\n'
            "# TYPE lat_p95_ms gauge\n"
            'lat_p95_ms{dep="x"} 10.0\n'
            'lat_p95_ms{dep="y"} 30.0\n'
        )

    def test_timing_families_are_typed_and_grouped(self, reg):
        for dep in ("db", "cache", "api"):
            reg.observe_ms("stockky_dependency_latency", 5, dependency=dep)
        text = reg.prometheus_text()
        assert text.count("# TYPE stockky_dependency_latency_avg_ms gauge") == 1
        assert text.count("# TYPE stockky_dependency_latency_p95_ms gauge") == 1
        _assert_valid_exposition(text)

    def test_empty_registry_output_is_unchanged(self, reg, monkeypatch):
        monkeypatch.setattr(metrics_mod.time, "time", lambda: reg._started + 3)
        assert reg.prometheus_text() == (
            "# HELP stockky_uptime_seconds Process uptime\n"
            "# TYPE stockky_uptime_seconds gauge\n"
            "stockky_uptime_seconds 3\n"
        )

    def test_name_used_as_counter_and_gauge_keeps_one_type_and_all_samples(self, reg, caplog):
        reg.inc("dual")
        reg.set_gauge("dual", 5, k="v")
        with caplog.at_level(logging.WARNING, logger="metrics"):
            text = reg.prometheus_text()
            reg.prometheus_text()                       # second scrape must not warn again
        lines = text.splitlines()
        assert lines.count("# TYPE dual counter") == 1 and "# TYPE dual gauge" not in lines
        assert "dual 1.0" in lines and 'dual{k="v"} 5' in lines
        assert len([r for r in caplog.records if "both counter and gauge" in r.getMessage()]) == 1
        _assert_valid_exposition(text)

    def test_realistic_registry_is_structurally_valid(self, reg):
        for dep in ("yfinance", "nse", "gemini"):
            reg.inc("stockky_dependency_ok_total", dependency=dep)
            reg.inc("stockky_dependency_errors_total", dependency=dep)
            reg.observe_ms("stockky_dependency_latency", 12.5, dependency=dep)
        for src, status in (("yahoo", "429"), ("yahoo", "503"), ("groq", "429")):
            reg.inc("rate_limit_events", source=src, status=status)
        reg.inc("stockky_scan_complete_total")
        reg.set_gauge("stockky_circuits_open", 1.0)
        reg.set_gauge("stockky_last_scan_symbols", 120.0)
        _assert_valid_exposition(reg.prometheus_text())

    def test_hostile_labels_keep_the_exposition_structurally_valid(self, reg):
        for v in ('q"uote', "new\nline", "back\\slash"):
            reg.inc("rate_limit_events", source=v, status="429")
        _assert_valid_exposition(reg.prometheus_text())


class TestLabelValueEscaping:
    def test_backslash_quote_newline_are_escaped(self):
        key = MetricsRegistry._key("c", {"a": 'x"y\\z\nq'})
        assert key == 'c{a="x\\"y\\\\z\\nq"}'

    def test_backslash_escaped_before_quote(self):
        # value  \"  must become  \\\"  (not \\"), i.e. backslash first, then the quote.
        assert MetricsRegistry._key("c", {"a": '\\"'}) == 'c{a="\\\\\\""}'

    def test_plain_values_unchanged(self):
        assert MetricsRegistry._key("hits", {"b": 2, "a": "x"}) == 'hits{a="x",b="2"}'

    @pytest.mark.parametrize("value,expected", [(None, "None"), (7, "7"), (1.5, "1.5"), (True, "True")])
    def test_non_string_values_are_stringified_as_before(self, value, expected):
        assert MetricsRegistry._key("c", {"a": value}) == f'c{{a="{expected}"}}'

    def test_unprintable_value_falls_back_instead_of_raising(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("no str")

        assert MetricsRegistry._key("c", {"a": Boom()}) == 'c{a="<unprintable>"}'

    def test_distinct_label_sets_do_not_collide(self, reg):
        # Without escaping both of these produced the key  c{a="x",b="y"}  and shared one counter.
        reg.inc("c", a='x",b="y')
        reg.inc("c", a="x", b="y")
        c = reg.snapshot()["counters"]
        assert len(c) == 2 and all(v == 1.0 for v in c.values())

    def test_hostile_value_cannot_inject_a_series_line(self, reg):
        reg.inc("rate_limit_events", source='evil"} 1\nforged_metric{x="y', status="429")
        lines = reg.prometheus_text().splitlines()
        assert not any(l.startswith("forged_metric") for l in lines)
        assert sum(1 for l in lines if l.startswith("rate_limit_events{")) == 1

    def test_every_sample_line_is_valid_exposition_for_hostile_values(self, reg):
        nasty = ['plain', 'q"uote', 'back\\slash', 'new\nline', '\\"', '"} 9\nx{a="', 'trail\\', '', 'a b', '{x}', '日本']
        for i, v in enumerate(nasty):
            reg.inc("stockky_c_total", source=v)
            reg.set_gauge("stockky_g", float(i), source=v)
            reg.observe_ms("stockky_lat", 10 + i, source=v)
        for line in reg.prometheus_text().splitlines():
            if line.startswith("#"):
                continue
            assert _SAMPLE.match(line), line

    def test_label_value_round_trips_through_escape_and_parse(self, reg):
        for v in ['q"uote', 'back\\slash', 'new\nline', '\\"', 'trail\\', 'mix"\\\n"']:
            key = MetricsRegistry._key("c", {"a": v})
            inner = key[len('c{a="'):-len('"}')]
            assert _unescape(inner) == v

    def test_escaped_key_is_what_the_json_snapshot_exposes(self, reg):
        reg.inc("c", a='x"y')
        assert 'c{a="x\\"y"}' in reg.snapshot()["counters"]


class TestLabelLimiter:
    def test_first_n_distinct_values_admitted_then_overflow(self):
        lim = LabelLimiter(max_distinct=3)
        assert [lim.clean("ns", v) for v in ("a", "b", "c")] == ["a", "b", "c"]
        assert lim.clean("ns", "d") == "other"
        assert lim.clean("ns", "e") == "other"

    def test_admitted_values_keep_passing_after_the_cap_is_hit(self):
        lim = LabelLimiter(max_distinct=2)
        lim.clean("ns", "a"), lim.clean("ns", "b"), lim.clean("ns", "zzz")
        assert lim.clean("ns", "a") == "a" and lim.clean("ns", "b") == "b"

    def test_namespaces_are_independent(self):
        lim = LabelLimiter(max_distinct=1)
        assert lim.clean("x", "a") == "a"
        assert lim.clean("y", "a") == "a"
        assert lim.clean("x", "b") == "other"

    def test_per_call_limit_overrides_default(self):
        lim = LabelLimiter(max_distinct=10)
        assert [lim.clean("ns", v, max_distinct=1) for v in ("a", "b")] == ["a", "other"]

    def test_overflow_value_itself_never_consumes_a_slot(self):
        lim = LabelLimiter(max_distinct=1)
        assert lim.clean("ns", "other") == "other"
        assert lim.clean("ns", "a") == "a"

    def test_values_are_stripped_and_truncated(self):
        lim = LabelLimiter(max_distinct=5, max_len=4)
        assert lim.clean("ns", "  yaho ") == "yaho"
        assert lim.clean("ns", "abcdefgh") == "abcd"

    def test_blank_and_unprintable_become_unknown(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("no str")

        lim = LabelLimiter()
        assert lim.clean("ns", "") == "unknown"
        assert lim.clean("ns", "   \t") == "unknown"
        assert lim.clean("ns", Boom()) == "unknown"

    def test_warns_once_per_namespace_on_first_overflow(self, caplog):
        lim = LabelLimiter(max_distinct=1)
        with caplog.at_level(logging.WARNING, logger="metrics"):
            lim.clean("ns", "a")
            lim.clean("ns", "b")
            lim.clean("ns", "c")
            lim.clean("other_ns", "a")
        warns = [r for r in caplog.records if "reached" in r.getMessage()]
        assert len(warns) == 1 and "'ns'" in warns[0].getMessage()

    def test_reset_clears_state(self):
        lim = LabelLimiter(max_distinct=1)
        lim.clean("ns", "a")
        assert lim.clean("ns", "b") == "other"
        lim.reset()
        assert lim.clean("ns", "b") == "b"

    def test_thread_safety_never_exceeds_the_cap(self):
        lim = LabelLimiter(max_distinct=10)
        out = []

        def work(i):
            for j in range(50):
                out.append(lim.clean("ns", f"v{i}-{j}"))

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert len(set(out) - {"other"}) == 10

    def test_registry_series_stay_bounded_when_labels_go_through_the_limiter(self, reg):
        lim = LabelLimiter(max_distinct=5)
        for i in range(1000):
            reg.inc("rate_limit_events", source=lim.clean("s", f"src{i}"))
        c = reg.snapshot()["counters"]
        assert len(c) == 6 and sum(c.values()) == 1000.0

    def test_module_singleton_is_a_limiter(self):
        assert isinstance(metrics_mod.label_limiter, LabelLimiter)


def test_module_singleton_is_a_registry():
    assert isinstance(metrics_mod.metrics, MetricsRegistry)
