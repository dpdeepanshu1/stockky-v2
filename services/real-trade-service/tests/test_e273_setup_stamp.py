"""E273 round 18: Real Trade stamps every entry with the rule that took it (flow_json["setup"]), record only.
Plain runner: python3 tests/test_e273_setup_stamp.py (service folder)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from entry_engine import setup_stamp as st  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m):
    return datetime(2026, 10, 26, h, m, tzinfo=IST)


def test_build_defaults_to_legacy_and_slots():
    s = st.build(trigger="HIGH_CONVICTION", confirmations=["orb_break"], penalties={"vix": 0.8}, size_mult=0.8, now=at(9, 21))
    assert s["id"] == "LEGACY_SHORTLIST" and s["slot"] == "0920" and s["side"] == "LONG" and s["penalties"] == {"vix": 0.8} and s["size_mult"] == 0.8
    assert st.time_slot(at(9, 45)) == "0930" and st.time_slot(at(10, 10)) == "1000" and st.time_slot(at(11, 0)) == "later" and st.time_slot(None) == "later"


def test_attach_keeps_other_keys_and_the_first_stamp_wins():
    base = json.dumps({"imbalance_5": 0.3, "orb": {"would_enter": True}})
    out = st.attach(base, st.build(trigger="A"))
    d = json.loads(out)
    assert d["imbalance_5"] == 0.3 and d["setup"]["trigger"] == "A"
    assert st.attach(out, st.build(trigger="B")) == out                  # the first reason stays
    assert st.read(out)["trigger"] == "A"
    assert st.attach(None, st.build())                                   # no flow_json yet is fine
    assert st.attach("not json", st.build()) is not None                 # garbage never raises


def test_confirmations_come_only_from_true_facts():
    fj = json.dumps({"orb": {"would_enter": True}, "imbalance_5": 0.4})
    tick = SimpleNamespace(price=101.0, vwap=100.5)
    assert st.confirmations_from(fj, tick) == ["orb_break", "book_bid_heavy", "above_vwap"]
    assert st.confirmations_from(json.dumps({"orb": {"would_enter": False}, "imbalance_5": -0.4}), SimpleNamespace(price=100.0, vwap=100.5)) == []
    assert st.confirmations_from(None, None) == [] and st.confirmations_from("junk", None) == []


def test_entry_wiring_is_record_only_and_after_the_penalties():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    i = src.index("setup_stamp.attach(")
    assert "# E273 round 18" in src[i - 1500:i]
    block = src[i - 1500:i + 700]
    assert "except Exception" in block and "never blocks" in block
    assert src.index("_og_pen[\"mult\"] < 1.0") < i < src.index("# ── Risk engine")      # after the size factors, before the risk engine
    assert "from tz_utils import ist_now" in src


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
