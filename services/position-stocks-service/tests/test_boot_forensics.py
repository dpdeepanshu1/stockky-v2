"""
tests/test_boot_forensics.py

Covers boot_forensics.py — previously 67%, missing lines 49-50, 62, 69-70,
84-85, 89-105, 131-152, 197-198 (position-stocks-service, session112 round
10). Ported from real-trade-service's tests/test_boot_forensics.py after
confirming the two boot_forensics.py copies are byte-identical — same
comprehensive suite applies unchanged apart from the service-name strings
below. Supersedes this service's previous, much smaller
test_boot_forensics.py, which only covered the wrong-shape-JSON regression
and the basic _classify causes via record_boot — this version keeps those
same scenarios plus direct coverage of every other function in the module
(memory_snapshot's cgroup v1/proc-RSS branches, _write's except branch, the
full _heartbeat_loop, _install_signal_logging's real handler chaining/
SIG_DFL branches, mark_clean_shutdown's except branch).

boot_forensics is the "why did the container restart with nothing in the
logs?" tool (cgroup OOM-kill = silent SIGKILL). It runs inside the service
that holds real-money state, so the contract that matters most is:
**it must never raise into the caller and must never disturb the process**.
Tests are grouped by function and hermetic:

  * state file  -> tmp_path (via monkeypatch of _STATE_PATH); never /tmp
  * signals     -> a fake `signal` module; the real SIGINT/SIGTERM handlers
                   (pytest's Ctrl-C included) are never touched
  * heartbeat   -> fake `time` whose sleep() raises a BaseException after N
                   iterations, so the infinite loop is driven deterministically
  * cgroup/proc -> fake _read_int / patched open(); works the same on any host
  * threads     -> record_boot's daemon Thread is replaced by a recorder

Also pins one behaviour fixed in an earlier session (same fix applied to the
identical copies in real-trade-service/ and api-gateway/): record_boot used
to raise on a previous-state file that was valid JSON of the wrong shape,
before the bad file was ever replaced, so forensics stayed dead on every
later boot.

    cd services/position-stocks-service
    python -m pytest tests/test_boot_forensics.py -v --cov=boot_forensics --cov-report=term-missing
"""
from __future__ import annotations

import builtins
import io
import json
import logging
import os
import re
import signal
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import boot_forensics as bf

MB = 1048576
LOGGER = "boot-forensics"


# ─────────────────────────── shared fixtures / fakes ───────────────────────────

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every test: private state file, empty _state, and _started=True so
    record_boot never spawns the real heartbeat thread or hijacks the real
    SIGTERM/SIGINT handlers (tests that need _started=False set it and stub
    both side effects)."""
    monkeypatch.setattr(bf, "_STATE_PATH", str(tmp_path / "boot_state.json"))
    monkeypatch.setattr(bf, "_state", {})
    monkeypatch.setattr(bf, "_started", True)


def _state_file() -> dict:
    with open(bf._STATE_PATH) as f:
        return json.load(f)


def _seed_state_file(obj) -> None:
    with open(bf._STATE_PATH, "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


class _Stop(BaseException):
    """Raised by the fake sleep to break out of the infinite heartbeat loop
    (BaseException so the loop's own `except Exception` can't swallow it)."""


class FakeTime:
    def __init__(self, start=1_000_000.0, max_sleeps=1, step=15.0):
        self.now = start
        self.max_sleeps = max_sleeps
        self.step = step
        self.sleeps = 0

    def time(self):
        return self.now

    def sleep(self, _s):
        self.sleeps += 1
        if self.sleeps > self.max_sleeps:
            raise _Stop()
        self.now += self.step


def _run_heartbeat(monkeypatch, *, max_sleeps=1, step=15.0, service="svc") -> FakeTime:
    ft = FakeTime(max_sleeps=max_sleeps, step=step)
    monkeypatch.setattr(bf, "time", ft)
    with pytest.raises(_Stop):
        bf._heartbeat_loop(service)
    return ft


def _mem(used_mb, limit_mb):
    return {"used": None if used_mb is None else int(used_mb * MB),
            "limit": None if limit_mb is None else int(limit_mb * MB),
            "rss": 1234}


# ─────────────────────────────── _read_int ───────────────────────────────

class TestReadInt:
    def test_reads_integer_with_trailing_newline(self, tmp_path):
        p = tmp_path / "mem"
        p.write_text("123456\n")
        assert bf._read_int(str(p)) == 123456

    def test_max_means_unlimited_none(self, tmp_path):
        p = tmp_path / "mem"
        p.write_text("max\n")
        assert bf._read_int(str(p)) is None

    def test_missing_file_is_none(self, tmp_path):
        assert bf._read_int(str(tmp_path / "does-not-exist")) is None

    def test_non_integer_content_is_none(self, tmp_path):
        p = tmp_path / "mem"
        p.write_text("not-a-number")
        assert bf._read_int(str(p)) is None

    def test_empty_file_is_none(self, tmp_path):
        p = tmp_path / "mem"
        p.write_text("")
        assert bf._read_int(str(p)) is None


# ───────────────────────────── memory_snapshot ─────────────────────────────

V2_USED, V2_LIMIT = "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"
V1_USED = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
V1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"


def _fake_read_int(monkeypatch, values: dict) -> list:
    calls: list = []

    def _fake(path):
        calls.append(path)
        return values.get(path)

    monkeypatch.setattr(bf, "_read_int", _fake)
    return calls


def _fake_proc_status(monkeypatch, content=None, raises=False):
    real_open = builtins.open

    def _open(path, *a, **k):
        if path == "/proc/self/status":
            if raises:
                raise OSError("no /proc here")
            return io.StringIO(content)
        return real_open(path, *a, **k)  # pragma: no cover

    monkeypatch.setattr(builtins, "open", _open)


class TestMemorySnapshot:
    def test_cgroup_v2_used_and_limit(self, monkeypatch):
        calls = _fake_read_int(monkeypatch, {V2_USED: 200 * MB, V2_LIMIT: 1024 * MB})
        _fake_proc_status(monkeypatch, "Name:\tpython\nVmRSS:\t  2048 kB\n")
        snap = bf.memory_snapshot()
        assert snap == {"used": 200 * MB, "limit": 1024 * MB, "rss": 2048 * 1024}
        assert V1_USED not in calls and V1_LIMIT not in calls  # v1 never consulted

    def test_cgroup_v2_unlimited_limit_is_none(self, monkeypatch):
        _fake_read_int(monkeypatch, {V2_USED: 200 * MB, V2_LIMIT: None})
        _fake_proc_status(monkeypatch, "VmRSS:\t 1 kB\n")
        snap = bf.memory_snapshot()
        assert snap["used"] == 200 * MB and snap["limit"] is None

    def test_falls_back_to_cgroup_v1(self, monkeypatch):
        calls = _fake_read_int(monkeypatch, {V1_USED: 300 * MB, V1_LIMIT: 512 * MB})
        _fake_proc_status(monkeypatch, "VmRSS:\t 1 kB\n")
        snap = bf.memory_snapshot()
        assert snap["used"] == 300 * MB and snap["limit"] == 512 * MB
        assert V1_USED in calls and V1_LIMIT in calls

    def test_v1_absurdly_large_limit_means_unlimited(self, monkeypatch):
        """cgroup v1 reports 'no limit' as a huge number (~2**63), not 'max'."""
        _fake_read_int(monkeypatch, {V1_USED: 300 * MB, V1_LIMIT: (1 << 63) - 4096})
        _fake_proc_status(monkeypatch, "VmRSS:\t 1 kB\n")
        assert bf.memory_snapshot()["limit"] is None

    def test_v1_limit_exactly_at_threshold_is_kept(self, monkeypatch):
        _fake_read_int(monkeypatch, {V1_USED: 1, V1_LIMIT: 1 << 60})
        _fake_proc_status(monkeypatch, "VmRSS:\t 1 kB\n")
        assert bf.memory_snapshot()["limit"] == 1 << 60

    def test_v1_missing_limit_stays_none(self, monkeypatch):
        _fake_read_int(monkeypatch, {V1_USED: 300 * MB})
        _fake_proc_status(monkeypatch, "VmRSS:\t 1 kB\n")
        snap = bf.memory_snapshot()
        assert snap["used"] == 300 * MB and snap["limit"] is None

    def test_no_cgroup_files_at_all(self, monkeypatch):
        _fake_read_int(monkeypatch, {})
        _fake_proc_status(monkeypatch, "VmRSS:\t 7 kB\n")
        assert bf.memory_snapshot() == {"used": None, "limit": None, "rss": 7 * 1024}

    def test_rss_absent_from_proc_status_is_none(self, monkeypatch):
        _fake_read_int(monkeypatch, {V2_USED: 1, V2_LIMIT: 2})
        _fake_proc_status(monkeypatch, "Name:\tpython\nVmSize:\t 99 kB\n")
        assert bf.memory_snapshot()["rss"] is None

    def test_proc_status_unreadable_is_none(self, monkeypatch):
        _fake_read_int(monkeypatch, {V2_USED: 1, V2_LIMIT: 2})
        _fake_proc_status(monkeypatch, raises=True)
        assert bf.memory_snapshot()["rss"] is None

    def test_malformed_vmrss_line_is_swallowed(self, monkeypatch):
        _fake_read_int(monkeypatch, {V2_USED: 1, V2_LIMIT: 2})
        _fake_proc_status(monkeypatch, "VmRSS:\n")  # no value -> IndexError inside
        assert bf.memory_snapshot()["rss"] is None

    def test_real_host_snapshot_has_the_three_keys(self):
        """No fakes: on any host (container, VM, CI) it must return the
        documented shape and never raise."""
        snap = bf.memory_snapshot()
        assert set(snap) == {"used", "limit", "rss"}
        for v in snap.values():
            assert v is None or isinstance(v, int)


class TestMb:
    @pytest.mark.parametrize("raw,expected", [
        (None, None), (0, 0.0), (MB, 1.0), (int(1.5 * MB), 1.5), (1024 * MB, 1024.0),
    ])
    def test_mb(self, raw, expected):
        assert bf._mb(raw) == expected


# ─────────────────────────────────── _write ───────────────────────────────────

class TestWrite:
    def test_writes_state_as_json_and_leaves_no_tmp_file(self):
        bf._state.update({"service": "svc", "pid": 1})
        bf._write()
        assert _state_file() == {"service": "svc", "pid": 1}
        assert not os.path.exists(bf._STATE_PATH + ".tmp")

    def test_overwrites_previous_content(self):
        bf._state.update({"a": 1})
        bf._write()
        bf._state.clear()
        bf._state.update({"b": 2})
        bf._write()
        assert _state_file() == {"b": 2}

    def test_unwritable_location_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bf, "_STATE_PATH", str(tmp_path / "no-such-dir" / "state.json"))
        bf._state.update({"a": 1})
        assert bf._write() is None  # no exception

    def test_unserialisable_state_is_swallowed_and_previous_file_survives(self):
        bf._state.update({"good": True})
        bf._write()
        bf._state["bad"] = {1, 2, 3}  # sets are not JSON-serialisable
        assert bf._write() is None
        assert _state_file() == {"good": True}  # atomic replace never happened


# ────────────────────────────── _heartbeat_loop ──────────────────────────────

class TestHeartbeatLoop:
    def test_iteration_refreshes_state_and_persists_it(self, monkeypatch):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(100, 1000))
        ft = _run_heartbeat(monkeypatch, max_sleeps=1)
        assert bf._state["last_heartbeat"] == ft.now
        assert bf._state["mem"] == _mem(100, 1000)
        on_disk = _state_file()
        assert on_disk["last_heartbeat"] == ft.now
        assert on_disk["mem"] == _mem(100, 1000)

    def test_sleeps_for_the_configured_interval(self, monkeypatch):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(1, 1000))
        seen = []
        ft = FakeTime(max_sleeps=1)
        real_sleep = ft.sleep
        ft.sleep = lambda s: (seen.append(s), real_sleep(s))[1]
        monkeypatch.setattr(bf, "time", ft)
        monkeypatch.setattr(bf, "_HEARTBEAT_S", 7.5)
        with pytest.raises(_Stop):
            bf._heartbeat_loop("svc")
        assert seen[0] == 7.5

    def test_below_threshold_no_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(84, 100))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=2)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_at_threshold_warns_with_service_and_numbers(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(85, 100))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=1, service="position-stocks-service")
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        msg = warns[0].getMessage()
        assert "MEMORY PRESSURE [position-stocks-service]" in msg
        assert "85/100 MB" in msg and "85%" in msg

    def test_custom_threshold_is_honoured(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "_MEM_WARN_PCT", 50.0)
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(60, 100))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=1)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_warning_is_rate_limited_to_once_per_minute(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(95, 100))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=4, step=15.0)  # 4 beats over 60s
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_warning_repeats_after_the_cooldown(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(95, 100))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=3, step=61.0)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3

    @pytest.mark.parametrize("mem", [
        {"used": 900 * MB, "limit": None, "rss": 1},   # unlimited cgroup
        {"used": None, "limit": 1000 * MB, "rss": 1},  # unreadable usage
        {"used": 0, "limit": 1000 * MB, "rss": 1},     # zero usage is falsy
    ])
    def test_missing_used_or_limit_never_warns(self, monkeypatch, caplog, mem):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: dict(mem))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            _run_heartbeat(monkeypatch, max_sleeps=1)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert bf._state["mem"] == mem  # state still refreshed

    def test_snapshot_failure_is_swallowed_and_loop_keeps_beating(self, monkeypatch):
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("cgroup vanished")
            return _mem(10, 1000)

        monkeypatch.setattr(bf, "memory_snapshot", _flaky)
        ft = _run_heartbeat(monkeypatch, max_sleeps=2)
        assert calls["n"] == 2
        assert bf._state["mem"] == _mem(10, 1000)
        assert bf._state["last_heartbeat"] == ft.now


# ────────────────────────────────── _classify ──────────────────────────────────

NOW = 10_000.0


class TestClassify:
    def test_no_previous_state_is_fresh_container(self):
        cause, detail = bf._classify(None, NOW)
        assert cause == "FRESH_CONTAINER"
        assert "re-created" in detail

    def test_corrupt_previous_state_is_unknown(self):
        cause, detail = bf._classify({"corrupt": "Expecting value: line 1"}, NOW)
        assert cause == "UNKNOWN"
        assert "unreadable" in detail and "Expecting value" in detail

    def test_clean_shutdown_wins_even_with_a_signal_recorded(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9100.0,
                "clean_shutdown": True, "signal": "SIGTERM"}
        cause, detail = bf._classify(prev, NOW)
        assert cause == "RESTART_AFTER_CLEAN_SHUTDOWN"
        assert "signal=SIGTERM" in detail and "ran ~100s" in detail

    def test_signal_without_clean_shutdown_is_sigterm_but_not_clean(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9100.0, "signal": "SIGINT",
                "mem": {"used": 500 * MB, "limit": 1000 * MB}}
        cause, detail = bf._classify(prev, NOW)
        assert cause == "SIGTERM_BUT_NOT_CLEAN"
        assert "SIGINT" in detail and "ran ~100s" in detail
        assert "last memory 500.0/1000.0 MB" in detail

    def test_no_signal_no_clean_flag_is_died_without_clean_shutdown(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9970.0,
                "mem": {"used": 990 * MB, "limit": 1000 * MB}}
        cause, detail = bf._classify(prev, NOW)
        assert cause == "DIED_WITHOUT_CLEAN_SHUTDOWN"
        assert "Last heartbeat 30s before this boot" in detail  # NOW - 9970
        assert "ran ~970s" in detail                            # 9970 - 9000
        assert "last memory 990.0/1000.0 MB" in detail          # OOM signature: used ~= limit

    def test_missing_memory_sample_says_so(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9100.0}
        _, detail = bf._classify(prev, NOW)
        assert "no memory sample" in detail

    def test_mem_dict_without_usage_counts_as_no_sample(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9100.0, "mem": {"used": None, "limit": 5}}
        _, detail = bf._classify(prev, NOW)
        assert "no memory sample" in detail

    def test_missing_heartbeat_falls_back_to_started_at(self):
        prev = {"started_at": 9500.0}
        cause, detail = bf._classify(prev, NOW)
        assert cause == "DIED_WITHOUT_CLEAN_SHUTDOWN"
        assert "Last heartbeat 500s before this boot" in detail  # NOW - started_at
        assert "ran ~500s" in detail                              # missing heartbeat -> `now` - started_at

    def test_empty_state_dict_does_not_blow_up(self):
        cause, detail = bf._classify({}, NOW)
        assert cause == "DIED_WITHOUT_CLEAN_SHUTDOWN"
        assert "Last heartbeat 0s before this boot" in detail and "ran ~0s" in detail

    def test_none_valued_signal_is_not_a_signal(self):
        prev = {"started_at": 9000.0, "last_heartbeat": 9100.0, "signal": None, "clean_shutdown": False}
        assert bf._classify(prev, NOW)[0] == "DIED_WITHOUT_CLEAN_SHUTDOWN"


# ───────────────────────── _install_signal_logging ─────────────────────────

class FakeSignal:
    """Stand-in for the `signal` module. Real enum members are reused so
    `sig.name` works; nothing here can touch the real process handlers."""
    SIGTERM = signal.SIGTERM
    SIGINT = signal.SIGINT
    SIG_DFL = signal.SIG_DFL
    SIG_IGN = signal.SIG_IGN

    def __init__(self, previous: dict, signal_raises=None, getsignal_raises=None):
        self.handlers = dict(previous)
        self.set_calls: list = []
        self.getsignal_calls: list = []
        self._signal_raises = signal_raises
        self._getsignal_raises = getsignal_raises

    def getsignal(self, sig):
        self.getsignal_calls.append(sig)
        if self._getsignal_raises:
            raise self._getsignal_raises
        return self.handlers.get(sig)

    def signal(self, sig, handler):
        self.set_calls.append((sig, handler))
        if self._signal_raises:
            raise self._signal_raises
        self.handlers[sig] = handler


@pytest.fixture()
def kill_calls(monkeypatch):
    calls: list = []
    monkeypatch.setattr(bf.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


def _install(monkeypatch, previous, **kw) -> FakeSignal:
    fake = FakeSignal(previous, **kw)
    monkeypatch.setattr(bf, "signal", fake)
    bf._install_signal_logging("svc")
    return fake


class TestInstallSignalLogging:
    def test_installs_a_handler_for_sigterm_and_sigint(self, monkeypatch):
        fake = _install(monkeypatch, {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL})
        assert [s for s, _ in fake.set_calls] == [signal.SIGTERM, signal.SIGINT]
        assert all(callable(h) for _, h in fake.set_calls)

    def test_handler_records_signal_persists_logs_and_chains_to_callable_previous(
        self, monkeypatch, caplog
    ):
        chained: list = []

        def _uvicorn_handler(signum, frame):
            chained.append((signum, frame))

        fake = _install(monkeypatch, {signal.SIGTERM: _uvicorn_handler, signal.SIGINT: _uvicorn_handler})
        bf._state["started_at"] = time.time() - 42
        handler = fake.handlers[signal.SIGTERM]
        sentinel_frame = object()

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            handler(signal.SIGTERM, sentinel_frame)

        assert bf._state["signal"] == "SIGTERM"
        assert _state_file()["signal"] == "SIGTERM"
        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) == 1
        msg = warns[0].getMessage()
        assert "BOOT FORENSICS [svc]" in msg and "received SIGTERM" in msg and "uptime 42s" in msg
        assert chained == [(signal.SIGTERM, sentinel_frame)]  # uvicorn's graceful shutdown intact

    def test_sigint_handler_records_its_own_name(self, monkeypatch):
        fake = _install(monkeypatch, {signal.SIGTERM: signal.SIG_IGN, signal.SIGINT: signal.SIG_IGN})
        fake.handlers[signal.SIGINT](signal.SIGINT, None)
        assert bf._state["signal"] == "SIGINT"

    def test_default_previous_handler_is_restored_and_signal_re_raised(self, monkeypatch, kill_calls):
        fake = _install(monkeypatch, {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL})
        fake.set_calls.clear()
        fake.handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert fake.set_calls == [(signal.SIGTERM, signal.SIG_DFL)]  # default disposition back
        assert kill_calls == [(os.getpid(), signal.SIGTERM)]        # process still terminates

    def test_ignored_previous_handler_is_not_chained_or_re_raised(self, monkeypatch, kill_calls):
        fake = _install(monkeypatch, {signal.SIGTERM: signal.SIG_IGN, signal.SIGINT: None})
        fake.set_calls.clear()
        fake.handlers[signal.SIGTERM](signal.SIGTERM, None)
        fake.handlers[signal.SIGINT](signal.SIGINT, None)
        assert fake.set_calls == [] and kill_calls == []
        assert bf._state["signal"] == "SIGINT"  # still recorded

    def test_logging_failure_inside_handler_never_blocks_chaining(self, monkeypatch):
        chained: list = []

        class _BrokenLogger:
            def warning(self, *a, **k):
                raise RuntimeError("logging blew up")

        monkeypatch.setattr(bf, "logger", _BrokenLogger())
        fake = _install(monkeypatch,
                        {signal.SIGTERM: lambda s, f: chained.append(s), signal.SIGINT: None})
        fake.handlers[signal.SIGTERM](signal.SIGTERM, None)  # must not raise
        assert chained == [signal.SIGTERM]

    def test_not_main_thread_signal_error_is_swallowed_for_every_signal(self, monkeypatch):
        fake = _install(monkeypatch,
                        {signal.SIGTERM: signal.SIG_DFL, signal.SIGINT: signal.SIG_DFL},
                        signal_raises=ValueError("signal only works in main thread"))
        assert len(fake.set_calls) == 2  # tried both; neither failure propagated

    def test_getsignal_failure_is_swallowed_for_every_signal(self, monkeypatch):
        fake = _install(monkeypatch, {}, getsignal_raises=OSError("boom"))
        assert len(fake.getsignal_calls) == 2 and fake.set_calls == []


# ─────────────────────────────────── record_boot ───────────────────────────────────

class _FakeThread:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True


@pytest.fixture()
def spawn_stubs(monkeypatch):
    """For tests that let record_boot take its first-boot branch: stub the two
    side effects (signal handlers, heartbeat thread) and record them."""
    _FakeThread.instances = []
    installs: list = []
    monkeypatch.setattr(bf, "_started", False)
    monkeypatch.setattr(bf, "_install_signal_logging", lambda svc: installs.append(svc))
    monkeypatch.setattr(bf, "threading", SimpleNamespace(Thread=_FakeThread))
    return installs


def _levels(caplog):
    return [r.levelno for r in caplog.records if r.name == LOGGER]


class TestRecordBoot:
    def test_fresh_container_returns_cause_and_persists_full_state(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: _mem(100, 1000))
        before = time.time()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            result = bf.record_boot("position-stocks-service")

        assert result["cause"] == "FRESH_CONTAINER" and "re-created" in result["detail"]
        st = _state_file()
        assert st["service"] == "position-stocks-service"
        assert re.fullmatch(r"[0-9a-f]{8}", st["boot_id"])
        assert st["pid"] == os.getpid()
        assert before <= st["started_at"] <= time.time()
        assert st["last_heartbeat"] == st["started_at"]
        assert st["clean_shutdown"] is False and st["signal"] is None
        assert st["mem"] == _mem(100, 1000)
        assert st == bf._state

        assert _levels(caplog) == [logging.INFO]
        msg = caplog.records[-1].getMessage()
        assert "BOOT FORENSICS [position-stocks-service]" in msg
        assert f"boot_id={st['boot_id']}" in msg and "cause=FRESH_CONTAINER" in msg
        assert "cgroup 100.0/1000.0 MB" in msg

    def test_unknown_memory_is_logged_without_crashing(self, monkeypatch, caplog):
        monkeypatch.setattr(bf, "memory_snapshot", lambda: {"used": None, "limit": None, "rss": None})
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            bf.record_boot("svc")
        assert "cgroup None/None MB" in caplog.records[-1].getMessage()

    def test_boot_ids_differ_between_boots(self):
        bf.record_boot("svc")
        first = bf._state["boot_id"]
        bf.record_boot("svc")
        assert bf._state["boot_id"] != first

    def test_state_is_replaced_not_merged(self):
        bf._state.update({"shutdown_reason": "stale", "signal": "SIGTERM", "junk": 1})
        bf.record_boot("svc")
        assert "shutdown_reason" not in bf._state and "junk" not in bf._state
        assert bf._state["signal"] is None

    @pytest.mark.parametrize("previous,cause,level", [
        ({"started_at": 1.0, "last_heartbeat": 2.0, "clean_shutdown": True, "signal": "SIGTERM"},
         "RESTART_AFTER_CLEAN_SHUTDOWN", logging.WARNING),
        ({"started_at": 1.0, "last_heartbeat": 2.0, "signal": "SIGTERM", "clean_shutdown": False},
         "SIGTERM_BUT_NOT_CLEAN", logging.ERROR),
        ({"started_at": 1.0, "last_heartbeat": 2.0, "clean_shutdown": False, "signal": None,
          "mem": {"used": 990 * MB, "limit": 1000 * MB}},
         "DIED_WITHOUT_CLEAN_SHUTDOWN", logging.ERROR),
    ])
    def test_previous_exit_is_classified_and_logged_at_the_right_level(
        self, previous, cause, level, caplog
    ):
        _seed_state_file(previous)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            result = bf.record_boot("svc")
        assert result["cause"] == cause
        assert _levels(caplog) == [level]
        assert f"cause={cause}" in caplog.records[-1].getMessage()

    def test_oom_signature_is_visible_in_the_returned_detail(self):
        _seed_state_file({"started_at": 1.0, "last_heartbeat": 2.0,
                          "mem": {"used": 999 * MB, "limit": 1000 * MB}})
        detail = bf.record_boot("svc")["detail"]
        assert "OOM-killer" in detail and "last memory 999.0/1000.0 MB" in detail

    @pytest.mark.parametrize("content", ["{not json", "", "[1, 2", "\x00\x01"])
    def test_unreadable_previous_file_is_unknown_and_self_heals(self, content, caplog):
        _seed_state_file(content)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            result = bf.record_boot("svc")
        assert result["cause"] == "UNKNOWN" and "unreadable" in result["detail"]
        assert _levels(caplog) == [logging.WARNING]
        assert _state_file()["boot_id"] == bf._state["boot_id"]  # bad file replaced

    def test_json_null_previous_file_counts_as_fresh(self):
        _seed_state_file("null")
        assert bf.record_boot("svc")["cause"] == "FRESH_CONTAINER"

    def test_state_path_being_a_directory_does_not_raise(self, tmp_path, monkeypatch):
        d = tmp_path / "a-directory"
        d.mkdir()
        monkeypatch.setattr(bf, "_STATE_PATH", str(d))
        result = bf.record_boot("svc")  # open() -> IsADirectoryError; _write() fails silently
        assert result["cause"] == "UNKNOWN"

    # ── regression: fixed this session ──
    @pytest.mark.parametrize("previous", [
        [],                                                        # valid JSON, not an object
        [1, 2, 3],
        "a string",
        42,
        {"last_heartbeat": "abc", "started_at": 1.0},              # non-numeric timestamp
        {"started_at": "x", "last_heartbeat": None},
        {"started_at": 1.0, "last_heartbeat": 2.0, "mem": "oops"},  # mem not a dict
        {"started_at": 1.0, "last_heartbeat": 2.0, "mem": {"used": "x", "limit": "y"}},
    ])
    def test_valid_json_of_wrong_shape_does_not_raise_and_self_heals(self, previous, caplog):
        """Previously _classify() raised out of record_boot BEFORE the bad file
        was replaced -> every later boot raised too, the heartbeat never
        started, and main.py's startup hook (same try-block) also skipped
        log_auth_config. Contract is 'never raises into the caller'."""
        _seed_state_file(json.dumps(previous))  # always valid JSON, even for bare strings
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            result = bf.record_boot("svc")

        assert result["cause"] == "UNKNOWN" and "unusable" in result["detail"]
        assert _levels(caplog) == [logging.WARNING]
        # the poisoned file is gone: state is rewritten, and the NEXT boot classifies normally
        assert _state_file()["service"] == "svc"
        assert bf.record_boot("svc")["cause"] == "DIED_WITHOUT_CLEAN_SHUTDOWN"

    def test_first_boot_installs_signal_logging_and_starts_daemon_heartbeat_once(self, spawn_stubs):
        bf.record_boot("position-stocks-service")
        assert spawn_stubs == ["position-stocks-service"]
        assert bf._started is True
        assert len(_FakeThread.instances) == 1
        t = _FakeThread.instances[0]
        assert t.started is True
        assert t.kw["target"] is bf._heartbeat_loop
        assert t.kw["args"] == ("position-stocks-service",)
        assert t.kw["name"] == "boot-forensics-heartbeat"
        assert t.kw["daemon"] is True

        bf.record_boot("position-stocks-service")  # re-boot in the same process
        assert spawn_stubs == ["position-stocks-service"] and len(_FakeThread.instances) == 1

    def test_already_started_never_touches_signals_or_threads(self, monkeypatch):
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called"))  # noqa: E731
        monkeypatch.setattr(bf, "_install_signal_logging", boom)
        monkeypatch.setattr(bf, "threading", SimpleNamespace(Thread=boom))
        bf.record_boot("svc")  # _started is True from the autouse fixture

    def test_full_lifecycle_through_the_real_state_file(self):
        assert bf.record_boot("svc")["cause"] == "FRESH_CONTAINER"
        assert bf.record_boot("svc")["cause"] == "DIED_WITHOUT_CLEAN_SHUTDOWN"  # no clean flag = SIGKILL/OOM
        bf.mark_clean_shutdown()
        assert bf.record_boot("svc")["cause"] == "RESTART_AFTER_CLEAN_SHUTDOWN"
        st = _state_file()
        st["signal"] = "SIGTERM"
        _seed_state_file(st)
        assert bf.record_boot("svc")["cause"] == "SIGTERM_BUT_NOT_CLEAN"


# ───────────────────────────── mark_clean_shutdown ─────────────────────────────

class TestMarkCleanShutdown:
    def test_marks_state_and_persists_with_default_reason(self):
        bf.record_boot("svc")
        before = time.time()
        bf.mark_clean_shutdown()
        assert bf._state["clean_shutdown"] is True
        assert bf._state["shutdown_reason"] == "shutdown-event"
        assert bf._state["last_heartbeat"] >= before
        assert _state_file() == bf._state

    def test_custom_reason_is_recorded(self):
        bf.mark_clean_shutdown("manual-test")
        assert _state_file()["shutdown_reason"] == "manual-test"

    def test_works_before_any_boot_was_recorded(self):
        bf.mark_clean_shutdown()  # empty _state
        assert _state_file()["clean_shutdown"] is True

    def test_never_raises(self, monkeypatch):
        class _BrokenLock:
            def __enter__(self):
                raise RuntimeError("lock exploded")

            def __exit__(self, *a):  # pragma: no cover - never reached, __enter__ raises
                return False

        monkeypatch.setattr(bf, "_lock", _BrokenLock())
        assert bf.mark_clean_shutdown() is None
