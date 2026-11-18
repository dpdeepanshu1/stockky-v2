"""round 617: scripts/check_event_sources.py also probes the feeds that FEEDS does not list (information only). The EXTRA rows never change the exit code
or the "switch on" line. Skips when the scripts folder is not in the image."""
from __future__ import annotations

import importlib.util
import os

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts", "check_event_sources.py")
pytestmark = pytest.mark.skipif(not os.path.exists(_P), reason="scripts/ not in this image")


def _mod():
    spec = importlib.util.spec_from_file_location("ces", _P)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _fake(m, verdicts):
    m._load_feeds = lambda: [("A", "http://a"), ("B", "http://b")]
    m.probe = lambda n, u, timeout=10.0: {"source": n, "url": u, "status": 200, "items": 1, "dated": 1, "newest_age_min": 1.0, "verdict": verdicts.get(n, "STALE"), "note": ""}


def test_extra_rows_are_listed_but_do_not_make_the_exit_code_ok(capsys):
    m = _mod()
    _fake(m, {"NDTVProfit-bloombergq": "OK", "Moneycontrol-latest": "OK", "CNBCTV18": "OK"})     # every extra OK, every FEEDS row STALE
    assert m.main([]) == 1
    out = capsys.readouterr().out
    assert "NDTVProfit-bloombergq" in out and "not in FEEDS" in out and "NONE reachable" in out


def test_feeds_ok_still_decides_the_exit_code_and_the_switch_line(capsys):
    m = _mod()
    _fake(m, {"A": "OK", "CNBCTV18": "OK"})
    assert m.main([]) == 0
    out = capsys.readouterr().out
    assert "switch on: A" in out and "CNBCTV18" not in out.split("switch on:")[1]


def test_no_extra_flag_probes_only_feeds(capsys):
    m = _mod()
    _fake(m, {"A": "OK"})
    m.main(["--no-extra"])
    assert "NDTVProfit-bloombergq" not in capsys.readouterr().out
