"""E284 round 83: the normal volume pace learned from recorded bars (orders/volume_profile.py). Fixed bars written to a temp folder, no network."""
import calendar
import json
import time

import config
from orders import liquidity_gate, volume_profile as vp


def _t0(date):
    y, m, d = (int(x) for x in date.split("-"))
    return calendar.timegm((y, m, d, 0, 0, 0)) - 19800 + 9 * 3600 + 15 * 60


def _write(root, date, sym, n=375, start=0, vol=lambda k: 1000.0):
    (root / date).mkdir(parents=True, exist_ok=True)
    t0 = _t0(date)
    bars = [[t0 + 60 * k, 100, 101, 99, 100, vol(k)] for k in range(start, start + n)]
    (root / date / f"{sym}.json").write_text(json.dumps({"v": 1, "symbol": sym, "date": date, "bars": bars, "backfilled": False}))


def _fill(root, days=5, syms=70, **kw):
    for d in range(days):
        for s in range(syms):
            _write(root, f"2026-10-{d + 1:02d}", f"S{s}", **kw)


def test_flat_volume_gives_a_straight_cumulative_curve(tmp_path):
    _fill(tmp_path)
    prof = vp.build(str(tmp_path))
    assert prof["days"] == 5 and prof["symbol_days"] == 350
    assert abs(prof["curve"][0] - 1 / 375) < 1e-9 and abs(prof["curve"][187] - 188 / 375) < 1e-9 and prof["curve"][374] == 1.0


def test_too_little_data_gives_no_curve(tmp_path):
    _fill(tmp_path, days=4)                                   # 4 days: under MIN_DAYS
    assert vp.build(str(tmp_path))["curve"] is None
    other = tmp_path / "b"
    _fill(other, days=5, syms=10)                             # 50 symbol-days: under MIN_SYMBOL_DAYS
    assert vp.build(str(other))["curve"] is None
    assert vp.build(str(tmp_path / "missing")) is None


def test_late_started_and_thin_files_are_not_complete_days(tmp_path):
    _fill(tmp_path, days=5, syms=70)
    _write(tmp_path, "2026-10-01", "LATE", n=300, start=60)   # starts at 10:15
    _write(tmp_path, "2026-10-01", "THIN", n=200)             # 200 of 375 minutes
    assert vp.build(str(tmp_path))["symbol_days"] == 350


def _ready(monkeypatch, curve, symbol_days=350):
    vp.reset()
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", True, raising=False)
    monkeypatch.setattr(vp, "_state", {"profile": {"curve": curve, "days": 6, "symbol_days": symbol_days}, "built_at": time.time(), "building": False, "error": None})


def test_adjust_is_shrunk_and_held_within_25_percent_of_the_old_curve(monkeypatch):
    old = (30 / 375) ** 0.6                                   # about 0.22 at 09:45
    _ready(monkeypatch, [0.08] * 375)                         # a far lower learned share
    assert abs(vp.adjust(old, 30) - old * 0.75) < 1e-9        # shrunk value would be ~0.144: the band floor binds
    _ready(monkeypatch, [0.9] * 375)
    assert abs(vp.adjust(old, 30) - old * 1.25) < 1e-9
    _ready(monkeypatch, [old * 1.1] * 375)
    assert old < vp.adjust(old, 30) < old * 1.1               # inside the band: a blend


def test_old_curve_when_off_not_ready_or_broken(monkeypatch):
    _ready(monkeypatch, [0.1] * 375)
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", False, raising=False)
    assert vp.adjust(0.22, 30) == 0.22
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", True, raising=False)
    _ready(monkeypatch, None)
    assert vp.adjust(0.22, 30) == 0.22
    monkeypatch.setattr(vp, "_profile", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert vp.adjust(0.22, 30) == 0.22 and vp.adjust(0.0, 30) == 0.0


def test_liquidity_gate_uses_the_learned_pace(monkeypatch):
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 11, 2, 9, 45, tzinfo=ist)
    vp.reset()
    monkeypatch.setattr(config, "VOLUME_PROFILE_LEARNED_ENABLED", False, raising=False)
    base = liquidity_gate.elapsed_fraction(now)
    assert abs(base - (30 / 375) ** 0.6) < 1e-6
    _ready(monkeypatch, [0.9] * 375)
    assert abs(liquidity_gate.elapsed_fraction(now) - base * 1.25) < 1e-6
    assert liquidity_gate.elapsed_fraction(datetime(2026, 11, 2, 9, 10, tzinfo=ist)) == 0.0


def test_switch_is_in_config_and_catalog():
    assert config.VOLUME_PROFILE_LEARNED_ENABLED is True
    src = open(__import__("os").path.join(__import__("os").path.dirname(__file__), "..", "control", "catalog.py")).read()
    assert "VOLUME_PROFILE_LEARNED_ENABLED" in src
