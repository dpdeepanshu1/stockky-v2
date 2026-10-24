"""E98 P0 item 3: scripts/wiring_check.py against fake services (offline), plus the job / pack / Dockerfile wiring. Plain asserts."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import jobs as J  # noqa: E402
import wiring_check as W  # noqa: E402
from regime import profiles  # noqa: E402

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


class FakeService:
    """One trading service as the script sees it. Flip the attributes to break one link at a time."""

    def __init__(self, svc, mode="live", mult="1.0", drop=(), not_live=(), not_overlayable=(), active=None, report_mult=None, report_mode=None,
                 reject_stamp=False, no_clear=False, shadow_ok=True, pending=False):
        self.svc, self.mode, self.mult, self.drop, self.not_live, self.not_overlayable = svc, mode, mult, set(drop), set(not_live), set(not_overlayable)
        self.active, self.report_mult, self.report_mode = active, report_mult, report_mode
        self.reject_stamp, self.no_clear, self.shadow_ok, self.pending = reject_stamp, no_clear, shadow_ok, pending
        self.calls = []

    def flags(self):
        rows = [{"key": "MARKET_STATE_MODE", "effective": self.mode, "running": self.mode, "desired": "shadow" if self.pending else self.mode, "pending_restart": self.pending, "live": True, "overlayable": False},
                {"key": "MARKET_STATE_RISK_MULT", "effective": self.mult, "live": True, "overlayable": True}]
        for k, d in profiles.DIALS.items():
            if self.svc in d["services"] and k not in self.drop and k != "MARKET_STATE_RISK_MULT":
                rows.append({"key": k, "live": k not in self.not_live, "overlayable": k not in self.not_overlayable, "effective": "1"})
        if "MARKET_STATE_RISK_MULT" in self.drop:
            rows = [r for r in rows if r["key"] != "MARKET_STATE_RISK_MULT"]
        if self.svc == "position-stocks":
            rows.append({"key": "SHORT_MODE", "effective": "shadow", "live": True})
        return {"service": self.svc, "flags": rows}

    def __call__(self, method, url, body):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1]
        self.calls.append((method, path, body))
        if path == "/control/flags":
            return 200, self.flags()
        if path == "/control/overlay":
            if method == "GET":
                return 200, {"supported": True, "mode": self.mode, "active": self.active, "overlays": []}
            if method == "POST":
                if self.reject_stamp:
                    return 409, {"detail": "mode is off"}
                self.active = {"state": body["state"], "version": body["version"], "until": body["until"]}
                return 200, {"ok": True}
            if method == "DELETE":
                if not self.no_clear:
                    self.active = None
                return 200, {"ok": True}
        rep = {"market_state_mode": self.report_mode or self.mode, "market_state_risk_mult": float(self.report_mult if self.report_mult is not None else self.mult)}
        if path.startswith("/risk-config/") and self.svc == "real-trade":
            return (200, rep) if path.endswith("REAL") else (404, {"detail": "No risk config row"})
        if path == "/ledger" and self.svc == "position-stocks":
            return 200, rep
        if path == "/short/shadow" and self.svc == "position-stocks":
            return (200, {"rows": []}) if self.shadow_ok else (500, "boom")
        return 404, "no route"


def _run(svc_obj, name, expect="live"):
    rep = W.Report()
    W.check_service(rep, name, f"http://{name}:1", svc_obj, expect, NOW)
    return rep


def test_everything_wired_passes_both_services():
    for name in ("real-trade", "position-stocks"):
        f = FakeService(name)
        rep = _run(f, name)
        assert rep.first_fail is None, rep.text()
        assert "WIRING: PASS" in rep.text()
        assert f.active is None                                              # the test overlay is gone again
        posts = [c for c in f.calls if c[0] == "POST"]
        assert len(posts) == 1 and posts[0][2]["entries"] == {} and posts[0][2]["version"] == "wiring-check"     # stamp only, never a dial


def test_real_overlay_is_never_touched():
    f = FakeService("real-trade", active={"state": "stressed", "version": "c1-p1", "until": "x"})
    rep = _run(f, "real-trade")
    assert rep.first_fail is None and any("left untouched" in l for l in rep.lines)
    assert not [c for c in f.calls if c[0] in ("POST", "DELETE")], f.calls
    assert f.active["state"] == "stressed"


def test_each_broken_link_fails_with_its_own_line():
    cases = [
        (FakeService("real-trade", mode="shadow"), "MARKET_STATE_MODE is shadow, expected live"),
        (FakeService("real-trade", drop=["RISK_MAX_ENTRIES_PER_DAY"]), "RISK_MAX_ENTRIES_PER_DAY missing"),
        (FakeService("real-trade", not_live=["ENTRY_LAST_TIME_IST"]), "ENTRY_LAST_TIME_IST not live"),
        (FakeService("real-trade", not_overlayable=["MARKET_STATE_CONVICTION_ADD"]), "MARKET_STATE_CONVICTION_ADD not overlayable"),
        (FakeService("real-trade", pending=True), "restart pending"),
        (FakeService("real-trade", reject_stamp=True), "stamp-only POST /control/overlay answered 409"),
        (FakeService("real-trade", no_clear=True), "NOT cleared"),
        (FakeService("real-trade", mult="0.5", report_mult=1.0), "reports risk multiplier 1.0 but the switch reads 0.5"),
        (FakeService("real-trade", report_mode="shadow"), "says mode shadow"),
        (FakeService("position-stocks", drop=["MARKET_STATE_SHORT_MULT"]), "MARKET_STATE_SHORT_MULT missing"),
        (FakeService("position-stocks", shadow_ok=False), "GET /short/shadow answered 500"),
    ]
    for f, needle in cases:
        rep = _run(f, f.svc)
        assert rep.first_fail and needle in rep.first_fail, (needle, rep.text())
        assert rep.text().rstrip().endswith("WIRING: FAIL - " + rep.first_fail)


def test_expect_mode_any_only_reports():
    assert _run(FakeService("real-trade", mode="shadow"), "real-trade", "any").first_fail is None
    assert _run(FakeService("real-trade", mode="shadow"), "real-trade", "shadow").first_fail is None


def test_unreachable_service_fails_cleanly():
    rep = W.Report()
    W.check_service(rep, "real-trade", "http://x:1", lambda m, u, b: (0, "URLError: refused"), "live", NOW)
    assert rep.first_fail and "no response" in rep.first_fail


def test_script_covers_every_dial_in_profiles():
    # the check reads profiles.DIALS itself, so a new dial can never be forgotten; both services must have at least one dial
    for name in ("real-trade", "position-stocks"):
        assert [k for k, d in profiles.DIALS.items() if name in d["services"]]


def test_job_pack_and_image_wiring():
    j = J.JOBS["wiring_check"]
    steps = j["builder"](J._ctx_for_test() if hasattr(J, "_ctx_for_test") else {"python": "py", "scripts": "/app/scripts", "rt": "http://rt", "ps": "http://ps"}, {})
    assert steps[0]["argv"][1].endswith("wiring_check.py") and "--rt-url" in steps[0]["argv"] and "--ps-url" in steps[0]["argv"]
    assert steps[0]["env"] == {"TOKEN": "{token}"}
    assert j["group"] == "ops" and not j["heavy"] and j["window"] == "any"
    assert ["wiring_check", {}] in J.PACK_MAP["monday_open"]["jobs"]
    assert (REPO / "scripts" / "wiring_check.py").exists()


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok", n)
