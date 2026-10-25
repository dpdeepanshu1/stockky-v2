"""E139: swing_universe.py lists every symbol of the replay folder (no silent alphabetical cut at 450). Runs under pytest or as a plain script."""
import io
import contextlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_universe as su  # noqa: E402


def _folder(n):
    d = Path(tempfile.mkdtemp())
    for i in range(n):
        (d / f"SYM{i:04d}.json").write_text("[]")
    (d / "^NSEI.json").write_text("[]")
    return d


def _run(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = su.main(args)
    return rc, out.getvalue(), err.getvalue()


def test_default_keeps_every_symbol_beyond_450():
    d = _folder(744)
    rc, out, err = _run([str(d)])
    line = out.splitlines()[0]
    assert rc == 0 and len(line.split("=", 1)[1].split(",")) == 744
    assert "SYM0743" in line and "^NSEI" not in line
    assert "SWING_UNIVERSE_MAX=744" in out.splitlines()
    assert "TRUNCATED" not in err


def test_explicit_max_warns_that_it_truncated():
    d = _folder(60)
    rc, out, err = _run([str(d), "--max", "20"])
    assert len(out.splitlines()[0].split("=", 1)[1].split(",")) == 20
    assert "SWING_UNIVERSE_MAX=20" in out.splitlines() and "TRUNCATED" in err


def test_empty_folder_exits_1():
    rc, out, err = _run([str(Path(tempfile.mkdtemp()))])
    assert rc == 1 and "NO SYMBOLS" in err


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
