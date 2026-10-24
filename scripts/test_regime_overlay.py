"""Market Weather step 1: the same-day OVERLAY in services/*/control/kit.py (tighten-only, in memory, expiring) and its catalog entries.
Plain asserts (python3 scripts/test_regime_overlay.py) or pytest. Temp dirs and fake modules only; no service is started."""
from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
RT = ROOT / "services" / "real-trade-service"
PS = ROOT / "services" / "position-stocks-service"
NS = ROOT / "services" / "notification-scheduler-service"


def _kit():
    for m in [k for k in sys.modules if k == "control" or k.startswith("control.")]:
        del sys.modules[m]
    sys.path.insert(0, str(RT))
    try:
        return importlib.import_module("control.kit")
    finally:
        sys.path.remove(str(RT))


kit = _kit()
F = kit.Flag
KEYS = ("T_MODE", "T_RISK", "T_ADD", "T_CUT", "T_MAXN", "T_GATE", "T_PLAIN", "MARKET_STATE_MODE")   # E128: the mode switch test sets MARKET_STATE_MODE in os.environ; it leaked into the next test (1 pending restart)


def _setup(mode="live", d=None):
    for k in KEYS:
        os.environ.pop(k, None)
    kit.ACTIVE.clear()
    m = types.ModuleType("fake_ovl_cfg")
    m.MODE, m.RISK, m.ADD, m.CUT, m.MAXN, m.GATE, m.PLAIN = mode, 1.0, 0.0, "14:00", 6, True, 3
    sys.modules["fake_ovl_cfg"] = m
    flags = (
        F("MARKET_STATE_MODE", "mode", "g", "enum", "h", choices=("off", "shadow", "live"), targets=(("fake_ovl_cfg", "MODE"),), live=True, shipped=mode),
        F("T_RISK", "risk", "g", "float", "h", lo=0.1, hi=1.0, looser="higher", overlay=True, targets=(("fake_ovl_cfg", "RISK"),), live=True, shipped="1.0"),
        F("T_ADD", "add", "g", "float", "h", lo=0, hi=20, looser="lower", overlay=True, targets=(("fake_ovl_cfg", "ADD"),), live=True, shipped="0"),
        F("T_CUT", "cutoff", "g", "time", "h", looser="later", overlay=True, targets=(("fake_ovl_cfg", "CUT"),), live=True, shipped="14:00"),
        F("T_MAXN", "maxn", "g", "int", "h", lo=0, hi=50, looser="higher", zero_off=True, overlay=True, targets=(("fake_ovl_cfg", "MAXN"),), live=True, shipped="6"),
        F("T_GATE", "gate", "g", "bool", "h", looser="off", targets=(("fake_ovl_cfg", "GATE"),), live=True, shipped="true"),   # NOT overlay=True
        F("T_PLAIN", "plain", "g", "int", "h", lo=0, hi=9, looser="higher", overlay=True, targets=(("fake_ovl_cfg", "PLAIN"),), live=False, shipped="3"),
    )
    d = d or tempfile.mkdtemp(prefix="ovl_")
    # T_PLAIN is deliberately live=False + overlay=True (invalid); keep it out of the controller, it is only used by validate_flag tests
    return kit.Controller("svc", flags[:-1], kit.Store(d)), m, d


def _until(hours=5):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _raises(fn, status=None):
    try:
        fn()
    except kit.ControlError as e:
        assert status is None or e.status == status, (e.status, e.detail)
        return e
    raise AssertionError("expected ControlError")


def test_tighten_is_accepted_and_applied_live():
    c, m, _ = _setup()
    r = c.overlay_set({"T_RISK": 0.5, "T_ADD": 5, "T_CUT": "13:00", "T_MAXN": 4}, _until(), "stressed", "v1", "runner")
    assert r["ok"] and (m.RISK, m.ADD, m.CUT, m.MAXN) == (0.5, 5.0, "13:00", 4)
    assert all(a["change"] == "TIGHTENS" for a in r["applied"])


def test_loosen_is_refused_against_the_SAVED_value_and_nothing_changes():
    c, m, _ = _setup()
    for entries in ({"T_RISK": 1.0 + 0.0001}, {"T_ADD": -1}, {"T_CUT": "14:30"}, {"T_MAXN": 7}):
        try:
            c.overlay_set(entries, _until(), "calm", "v1", "runner")
        except kit.ControlError as e:                     # out-of-range values fail validation (400), in-range loosening fails the direction test (409)
            assert e.status in (400, 409)
        else:
            raise AssertionError(entries)
    e = _raises(lambda: c.overlay_set({"T_CUT": "14:30"}, _until(), "calm", "v1", "runner"), 409)
    assert "can only tighten" in str(e.detail)
    assert (m.RISK, m.ADD, m.CUT, m.MAXN) == (1.0, 0.0, "14:00", 6)


def test_a_person_can_loosen_the_saved_value_later_and_the_overlay_is_judged_against_it():
    c, m, _ = _setup()
    c.set("T_RISK", "0.8", "admin", confirm=True)                 # saved value is now 0.8
    _raises(lambda: c.overlay_set({"T_RISK": 0.9}, _until(), "calm", "v1", "runner"), 409)   # 0.9 loosens the saved 0.8
    c.overlay_set({"T_RISK": 0.4}, _until(), "panic", "v1", "runner")
    assert m.RISK == 0.4


def test_all_or_nothing_one_bad_entry_applies_none():
    c, m, _ = _setup()
    _raises(lambda: c.overlay_set({"T_RISK": 0.5, "T_CUT": "15:00"}, _until(), "stressed", "v1", "runner"), 409)
    assert (m.RISK, m.CUT) == (1.0, "14:00") and not c._overlays
    _raises(lambda: c.overlay_set({"T_RISK": 0.5, "NOPE": 1}, _until(), "stressed", "v1", "runner"), 404)
    assert m.RISK == 1.0


def test_only_overlay_marked_switches_can_be_overlaid_and_never_the_mode_switch():
    c, m, _ = _setup()
    _raises(lambda: c.overlay_set({"T_GATE": False}, _until(), "stressed", "v1", "runner"), 409)
    _raises(lambda: c.overlay_set({"MARKET_STATE_MODE": "off"}, _until(), "stressed", "v1", "runner"), 409)
    assert m.GATE is True and m.MODE == "live"


def test_nothing_is_written_to_overrides_or_environment():
    c, m, d = _setup()
    c.overlay_set({"T_RISK": 0.5, "T_CUT": "13:00"}, _until(), "stressed", "v1", "runner")
    assert not os.path.exists(os.path.join(d, "overrides.json")), "an overlay must never be saved"
    assert "T_RISK" not in os.environ and "T_CUT" not in os.environ
    assert c.store.overrides() == {}
    assert any(r.get("action") == "overlay" for r in c.store.read_audit(10))      # but it IS audited


def test_expiry_restores_the_saved_value_and_the_state_stamp():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.5}, _until(2), "stressed", "v7", "runner")
    assert kit.current_profile() == {"market_state": "stressed", "profile_version": "v7"}
    assert c.reap() == [] and m.RISK == 0.5                                      # not yet
    assert c.reap(now=datetime.now(timezone.utc).timestamp() + 3 * 3600) == ["T_RISK"]
    assert m.RISK == 1.0 and not c._overlays
    assert kit.ACTIVE == {} and kit.current_profile() == {"market_state": None, "profile_version": None}


def test_a_persons_set_cancels_the_overlay_and_wins():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.5}, _until(), "stressed", "v1", "runner")
    r = c.set("T_RISK", "0.7", "admin")                                          # 0.7 is tighter than the saved 1.0
    assert r["change"] == "TIGHTENS" and float(r["from"]) == 1.0 and m.RISK == 0.7 and "T_RISK" not in c._overlays
    c.overlay_set({"T_RISK": 0.3}, _until(), "panic", "v2", "runner")
    c.set("T_RISK", "0.7", "admin")                                              # same as the saved value: still cancels the overlay
    assert m.RISK == 0.7 and "T_RISK" not in c._overlays
    c.overlay_set({"T_CUT": "13:00"}, _until(), "stressed", "v1", "runner")
    c.clear("T_CUT", "admin")
    assert m.CUT == "14:00" and "T_CUT" not in c._overlays


def test_restart_returns_to_saved_values():
    c, m, d = _setup()
    c.set("T_MAXN", "5", "admin")                                                # a saved override of 5
    c.overlay_set({"T_MAXN": 2, "T_RISK": 0.5}, _until(), "panic", "v1", "runner")
    assert m.MAXN == 2
    for k in ("T_MAXN", "T_RISK"):
        os.environ.pop(k, None) if k != "T_MAXN" else None                       # a restart: environment comes only from boot_apply()
    os.environ.pop("T_MAXN", None)
    flags = tuple(c.catalog)
    applied = kit.boot_apply(flags, d)
    assert applied == {"T_MAXN": "5"}, applied                                   # only the saved override is replayed; the overlay is gone
    c2 = kit.Controller("svc", flags, kit.Store(d))
    assert not c2._overlays and float(c2._saved(c2.by_key["T_RISK"])) == 1.0


def test_mode_gate_live_only_shadow_may_stamp_off_refuses_everything():
    c, m, _ = _setup(mode="shadow")
    _raises(lambda: c.overlay_set({"T_RISK": 0.5}, _until(), "stressed", "v1", "runner"), 409)
    assert m.RISK == 1.0
    r = c.overlay_set({}, _until(), "stressed", "v1", "runner")                  # stamp only: no dial moves
    assert r["ok"] and r["applied"] == [] and kit.current_profile()["market_state"] == "stressed" and m.RISK == 1.0
    c, m, _ = _setup(mode="off")
    _raises(lambda: c.overlay_set({}, _until(), "stressed", "v1", "runner"), 409)
    assert kit.current_profile()["market_state"] is None


def test_service_without_the_mode_switch_refuses():
    f = F("T_RISK", "risk", "g", "float", "h", lo=0.1, hi=1.0, looser="higher", overlay=True, targets=(("fake_ovl_cfg", "RISK"),), live=True, shipped="1.0")
    _setup()
    c = kit.Controller("svc", (f,), kit.Store(tempfile.mkdtemp(prefix="ovl_")))
    _raises(lambda: c.overlay_set({}, _until(), "normal", "v1", "runner"), 409)


def test_until_state_and_version_are_validated():
    c, m, _ = _setup()
    bad = [(datetime.now().replace(tzinfo=None).isoformat(), "stressed", "v1"),                      # no UTC offset
           (_until(-1), "stressed", "v1"),                                                            # in the past
           (_until(25), "stressed", "v1"),                                                            # more than 24 h ahead
           ("garbage", "stressed", "v1"),
           (_until(), "sunny", "v1"),                                                                 # unknown state
           (_until(), "stressed", "bad version!"), (_until(), "stressed", ""), (_until(), "stressed", "x" * 25)]
    for u, st, v in bad:
        _raises(lambda: c.overlay_set({"T_RISK": 0.5}, u, st, v, "runner"), 400)
    _raises(lambda: c.overlay_set(["T_RISK"], _until(), "stressed", "v1", "runner"), 400)
    assert m.RISK == 1.0


def test_back_to_the_saved_value_is_allowed_and_drops_that_overlay():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.4}, _until(), "panic", "v1", "runner")
    c.overlay_set({"T_RISK": 0.8}, _until(), "stressed", "v2", "runner")         # easing, still tighter than saved
    assert m.RISK == 0.8 and c._overlays["T_RISK"]["state"] == "stressed"
    r = c.overlay_set({"T_RISK": 1.0}, _until(), "normal", "v3", "runner")       # back to saved
    assert r["applied"][0]["change"] == "SAME" and m.RISK == 1.0 and "T_RISK" not in c._overlays
    assert kit.current_profile()["market_state"] == "normal"


def test_switching_the_mode_off_live_clears_every_overlay_at_once():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.5, "T_ADD": 5}, _until(), "stressed", "v1", "runner")
    c.set("MARKET_STATE_MODE", "shadow", "admin")                                # live -> shadow TIGHTENS: no confirm needed
    assert (m.RISK, m.ADD) == (1.0, 0.0) and not c._overlays
    assert kit.current_profile()["market_state"] == "stressed"                   # shadow keeps stamping
    c.set("MARKET_STATE_MODE", "off", "admin")
    assert kit.current_profile()["market_state"] is None
    e = _raises(lambda: c.set("MARKET_STATE_MODE", "live", "admin"), 409)        # going live asks for confirmation
    assert e.detail["needs_confirm"]


def test_describe_shows_the_overlay_and_does_not_call_it_pending_restart():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.5}, _until(), "stressed", "v1", "runner")
    d = c.describe()
    row = [r for r in d["flags"] if r["key"] == "T_RISK"][0]
    assert float(row["overlay"]["value"]) == 0.5 and float(row["overlay"]["saved"]) == 1.0 and row["overlayable"] is True
    assert row["pending_restart"] is False and float(row["desired"]) == 1.0 and float(row["running"]) == 0.5
    assert d["summary"]["pending_restart"] == 0
    assert d["market_state"]["active"]["state"] == "stressed" and d["market_state"]["mode"] == "live"


def test_overlay_clear_restores_everything_and_is_idempotent():
    c, m, _ = _setup()
    c.overlay_set({"T_RISK": 0.5, "T_CUT": "12:00"}, _until(), "panic", "v1", "runner")
    r = c.overlay_clear("admin", "test")
    assert r["cleared"] == ["T_CUT", "T_RISK"] and (m.RISK, m.CUT) == (1.0, "14:00") and kit.ACTIVE == {}
    assert c.overlay_clear("admin")["cleared"] == []


def test_validate_flag_rules_for_overlay():
    ok = F("X", "x", "g", "float", "h", lo=0, hi=1, looser="higher", overlay=True, targets=(("m", "A"),), live=True, shipped="1")
    assert kit.validate_flag(ok) == []
    assert kit.validate_flag(F("X", "x", "g", "float", "h", lo=0, hi=1, looser="higher", overlay=True, shipped="1"))                     # not live
    assert kit.validate_flag(F("X", "x", "g", "float", "h", lo=0, hi=1, looser="neutral", overlay=True, targets=(("m", "A"),), live=True, shipped="1"))
    assert kit.validate_flag(F("MARKET_STATE_MODE", "x", "g", "enum", "h", choices=("off", "live"), overlay=True, targets=(("m", "A"),), live=True, shipped="off"))


def test_kit_is_byte_identical_in_every_service():
    h = {s.name: hashlib.sha256((s / "control" / "kit.py").read_bytes()).hexdigest() for s in (RT, PS, NS)}
    assert len(set(h.values())) == 1, h


def test_real_catalogs_declare_the_market_state_switches_correctly():
    sys.path.insert(0, str(SCRIPTS))
    import check_control_catalog as chk
    for svc in chk.SERVICES:
        cat, k = chk._load_catalog(ROOT / "services" / svc)
        by = {f.key: f for f in cat.CATALOG}
        assert all(k.validate_flag(f) == [] for f in cat.CATALOG), svc
        mode, mult = by["MARKET_STATE_MODE"], by["MARKET_STATE_RISK_MULT"]
        assert mode.shipped == "shadow" and not mode.overlay and mode.live
        assert mult.shipped == "1.0" and mult.overlay and mult.looser == "higher" and (mult.lo, mult.hi) == (0.1, 1.0)
        over = {f.key for f in cat.CATALOG if f.overlay}
        # a gate / safety switch must never be an overlay dial
        assert not any(x in over for x in ("STOPOUT_DAY_PAUSE_N", "ACCOUNT_MAX_DAILY_LOSS_PCT", "DEFAULT_RISK_PER_TRADE_PCT")), over
        assert not any(x.startswith(("STRUCTURE_", "PS_STRUCTURE_")) for x in over), over
    rt = {f.key for f in chk._load_catalog(RT)[0].CATALOG if f.overlay}
    assert {"MARKET_STATE_RISK_MULT", "MARKET_STATE_CONVICTION_ADD", "RISK_MAX_ENTRIES_PER_DAY", "ENTRY_LAST_TIME_IST", "OPENING_ENTRY_NOT_BEFORE_IST"} == rt, rt
    ps = {f.key for f in chk._load_catalog(PS)[0].CATALOG if f.overlay}
    assert {"MARKET_STATE_RISK_MULT", "MAX_CONCURRENT_SCALP_POSITIONS", "SCALP_ENTRY_CUTOFF_IST", "MARKET_STATE_SHORT_MULT"} == ps, ps   # the short dial exists in position-stocks only (step 6)


if __name__ == "__main__":
    sys.path.insert(0, str(SCRIPTS))
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
