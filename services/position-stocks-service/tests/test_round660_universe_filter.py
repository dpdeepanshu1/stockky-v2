"""Round 660 (T4): the subscription is trimmed to names that can pass the liquidity floor; recording, trimming, fail-open rules and wiring."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from feed import universe_filter as uf

IST = uf._IST
HERE = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _cfg(tmp_path, monkeypatch):
    conf = {"UNIVERSE_TRIM_ENABLED": True, "UNIVERSE_MIN_TURNOVER_CR": 1.0, "UNIVERSE_MIN_DAYS": 1, "UNIVERSE_MIN_KEEP": 3,
            "UNIVERSE_FILE": str(tmp_path / "u.json"), "BAR_RECORD_DIR": str(tmp_path)}
    monkeypatch.setattr(uf, "_cfg", lambda name, default=None: conf.get(name, default))
    uf.reset_for_tests()
    yield conf
    uf.reset_for_tests()


def write(tmp_path, sessions):
    (tmp_path / "u.json").write_text(json.dumps({"v": 1, "sessions": sessions}))
    uf._cache.update(path=None, mtime=None, data=None)


MAP = {"AAA": "1", "BBB": "2", "CCC": "3", "DDD": "4", "NEW": "5"}
TODAY = "2026-11-23"


def test_thin_names_are_dropped_known_liquid_and_unknown_stay(tmp_path):
    write(tmp_path, {"2026-11-20": {"AAA": 12.0, "BBB": 0.2, "CCC": 1.0, "DDD": 0.99}})
    kept, n = uf.trim(MAP, today=TODAY)
    assert set(kept) == {"AAA", "CCC", "NEW"} and n == 2        # CCC is exactly at the floor; NEW has no history


def test_best_session_decides_so_one_busy_day_keeps_a_name(tmp_path):
    write(tmp_path, {"2026-11-18": {"BBB": 0.3, "DDD": 0.1}, "2026-11-19": {"BBB": 4.0, "DDD": 0.2}, "2026-11-20": {"BBB": 0.2, "DDD": 0.1, "AAA": 9}})
    kept, _ = uf.trim(MAP, today=TODAY)
    assert "BBB" in kept and "DDD" not in kept


def test_min_days_needs_enough_history(tmp_path, _cfg):
    _cfg["UNIVERSE_MIN_DAYS"] = 2
    write(tmp_path, {"2026-11-20": {"AAA": 9, "BBB": 0.1, "CCC": 9, "DDD": 9}})
    assert uf.trim(MAP, today=TODAY) == (MAP, 0)                 # one session is not enough
    write(tmp_path, {"2026-11-19": {"BBB": 0.1, "AAA": 9, "CCC": 9, "DDD": 9}, "2026-11-20": {"BBB": 0.1, "AAA": 9, "CCC": 9, "DDD": 9}})
    kept, n = uf.trim(MAP, today=TODAY)
    assert "BBB" not in kept and n == 1


def test_protected_names_are_kept(tmp_path):
    write(tmp_path, {"2026-11-20": {"AAA": 12.0, "BBB": 0.2, "CCC": 5, "DDD": 5}})
    kept, n = uf.trim(MAP, keep=["BBB"], today=TODAY)
    assert "BBB" in kept and n == 0


@pytest.mark.parametrize("case", ["missing", "empty", "stale", "garbage", "off", "too_few_left"])
def test_fails_open(tmp_path, _cfg, case):
    if case == "empty":
        write(tmp_path, {})
    elif case == "stale":
        write(tmp_path, {"2026-10-01": {"AAA": 9, "BBB": 0.1}})
    elif case == "garbage":
        (tmp_path / "u.json").write_text("{not json")
    elif case == "off":
        _cfg["UNIVERSE_TRIM_ENABLED"] = False
        write(tmp_path, {"2026-11-20": {"AAA": 9, "BBB": 0.1}})
    elif case == "too_few_left":
        _cfg["UNIVERSE_MIN_KEEP"] = 400
        write(tmp_path, {"2026-11-20": {"AAA": 9, "BBB": 0.1, "CCC": 0.1, "DDD": 0.1}})
    out, n = uf.trim(MAP, today=TODAY)
    assert out == MAP and n == 0


def test_input_map_is_not_modified(tmp_path):
    write(tmp_path, {"2026-11-20": {"AAA": 9, "BBB": 0.1, "CCC": 9, "DDD": 9}})
    snap = dict(MAP)
    uf.trim(MAP, today=TODAY)
    assert MAP == snap


# ── recording ────────────────────────────────────────────────────────────────────────────────────────────────────────
def ts(h, m, day=20):
    return datetime(2026, 11, day, h, m, tzinfo=IST).timestamp()


VOLS = {f"S{i}": 1_000_000 + i for i in range(150)}


def test_records_only_from_1520_on_a_weekday(tmp_path):
    px = lambda s: 100.0
    assert uf.maybe_record(VOLS, px, now=ts(12, 0)) is False
    assert uf.maybe_record(VOLS, px, now=ts(15, 19)) is False
    assert uf.maybe_record(VOLS, px, now=ts(15, 20, day=21)) is False        # Saturday
    assert not (tmp_path / "u.json").exists()
    assert uf.maybe_record(VOLS, px, now=ts(15, 21)) is True
    doc = json.loads((tmp_path / "u.json").read_text())
    assert list(doc["sessions"]) == ["2026-11-20"]
    assert doc["sessions"]["2026-11-20"]["S0"] == round(1_000_000 * 100.0 / 1e7, 3) == 10.0


def test_a_barely_ticking_feed_is_not_recorded_and_missing_prices_are_skipped(tmp_path):
    assert uf.maybe_record({f"S{i}": 5 for i in range(20)}, lambda s: 10.0, now=ts(15, 25)) is False
    vols = dict(VOLS)
    assert uf.maybe_record(vols, lambda s: None if s == "S1" else 50.0, now=ts(15, 25)) is True
    assert "S1" not in json.loads((tmp_path / "u.json").read_text())["sessions"]["2026-11-20"]       # absent, never zero


def test_only_the_last_twelve_sessions_are_kept(tmp_path):
    write(tmp_path, {f"2026-10-{d:02d}": {"AAA": 5} for d in range(1, 21)})
    uf.maybe_record(VOLS, lambda s: 10.0, now=ts(15, 30))
    assert len(json.loads((tmp_path / "u.json").read_text())["sessions"]) == uf.KEEP_SESSIONS


def test_record_then_trim_round_trip(tmp_path):
    vols = {f"S{i}": 1_000_000 for i in range(120)}
    vols.update({"BIG": 5_000_000, "THIN": 2_000})
    uf.maybe_record(vols, lambda s: 100.0, now=ts(15, 25))
    kept, n = uf.trim({"BIG": "1", "THIN": "2", "OTHER": "3", **{f"S{i}": str(i) for i in range(120)}}, today=TODAY)
    assert "BIG" in kept and "OTHER" in kept and "THIN" not in kept and n >= 1


def test_junk_never_raises():
    assert uf.trim(None) == (None, 0) and uf.trim({}) == ({}, 0)
    assert uf.maybe_record(None, None, now=ts(15, 30)) is False


# ── wiring and config (source level) ─────────────────────────────────────────────────────────────────────────────────
def test_wired_into_ws_md_poller_and_config():
    ws = (HERE / "feed" / "ws_client.py").read_text(encoding="utf-8")
    md = (HERE / "feed" / "md_poller.py").read_text(encoding="utf-8")
    cf = (HERE / "config.py").read_text(encoding="utf-8")
    assert "_uf.trim(symbol_map=symbol_token_map" in ws and "_uf_rec.maybe_record(_last_volume" in ws
    assert "_uf.trim(symbol_map=symbol_token_map" in md
    for k in ("UNIVERSE_TRIM_ENABLED", "UNIVERSE_MIN_TURNOVER_CR", "UNIVERSE_MIN_DAYS", "UNIVERSE_MIN_KEEP", "UNIVERSE_FILE"):
        assert re.search(rf"^{k} = ", cf, re.M)


def test_floor_matches_the_entry_liquidity_gate():
    cf = (HERE / "config.py").read_text(encoding="utf-8")
    day_floor = float(re.search(r'ENTRY_MIN_DAY_TURNOVER_CR = _get_float\("ENTRY_MIN_DAY_TURNOVER_CR", ([\d.]+)\)', cf).group(1))
    trim_floor = float(re.search(r'UNIVERSE_MIN_TURNOVER_CR = _get_float\("UNIVERSE_MIN_TURNOVER_CR", ([\d.]+)\)', cf).group(1))
    assert trim_floor <= day_floor     # never trims a name the entry gate could still accept on its day-turnover floor
