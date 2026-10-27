"""E232: scan-hit recorder, day-mover path (long + short) and the why-not-picked explainer."""
import time

import config
from feed import ws_client
from screening import engine, scan_hits, scan_why


def _feed(monkeypatch, series, day_stats=None, volume=1_000_000):
    now = time.time()
    bufs = {s: [(now - age, px) for age, px in pts] for s, pts in series.items()}
    monkeypatch.setattr(ws_client, "_token_to_symbol", {str(i): s for i, s in enumerate(series)})
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: list(bufs.get(s, [])))
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: volume)
    monkeypatch.setattr(ws_client, "get_best_bid_ask", lambda s: None)
    monkeypatch.setattr(ws_client, "get_day_range", lambda s: None)
    monkeypatch.setattr(ws_client, "get_day_stats", lambda s: (day_stats or {}).get(s))


def _flat(px=100.0):
    return [(4200 - i * 60, px) for i in range(0, 71)]      # an hour+ of ticks at one price: no rolling window can clear


def test_a_steady_all_day_climber_is_listed_as_a_day_mover(monkeypatch):
    _feed(monkeypatch, {"CLIMB": _flat(106.0)}, {"CLIMB": (100.0, 106.0, 99.0, 100.0)})
    out = engine.scan()
    assert [c.symbol for c in out] == ["CLIMB"]
    c = out[0]
    assert c.window_label == "day" and c.window_minutes == engine.DAY_WINDOW_MINUTES
    assert 5.9 < c.pct_change < 6.1 and c.composite_score > 0


def test_day_mover_off_restores_the_old_scan(monkeypatch):
    monkeypatch.setattr(config, "DAY_MOVER_ENABLED", False)
    _feed(monkeypatch, {"CLIMB": _flat(106.0)}, {"CLIMB": (100.0, 106.0, 99.0, 100.0)})
    assert engine.scan() == []


def test_day_move_below_the_minimum_or_above_the_cap_is_not_listed(monkeypatch):
    _feed(monkeypatch, {"SMALL": _flat(102.0), "HUGE": _flat(130.0)},
          {"SMALL": (100.0, 102.0, 99.0, 100.0), "HUGE": (100.0, 130.0, 99.0, 100.0)})
    assert engine.scan() == []


def test_day_mover_still_obeys_the_volume_floor(monkeypatch):
    _feed(monkeypatch, {"CLIMB": _flat(106.0)}, {"CLIMB": (100.0, 106.0, 99.0, 100.0)}, volume=10)
    assert engine.scan() == []


def test_unknown_previous_close_is_never_guessed(monkeypatch):
    _feed(monkeypatch, {"CLIMB": _flat(106.0)}, {"CLIMB": (100.0, 106.0, 99.0, None)})
    assert engine.scan() == []


def test_a_rolling_window_hit_is_not_duplicated_by_the_day_mover(monkeypatch):
    pts = [(4200 - i * 60, 100.0) for i in range(0, 64)] + [(360 - i * 60, 100.0 + 6.0 * i / 6.0) for i in range(0, 7)]
    _feed(monkeypatch, {"RUN": pts}, {"RUN": (100.0, 106.0, 99.0, 100.0)})
    out = engine.scan()
    assert out and all(c.window_label != "day" for c in out)


def test_a_steady_all_day_faller_is_a_day_mover_short(monkeypatch):
    _feed(monkeypatch, {"SLIDE": _flat(94.0)}, {"SLIDE": (100.0, 100.0, 93.0, 100.0)})
    out = engine.scan_shorts()
    assert [c.symbol for c in out] == ["SLIDE"] and out[0].window_label == "day" and out[0].pct_change < -5.9
    monkeypatch.setattr(config, "DAY_MOVER_SHORT_ENABLED", False)
    assert engine.scan_shorts() == []


class _C:
    def __init__(self, symbol, pct=1.2, label="5m", score=1.0):
        self.symbol, self.pct_change, self.window_label, self.composite_score, self.current_ltp = symbol, pct, label, score, 100.0


def test_scan_hits_keeps_names_that_never_reached_the_quality_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCAN_HITS_DIR", str(tmp_path))
    scan_hits.reset()
    cands = [_C(f"S{i}") for i in range(8)]
    assert scan_hits.record_scan(cands) == 8
    scan_hits.mark_checked([c.symbol for c in cands[:5]])
    never = scan_hits.snapshot(never_checked_only=True)["rows"]
    assert sorted(r["symbol"] for r in never) == ["S5", "S6", "S7"]
    assert scan_hits.get("S6")["best_rank"] == 7 and scan_hits.get("S0")["checked_cycles"] == 1


def test_scan_hits_survive_a_restart_and_flag_an_unwritable_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCAN_HITS_DIR", str(tmp_path))
    scan_hits.reset()
    scan_hits.record_scan([_C("KEEP")])
    scan_hits.flush()
    scan_hits.reset()                                   # a new process: nothing in memory
    assert scan_hits.get("KEEP")["scans"] == 1
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(config, "SCAN_HITS_DIR", str(blocker / "sub"))
    scan_hits.reset()
    scan_hits.record_scan([_C("X")])
    scan_hits.flush()
    assert scan_hits.stats()["file_write_off"] and scan_hits.get("X") is not None   # file write off, memory record still works


def test_scan_hits_off_records_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCAN_HITS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "SCAN_HITS_ENABLED", False)
    scan_hits.reset()
    assert scan_hits.record_scan([_C("A")]) == 0 and scan_hits.get("A") is None


def test_why_names_the_first_blocker(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SCAN_HITS_DIR", str(tmp_path))
    scan_hits.reset()
    _feed(monkeypatch, {"CLIMB": _flat(106.0), "THIN": _flat(100.0)}, {"CLIMB": (100.0, 106.0, 99.0, 100.0)})
    assert "not subscribed" in scan_why.explain("NOPE")["verdict"]
    ok = scan_why.explain("CLIMB")
    assert ok["subscribed"] and not ok["blocking"] and ok["day_move_pct"] > 5.9
    thin = scan_why.explain("THIN")
    assert thin["blocking"] and "no rolling window" in thin["verdict"]
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: 5)
    assert "volume" in scan_why.explain("CLIMB")["verdict"]
