"""E112: the Market Weather card's saved / today / expiry view and the revert button (weather_overlay.py). Offline: the control services are fakes."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import weather_overlay as WO  # noqa: E402
from regime import profiles  # noqa: E402

KEYS = WO.DEFAULT_KEYS


class FakeControl:
    def __init__(self, down=(), overlays=None, mode="live"):
        self.down, self.mode = set(down), mode
        self.overlays = overlays or {}          # service key -> {flag: {value, saved, until, state}}
        self.cleared = []

    def flags(self, key):
        if key in self.down:
            raise OSError("connection refused")
        rows = {"MARKET_STATE_MODE": {"desired": self.mode}}
        for k in profiles.DIALS:
            rows[k] = {"desired": "1", "overlay": (self.overlays.get(key) or {}).get(k)}
        return rows

    def overlay_clear(self, key):
        if key in self.down:
            raise OSError("connection refused")
        gone = sorted(self.overlays.pop(key, {}))
        self.cleared.append(key)
        return {"ok": True, "cleared": gone}


def test_each_service_lists_only_its_own_dials():
    v = WO.overlay_view(FakeControl())["services"]
    rt = {d["key"] for d in v["real-trade"]["dials"]}
    ps = {d["key"] for d in v["position-stocks"]["dials"]}
    assert "RISK_MAX_ENTRIES_PER_DAY" in rt and "RISK_MAX_ENTRIES_PER_DAY" not in ps
    assert "MARKET_STATE_SHORT_MULT" in ps and "MARKET_STATE_SHORT_MULT" not in rt
    assert "MARKET_STATE_RISK_MULT" in rt and "MARKET_STATE_RISK_MULT" in ps
    assert v["real-trade"]["mode"] == "live" and v["real-trade"]["ok"] is True


def test_no_overlay_means_today_equals_saved():
    d = WO.overlay_view(FakeControl())["services"]["real-trade"]
    assert d["overlaid"] == 0
    row = next(x for x in d["dials"] if x["key"] == "MARKET_STATE_RISK_MULT")
    assert row["saved"] == "1" and row["today"] == "1" and row["until"] is None and row["overlaid"] is False


def test_overlay_shows_saved_today_and_expiry():
    ov = {"real-trade-service": {"MARKET_STATE_RISK_MULT": {"value": "0.6", "saved": "1", "until": "2026-10-26T15:30:00+05:30", "state": "stressed"}}}
    d = WO.overlay_view(FakeControl(overlays=ov))["services"]["real-trade"]
    row = next(x for x in d["dials"] if x["key"] == "MARKET_STATE_RISK_MULT")
    assert (row["saved"], row["today"], row["state"]) == ("1", "0.6", "stressed") and row["until"].endswith("15:30:00+05:30") and row["overlaid"] is True
    assert d["overlaid"] == 1
    other = WO.overlay_view(FakeControl(overlays=ov))["services"]["position-stocks"]
    assert other["overlaid"] == 0                                                   # the other service is untouched


def test_a_service_that_is_down_is_reported_not_raised():
    v = WO.overlay_view(FakeControl(down=("position-stocks-service",)))["services"]
    assert v["position-stocks"]["ok"] is False and "OSError" in v["position-stocks"]["why"] and v["position-stocks"]["dials"] == []
    assert v["real-trade"]["ok"] is True                                            # the other one still shows


def test_revert_clears_only_the_named_service_and_returns_its_fresh_view():
    ov = {"real-trade-service": {"MARKET_STATE_RISK_MULT": {"value": "0.6", "saved": "1", "until": "u", "state": "stressed"}},
          "position-stocks-service": {"MARKET_STATE_RISK_MULT": {"value": "0.6", "saved": "1", "until": "u", "state": "stressed"}}}
    c = FakeControl(overlays=ov)
    r = WO.revert(c, "real-trade")
    assert r["ok"] is True and r["cleared"] == ["MARKET_STATE_RISK_MULT"] and c.cleared == ["real-trade-service"]
    assert r["view"]["overlaid"] == 0
    assert WO.overlay_view(c)["services"]["position-stocks"]["overlaid"] == 1       # the other service keeps its overlay


def test_revert_of_position_stocks_clears_position_stocks_not_real_trade():
    ov = {"real-trade-service": {"MARKET_STATE_RISK_MULT": {"value": "0.6", "saved": "1", "until": "u", "state": "stressed"}},
          "position-stocks-service": {"MARKET_STATE_RISK_MULT": {"value": "0.6", "saved": "1", "until": "u", "state": "stressed"}}}
    c = FakeControl(overlays=ov)
    r = WO.revert(c, "position-stocks")
    assert c.cleared == ["position-stocks-service"] and r["view"]["overlaid"] == 0
    assert WO.overlay_view(c)["services"]["real-trade"]["overlaid"] == 1


def test_revert_failure_is_reported_and_unknown_service_is_refused():
    r = WO.revert(FakeControl(down=("real-trade-service",)), "real-trade")
    assert r["ok"] is False and "OSError" in r["why"]
    try:
        WO.revert(FakeControl(), "../control/flags")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown service name must be refused")


def test_static_rails():
    src = (HERE / "weather_overlay.py").read_text(encoding="utf-8")
    assert ".set(" not in src and "build_overlay" not in src and "/control/flags" not in src and "overlay_set" not in src
    assert src.count("overlay_clear(") == 1                                          # one call site, inside revert()
    assert "weather_overlay.py" in (HERE / "Dockerfile").read_text(encoding="utf-8")
    main = (HERE / "main.py").read_text(encoding="utf-8")
    assert '"/weather/overlay/revert"' in main and '@app.get("/weather/overlay")' in main
    for name in ("weather.py", "weather_apply.py"):                                  # the two modules the older pins protect stay clear of the revert
        assert "overlay_clear" not in (HERE / name).read_text(encoding="utf-8")
    assert "def overlay_clear" in (HERE / "autocalib.py").read_text(encoding="utf-8")


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f(); print("ok  ", n)
            except Exception as e:  # noqa: BLE001
                fails += 1; print("FAIL", n, type(e).__name__, e)
    sys.exit(1 if fails else 0)
