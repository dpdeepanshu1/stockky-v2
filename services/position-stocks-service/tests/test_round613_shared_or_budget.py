"""Round 613: the live OR rescue count is ONE shared file, kept across restarts (orders/shared_or_budget.py). Plain runner: `python tests/test_round613_shared_or_budget.py` (also collected by pytest)."""
import datetime as dt
import json
import multiprocessing
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DIR = tempfile.mkdtemp()
os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(_DIR, "or_live_budget.json")
os.environ.pop("OR_LIVE_SHARED_BUDGET", None)
import config  # noqa: E402
from orders import shared_or_budget as sb  # noqa: E402
from orders import or_live as o  # noqa: E402

DAY = "2026-11-18"


def _fresh(shared="1"):
    os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(tempfile.mkdtemp(), "or_live_budget.json")
    os.environ["OR_LIVE_SHARED_BUDGET"] = shared
    sb.reset_process()


def test_two_services_spend_one_budget():
    _fresh()
    got = [sb.take(DAY, 4, "rt"), sb.take(DAY, 4, "ps"), sb.take(DAY, 4, "rt"), sb.take(DAY, 4, "ps"), sb.take(DAY, 4, "rt"), sb.take(DAY, 4, "ps")]
    assert [g[0] for g in got] == [True, True, True, True, False, False]
    assert [g[1] for g in got] == [1, 2, 3, 4, 4, 4] and all(g[2] == "file" for g in got)
    rec = json.load(open(sb.path()))
    assert rec == {"day": DAY, "n": 4, "by": {"rt": 2, "ps": 2}}


def test_count_survives_a_restart():
    _fresh()
    assert sb.take(DAY, 2, "rt")[0] and sb.take(DAY, 2, "rt")[0]
    sb.reset_process()                                   # the process restarts: its memory is gone, the file is not
    assert sb.take(DAY, 2, "rt") == (False, 2, "file")
    assert sb.used(DAY) == 2


def test_a_new_day_starts_at_zero():
    _fresh()
    sb.take(DAY, 1, "rt")
    assert sb.take(DAY, 1, "rt")[0] is False
    assert sb.take("2026-11-19", 1, "rt") == (True, 1, "file")
    assert sb.used(DAY) == 0 and sb.used("2026-11-19") == 1


def test_cap_zero_and_bad_cap_take_nothing():
    _fresh()
    assert sb.take(DAY, 0, "rt")[0] is False and sb.take(DAY, -3, "rt")[0] is False and sb.take(DAY, "x", "rt")[0] is False
    assert not os.path.exists(sb.path())


def test_corrupt_file_counts_from_zero():
    _fresh()
    os.makedirs(os.path.dirname(sb.path()), exist_ok=True)
    for junk in ("{not json", "[]", "", json.dumps({"day": DAY, "n": "x"}), json.dumps({"day": DAY, "n": -5, "by": 7})):
        open(sb.path(), "w").write(junk)
        sb.reset_process()
        ok, n, src = sb.take(DAY, 4, "rt")
        assert ok is True and n == 1 and src == "file", junk


def test_unusable_file_falls_back_to_the_process_count():
    _fresh()
    blocker = os.path.join(tempfile.mkdtemp(), "afile")
    open(blocker, "w").write("x")
    os.environ["OR_LIVE_BUDGET_FILE"] = os.path.join(blocker, "sub", "b.json")   # a directory under a regular file: cannot be created
    got = [sb.take(DAY, 2, "rt") for _ in range(3)]
    assert got == [(True, 1, "process"), (True, 2, "process"), (False, 2, "process")]
    assert sb.used(DAY) is None


def test_switch_off_is_the_round_612_per_process_count():
    _fresh(shared="0")
    assert [sb.take(DAY, 2, "rt")[2] for _ in range(3)] == ["process"] * 3
    assert not os.path.exists(sb.path())


def _worker(args):
    path, q = args
    os.environ["OR_LIVE_BUDGET_FILE"] = path
    os.environ["OR_LIVE_SHARED_BUDGET"] = "1"
    sb.reset_process()
    q.put(sb.take(DAY, 5, "w")[0])


def test_parallel_processes_never_spend_more_than_the_cap():
    _fresh()
    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    ps = [ctx.Process(target=_worker, args=((sb.path(), q),)) for _ in range(12)]
    [p.start() for p in ps]
    [p.join(20) for p in ps]
    res = [q.get(timeout=5) for _ in ps]
    assert sum(1 for r in res if r) == 5 and json.load(open(sb.path()))["n"] == 5


def test_or_live_allow_uses_the_shared_budget():
    _fresh()
    config.OR_LIVE_MODE, config.OR_LIVE_SINCE_IST, config.OR_LIVE_MAX_PER_DAY = "live", "09:45", 3
    hit = {"rule": "B", "confirms": ["vwap", "event", "sector_top"], "refused": "x"}
    t = dt.datetime(2026, 11, 18, 10, 0)
    assert [o.allow(hit, t) for _ in range(4)] == [True, True, True, False]
    assert o.used_today() == 3 and o.budget_source() == "file"
    # the other service's spend counts here too
    sb.reset_process()
    assert o.allow(hit, t) is False and o.used_today() == 3
    # a restarted process still sees the day's total
    o._LAST["n"] = 0
    assert o.allow(hit, t) is False
    config.OR_LIVE_MAX_PER_DAY = 4
    assert o.allow(hit, t) is True and o.used_today() == 4


def test_or_live_allow_still_checks_time_and_mode_before_spending():
    _fresh()
    config.OR_LIVE_MODE, config.OR_LIVE_SINCE_IST, config.OR_LIVE_MAX_PER_DAY = "live", "09:45", 4
    hit = {"rule": "B", "confirms": ["vwap"], "refused": "x"}
    assert o.allow(hit, dt.datetime(2026, 11, 18, 9, 30)) is False
    config.OR_LIVE_MODE = "off"
    assert o.allow(hit, dt.datetime(2026, 11, 18, 10, 0)) is False
    assert sb.used(DAY) == 0                              # a refused call spends nothing
    config.OR_LIVE_MODE = "live"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
