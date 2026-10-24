"""E116: a default schedule slot added by a later release (s_tm_open, E115) reaches an existing schedule.json once; a slot the person deleted is never put back."""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jobs as J  # noqa: E402
from scheduler import Scheduler  # noqa: E402


def make(path):
    return Scheduler(str(path), J.SCHEDULE_DEFAULT, lambda *a, **k: None, list(J.PACK_MAP))


def ids(s):
    return {e["id"] for e in s.get()["entries"]}


def test_fresh_install_has_every_default_and_records_them(tmp_path):
    s = make(tmp_path / "state" / "schedule.json")
    assert "s_tm_open" in ids(s) and "s_tm_open" in s.get()["seen_defaults"]


def test_old_file_without_the_record_gets_only_the_new_slot_and_keeps_deletions(tmp_path):
    p = tmp_path / "schedule.json"
    old = [e for e in J.SCHEDULE_DEFAULT if e["id"] not in ("s_tm_open", "s_weekly_cal")]      # an E114 file where the person had deleted s_weekly_cal
    p.write_text(json.dumps({"enabled": True, "entries": old, "last_fired": {}}))
    s = make(p)
    assert "s_tm_open" in ids(s) and "s_weekly_cal" not in ids(s)
    assert next(e for e in s.get()["entries"] if e["id"] == "s_tm_open")["enabled"] is True
    assert json.loads(p.read_text())["seen_defaults"]                                          # saved, so it happens once


def test_a_slot_deleted_after_it_was_offered_stays_deleted(tmp_path):
    p = tmp_path / "schedule.json"
    s = make(p)
    s.put(None, [e for e in s.get()["entries"] if e["id"] != "s_tm_open"])
    s2 = make(p)
    assert "s_tm_open" not in ids(s2)


def test_a_damaged_file_still_falls_back_to_defaults(tmp_path):
    p = tmp_path / "schedule.json"
    p.write_text("{not json")
    assert "s_tm_open" in ids(make(p))
