"""group466: run_jobs checkpoint / resume (a run that hit its time limit continues instead of starting over). Offline, stdlib only. Plain asserts or pytest."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _progress import file_stamp, run_jobs, settings_stamp  # noqa: E402

CALLS: list = []


def _sq(j):
    CALLS.append(j[1])
    return {"sym": j[1], "v": j[0] ** 2}


def _jobs(n=6):
    return [(i, f"S{i}") for i in range(n)]


def test_fresh_run_saves_every_result_and_resume_computes_only_the_rest():
    ck = os.path.join(tempfile.mkdtemp(), "ck")
    CALLS.clear()
    out = run_jobs(_sq, _jobs(), 1, "t", ckpt=ck)
    assert [r["v"] for r in out] == [0, 1, 4, 9, 16, 25] and len(os.listdir(ck)) == 6
    keep = {f for f in os.listdir(ck)}
    for f in sorted(os.listdir(ck))[3:]:
        os.remove(os.path.join(ck, f))
    CALLS.clear()
    out2 = run_jobs(_sq, _jobs(), 1, "t", ckpt=ck, resume=True)
    assert out2 == out and len(CALLS) == 3 and set(os.listdir(ck)) == keep


def test_without_resume_the_checkpoint_is_emptied_and_everything_recomputed():
    ck = os.path.join(tempfile.mkdtemp(), "ck")
    run_jobs(_sq, _jobs(4), 1, "t", ckpt=ck)
    CALLS.clear()
    run_jobs(_sq, _jobs(4), 1, "t", ckpt=ck)
    assert len(CALLS) == 4


def test_a_changed_input_or_setting_invalidates_only_that_symbol(tmp_path):
    ck = str(tmp_path / "ck")
    files = []
    for i in range(3):
        p = tmp_path / f"S{i}.json"
        p.write_text("[]")
        files.append(str(p))
    jobs = [(files[i], f"S{i}") for i in range(3)]
    run_jobs(lambda j: {"sym": j[1]}, jobs, 1, "t", ckpt=ck, stamp=lambda j: file_stamp(j[0], "A"))
    Path(files[1]).write_text("[1,2,3]")
    seen = []
    run_jobs(lambda j: seen.append(j[1]) or {"sym": j[1]}, jobs, 1, "t", ckpt=ck, resume=True, stamp=lambda j: file_stamp(j[0], "A"))
    assert seen == ["S1"]
    seen.clear()
    run_jobs(lambda j: seen.append(j[1]) or {"sym": j[1]}, jobs, 1, "t", ckpt=ck, resume=True, stamp=lambda j: file_stamp(j[0], "B"))
    assert seen == ["S0", "S1", "S2"]


def test_a_half_written_or_unreadable_checkpoint_is_recomputed():
    ck = os.path.join(tempfile.mkdtemp(), "ck")
    run_jobs(_sq, _jobs(3), 1, "t", ckpt=ck)
    open(os.path.join(ck, sorted(os.listdir(ck))[0]), "wb").write(b"not a pickle")
    CALLS.clear()
    run_jobs(_sq, _jobs(3), 1, "t", ckpt=ck, resume=True)
    assert len(CALLS) == 1


def test_killed_run_keeps_what_finished():
    ck = os.path.join(tempfile.mkdtemp(), "ck")

    def boom(j):
        if j[0] == 3:
            raise KeyboardInterrupt
        return {"sym": j[1], "v": j[0]}
    try:
        run_jobs(boom, _jobs(6), 1, "t", ckpt=ck)
    except KeyboardInterrupt:
        pass
    assert len(os.listdir(ck)) == 3
    CALLS.clear()
    run_jobs(_sq, _jobs(6), 1, "t", ckpt=ck, resume=True)
    assert sorted(CALLS) == ["S3", "S4", "S5"]


def test_settings_stamp_ignores_output_paths_but_not_real_settings(tmp_path):
    import argparse
    a = argparse.Namespace(step=3, out="/a", jobs=2, checkpoint="/c", resume=True, dir=str(tmp_path))
    b = argparse.Namespace(step=3, out="/b", jobs=4, checkpoint="/d", resume=False, dir=str(tmp_path))
    c = argparse.Namespace(step=5, out="/a", jobs=2, checkpoint="/c", resume=True, dir=str(tmp_path))
    assert settings_stamp(a, str(tmp_path)) == settings_stamp(b, str(tmp_path)) != settings_stamp(c, str(tmp_path))
    json.loads(settings_stamp(a, str(tmp_path)))


def _double_job(j):
    return {"sym": j[1], "v": j[0] * 2}


def test_resume_in_a_pool_keeps_order(tmp_path, capsys):
    ck = str(tmp_path / "ck")
    jobs = _jobs(8)
    first = run_jobs(_double_job, jobs, 2, "t", ckpt=ck)
    for f in sorted(os.listdir(ck))[:4]:
        os.remove(os.path.join(ck, f))
    again = run_jobs(_double_job, jobs, 2, "t", ckpt=ck, resume=True)
    assert again == first and [r["sym"] for r in again] == [j[1] for j in jobs]
    assert "resuming: 4 already done, 4 to go" in capsys.readouterr().err


if __name__ == "__main__":
    import tempfile as _t
    n = 0
    for k, f in sorted(globals().items()):
        if not k.startswith("test_"):
            continue
        args = f.__code__.co_varnames[:f.__code__.co_argcount]
        if "capsys" in args:
            continue
        f(**({"tmp_path": Path(_t.mkdtemp())} if "tmp_path" in args else {}))
        n += 1
        print("ok", k)
    print(n, "tests passed (capsys ones run under pytest)")
