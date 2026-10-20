"""group471: 'Download all latest results' = one zip with the latest finished result of every test + an index. Offline, stdlib only."""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402
import runner as R  # noqa: E402

TOKEN = "tok-ABCDEF-1234567890"


def _runner():
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    ctx = dict(python=sys.executable, scripts=sc, bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
               decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t")
    r = R.Runner(os.path.join(d, "data"), sc, ctx, {}, {}, lambda: TOKEN, nice=0, preflight_s=0)
    r.start()
    return r, d, sc


def test_zip_latest_has_newest_result_per_test_and_an_index():
    r, d, sc = _runner()
    open(os.path.join(sc, "ok.py"), "w").write("print('hello from ok')\n")
    open(os.path.join(sc, "bad.py"), "w").write("raise SystemExit(3)\n")
    r.jobs = {"a": J._job("a", "A", "ops", "now", "t", lambda c, p: [J._py(c, "ok.py")]),
              "b": J._job("b", "B", "ops", "now", "t", lambda c, p: [J._py(c, "bad.py")]),
              "c": J._job("c", "C", "ops", "now", "t", lambda c, p: [J._py(c, "ok.py")])}
    r.submit("job", "a")
    assert r.drain(30)
    r.submit("job", "a")                 # a newer run of "a": only this one may be in the zip
    r.submit("job", "b")
    assert r.drain(30)
    newest_a = r.latest("a")["id"]
    dest = os.path.join(d, "all.zip")
    n = r.zip_latest(dest)
    assert n == 2                         # a and b ran, c never did
    names = zipfile.ZipFile(dest).namelist()
    assert "_INDEX.txt" in names and "a/output.txt" in names and "b/output.txt" in names
    assert not any(x.startswith("c/") for x in names)
    z = zipfile.ZipFile(dest)
    idx = z.read("_INDEX.txt").decode()
    assert "a: SUCCESS" in idx and newest_a in idx and "b: FAILED" in idx and "c: never run" in idx
    assert "hello from ok" in z.read("a/output.txt").decode()
    r.shutdown()


def test_zip_latest_with_nothing_run_still_gives_an_index():
    r, d, _ = _runner()
    dest = os.path.join(d, "none.zip")
    assert r.zip_latest(dest, ["x", "y"]) == 0
    assert zipfile.ZipFile(dest).namelist() == ["_INDEX.txt"]
    r.shutdown()
