"""group442: scripts/control_snapshot.py. Plain asserts or pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control_snapshot as c  # noqa: E402

DOC = {"service": "svc", "persistent": True, "summary": {"flags": 3, "overrides": 1, "pending_restart": 1, "off_recommended": 1}, "flags": [
    {"key": "A", "title": "Gate A", "group": "G1", "effective": "true", "live": True, "source": "default", "recommended": "true", "matches_recommended": True},
    {"key": "B", "title": "Gate B", "group": "G1", "effective": "false", "live": True, "source": "override", "recommended": "true", "matches_recommended": False,
     "override": {"by": "admin", "at": "2026-10-19T10:00:00+00:00"}},
    {"key": "C", "title": "Mode C", "group": "G2", "effective": "log", "live": False, "source": "override", "pending_restart": True, "running": "off", "desired": "log",
     "matches_recommended": None, "override": {"by": "admin", "at": "2026-10-19T10:01:00+00:00"}},
]}


def test_render_marks_overrides_pending_and_off_recommended():
    t = c.render(DOC)
    assert "== svc ==  3 switches, 1 overridden, 1 pending restart, 1 differ from recommended" in t
    assert "Gate A" in t and "[A]" in t
    assert "!= RECOMMENDED (true)" in t and "OVERRIDE by admin 2026-10-19T10:00" in t
    assert "PENDING RESTART (running off, saved log)" in t and "restart" in t.split("Mode C")[1].split("\n")[0]


def test_long_titles_are_not_cut_at_44_characters():
    # group446: real catalog titles are up to 46 characters and the 44-character cut hid the end of the longest ones; widened to 60
    title = "Skip broker import while own BUY pending (minutes)"
    assert len(title) > 44
    doc = dict(DOC, flags=[dict(DOC["flags"][0], title=title)])
    assert title in c.render(doc)


def test_only_changes_hides_the_plain_switches():
    t = c.render(DOC, only_changes=True)
    assert "Gate A" not in t and "Gate B" in t and "Mode C" in t
    clean = dict(DOC, flags=[DOC["flags"][0]])
    assert "nothing to show: no overrides" in c.render(clean, only_changes=True)


def test_unwritable_state_dir_is_called_out():
    assert "NOT writable" in c.render(dict(DOC, persistent=False, state_dir="/data/control", store_error="PermissionError"))


def test_cli_offline_and_failure_exit_codes():
    d = Path(tempfile.mkdtemp())
    (d / "a.json").write_text(json.dumps(DOC))
    assert c.main(["--rt-file", str(d / "a.json"), "--ps-file", str(d / "a.json")]) == 0
    assert c.main(["--rt-file", str(d / "a.json"), "--ps-url", "http://127.0.0.1:1", "--token", "x"]) == 1
    assert c.main(["--rt-url", "http://127.0.0.1:1", "--ps-url", "http://127.0.0.1:1", "--token", "x"]) == 2


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
