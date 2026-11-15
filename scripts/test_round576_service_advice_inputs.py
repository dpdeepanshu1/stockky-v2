"""round 576: service_onoff_advice.py exits 2 (no report) when a named input cannot be read; a traceback used to exit 1 = the TURN OFF code.
Plain asserts or pytest (tmp_path)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_onoff_advice as s  # noqa: E402


def _run(*args):
    return s.main(list(args))


def test_missing_rt_file_exits_2_and_writes_no_report():
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "advice.txt"
        assert _run("--rt-file", str(Path(td) / "nope.json"), "--no-ps", "--out", str(out)) == 2
        assert not out.exists()


def test_corrupt_ps_file_exits_2():
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "ps.json"
        bad.write_text("not json", encoding="utf-8")
        assert _run("--ps-file", str(bad), "--no-rt") == 2


def test_missing_csv_exits_2():
    with tempfile.TemporaryDirectory() as td:
        assert _run("--from-csv", str(Path(td) / "nope.csv")) == 2


def test_one_good_one_missing_file_is_still_refused():
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "rt.json"
        good.write_text(json.dumps({"records": []}), encoding="utf-8")
        assert _run("--rt-file", str(good), "--ps-file", str(Path(td) / "nope.json"), "--no-rt") == 2


def test_bad_as_of_exits_2_instead_of_using_now():
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "rt.json"
        good.write_text(json.dumps({"records": []}), encoding="utf-8")
        assert _run("--rt-file", str(good), "--no-ps", "--as-of", "yesterday-ish") == 2


def test_readable_empty_input_is_still_exit_1_no_trades():
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "rt.json"
        good.write_text(json.dumps({"records": []}), encoding="utf-8")
        assert _run("--rt-file", str(good), "--no-ps") == 1


if __name__ == "__main__":
    n = 0
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v(); n += 1
    print(f"ok {n}")
