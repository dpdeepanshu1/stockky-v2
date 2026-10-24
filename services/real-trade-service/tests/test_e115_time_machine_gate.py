"""E115: the Time Machine live gate in real-trade-service (entry_engine/tm_gate.py adapter + Gate 4h wiring). No network: the market-data fetch is replaced."""
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from entry_engine import tm_gate  # noqa: E402
from timemachine import data as tm_data  # noqa: E402
from timemachine import gate_core as g  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=IST)


def trend_rows(step, n_days=24):
    rows = []
    for d in range(1, n_days + 1):
        px = 100.0
        for s in range(75):
            o, c = px, px * (1 + step)
            m = 555 + 5 * s
            rows.append({"date": f"2026-09-{d:02d} {m // 60:02d}:{m % 60:02d}", "open": o, "high": max(o, c) * 1.0001, "low": min(o, c) * 0.9999, "close": c, "volume": 1000})
            px = c
    return rows


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    g.reset()
    tm_data.clear_cache()
    monkeypatch.setattr(g, "_now_ist", lambda now=None: MON_10 if now is None else (now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)))
    for k, v in (("TM_GATE_MODE", "live"), ("TIME_MACHINE_ENABLED", True), ("TM_GATE_INLINE_BUDGET_S", 5.0), ("TM_PICK_NUDGE_PTS", 6.0), ("TM_GATE_BLOCK", "against")):
        monkeypatch.setattr(config, k, v, raising=False)
    yield
    g.reset()
    tm_data.clear_cache()


def feed(monkeypatch, rows):
    monkeypatch.setattr(tm_data, "http_fetcher", lambda sym, lb: rows)


def test_a_long_in_a_falling_window_is_refused_with_the_real_cost_model(monkeypatch):
    feed(monkeypatch, trend_rows(-0.0006))
    reason, ev = tm_gate.evaluate("ABC", 1.0, 2.0, 100.0)
    assert reason and reason.startswith("TIME_MACHINE:") and ev["v"]["code"] == "AGAINST"


def test_a_long_in_a_rising_window_passes_and_ranks_up(monkeypatch):
    feed(monkeypatch, trend_rows(0.0006))
    reason, ev = tm_gate.evaluate("ABC", 1.0, 2.0, 100.0)
    assert reason is None and ev["v"]["code"] == "FAVOURS"
    assert tm_gate.rank_points(ev) == 6.0
    import json
    assert json.loads(tm_gate.stamp('{"a":1}', ev))["tm"]["code"] == "FAVOURS"


def test_log_mode_never_blocks_and_off_does_nothing(monkeypatch):
    feed(monkeypatch, trend_rows(-0.0006))
    monkeypatch.setattr(config, "TM_GATE_MODE", "log")
    reason, ev = tm_gate.evaluate("ABC", 1.0, 2.0, 100.0)
    assert reason is None and ev["would_reject"] is True
    monkeypatch.setattr(config, "TM_GATE_MODE", "off")
    assert tm_gate.evaluate("ABC", 1.0, 2.0, 100.0) == (None, None) and tm_gate.rank_points(ev) == 0.0


def test_dead_market_data_never_blocks(monkeypatch):
    def boom(sym, lb):
        raise tm_data.DataError("DATA_UNREACHABLE", "down")
    monkeypatch.setattr(tm_data, "http_fetcher", boom)
    reason, ev = tm_gate.evaluate("ABC", 1.0, 2.0, 100.0)
    assert reason is None and ev["opinion"] == "no_opinion"


def test_note_shortlist_queues_background_verdicts(monkeypatch):
    feed(monkeypatch, trend_rows(0.0006))
    entries = [{"cand": SimpleNamespace(symbol="ABC"), "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0},
               {"cand": SimpleNamespace(symbol="XYZ"), "entry_price": 200.0, "stop_price": 198.0, "target_price": 204.0},
               {"bad": 1}]
    assert tm_gate.note_shortlist(entries) == 2
    assert g.process_queue_once() == 2
    assert g.peek("ABC", "LONG")["code"] == "FAVOURS"
    assert tm_gate.rank_points(None) == 0.0 and tm_gate.note_shortlist([]) == 0


def test_the_real_trade_copy_of_the_gate_runs_without_a_database():
    src = (ROOT / "timemachine" / "gate_core.py").read_text()
    assert "sqlalchemy" not in src and "place_order" not in src


def test_gate_4h_is_before_the_reward_risk_floor_and_fails_open():
    src = (ROOT / "entry_engine" / "entry.py").read_text()
    i = src.index("Gate 4h (E115)")
    assert i < src.index("Gate 5: reward:risk floor")
    block = src[i:src.index("Gate 5: reward:risk floor")]
    assert "except Exception" in block and "(allowing)" in block and 'decision.gate_tag = tm_gate.TAG' in block and "asyncio.to_thread" in block


def test_the_switches_exist_in_real_trade_config():
    for k in ("TM_GATE_MODE", "TM_GATE_BLOCK", "TM_GATE_SIMS", "TM_GATE_INLINE_BUDGET_S", "TM_GATE_TTL_S", "TM_PICK_NUDGE_PTS", "TM_AUTO_ENABLED", "TM_AUTO_FIRST_IST",
              "TM_AUTO_LAST_IST", "TM_AUTO_INTERVAL_MIN", "TIME_MACHINE_ENABLED", "TIME_MACHINE_LOOKBACK"):
        assert hasattr(config, k), k
    assert config.TM_GATE_MODE in ("live", "log", "off")
