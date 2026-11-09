"""Round 269: a refused or dry-run probe must not hide the real round trip; the refusal reason and the contract are kept in the saved result; the file stamp is IST. No network, no money."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from fno import probecheck, realprobe
from fno.realprobe import IST
from test_round261 import FakeBroker, NOW, Prov, go
from test_round265_probecheck import make_probe


def put(d, name, body):
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(body, fh)


REFUSED = {"live": True, "underlying": "NIFTY", "side": "CE", "exit_code": 2, "orders": [], "reason": "REFUSED: outside the live window."}


def test_newest_file_that_sent_an_order_wins_over_a_later_refusal_and_dry_run(tmp_path):
    d = str(tmp_path)
    put(d, "probe_20261111_101500.json", make_probe())
    put(d, "probe_20261111_153000.json", REFUSED)
    put(d, "probe_20261112_093500.json", {"live": False, "underlying": "NIFTY", "side": "CE", "exit_code": 0, "orders": []})
    name, probe = probecheck.load(d)
    assert name == "probe_20261111_101500.json" and probecheck.attempted(probe)
    assert probecheck.list_files(d)[0] == "probe_20261112_093500.json"


def test_with_no_order_ever_sent_the_newest_refusal_is_shown_with_its_reason(tmp_path):
    d = str(tmp_path)
    put(d, "probe_20261111_153000.json", REFUSED)
    name, probe = probecheck.load(d)
    assert name == "probe_20261111_153000.json"
    s = probecheck.summarize(probe, name)
    assert "refused" in s["outcome"] and "live window" in s["reason"]


def test_a_named_file_is_returned_even_when_it_is_a_refusal(tmp_path):
    d = str(tmp_path)
    put(d, "probe_20261111_101500.json", make_probe())
    put(d, "probe_20261111_153000.json", REFUSED)
    assert probecheck.load(d, "probe_20261111_153000.json")[0] == "probe_20261111_153000.json"


def test_an_unreadable_newest_file_is_reported_but_an_unreadable_older_one_is_skipped(tmp_path):
    d = str(tmp_path)
    put(d, "probe_20261111_101500.json", make_probe())
    with open(os.path.join(d, "probe_20261111_120000.json"), "w") as fh:
        fh.write("{not json")
    put(d, "probe_20261111_153000.json", REFUSED)
    assert probecheck.load(d)[0] == "probe_20261111_101500.json"
    with open(os.path.join(d, "probe_20261112_093500.json"), "w") as fh:
        fh.write("{not json")
    assert probecheck.load(d) == (None, None)


def test_a_refusal_saves_its_reason_and_a_later_refusal_still_names_the_contract():
    b = FakeBroker([])
    r = go(b, now=datetime(2026, 11, 9, 17, 58, tzinfo=IST))
    assert r["exit_code"] == 2 and b.placed == []
    assert "live window" in r["reason"] and r["contract"].startswith("NIFTY") and r["security_id"] == 777 and r["lot_size"] == 65
    s = probecheck.summarize(r, "probe_20261109_175800.json")
    assert s["contract"] and "live window" in s["reason"]


def test_a_good_round_trip_has_no_reason():
    r = go(FakeBroker(["fill", "fill"]))
    assert r["exit_code"] == 0 and not r.get("reason")


def test_file_stamp_is_ist_whatever_the_host_clock_zone():
    utc = datetime(2026, 11, 9, 12, 28, 5, tzinfo=timezone.utc)
    assert realprobe.file_stamp(utc) == "20261109_175805"
    assert realprobe.file_stamp(datetime(2026, 11, 9, 17, 58, 5, tzinfo=IST)) == "20261109_175805"
