"""E274 round 48: swing momentum health on 5-minute bars (exit_engine/swing_momentum.py). Plain runner: python3 tests/test_e274_swing_momentum.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exit_engine import swing_momentum as sm

IST = timezone(timedelta(hours=5, minutes=30))
DAY = datetime(2026, 11, 2, 9, 15, tzinfo=IST)


def _bars(n_min, price_fn, vol=1000.0, start=DAY):
    out = []
    for i in range(n_min):
        t = (start + timedelta(minutes=i)).timestamp()
        p = price_fn(i)
        out.append((t, p, p + 0.2, p - 0.2, p, vol))
    return out


def test_to_5m_only_complete_buckets():
    now = DAY + timedelta(minutes=12)
    b = sm.to_5m(_bars(12, lambda i: 100.0), DAY.timestamp(), now.timestamp())
    assert len(b) == 2 and b[0][0] == DAY.timestamp()           # 09:15-09:19 and 09:20-09:24 end by 09:27? only 2 complete
    assert sm.to_5m(_bars(12, lambda i: 100.0), DAY.timestamp() + 600, now.timestamp()) == []


def test_too_few_bars_no_opinion():
    now = DAY + timedelta(minutes=20)
    v = sm.evaluate(bars_1m=_bars(20, lambda i: 100.0), entry=98.0, stop=95.0, ltp=100.0, qty=10, opened_at=DAY - timedelta(days=1), now=now)
    assert v["action"] == "HOLD"


def test_stale_bars_no_opinion():
    v = sm.evaluate(bars_1m=_bars(60, lambda i: 100.0 + i * 0.01), entry=98.0, stop=95.0, ltp=100.6, qty=10, opened_at=DAY - timedelta(days=1), now=DAY + timedelta(minutes=200))
    assert v["action"] == "HOLD" and "stale" in v["reason"]


def test_flat_after_peak_tightens_never_lowers():
    # climbs for 40 minutes then drifts flat for 100 minutes: no new high for > 90 minutes
    f = lambda i: 100.0 + min(i, 40) * 0.1
    now = DAY + timedelta(minutes=140)
    v = sm.evaluate(bars_1m=_bars(140, f), entry=98.0, stop=95.0, ltp=104.0, qty=10, opened_at=DAY - timedelta(days=1), now=now, mode_="exit")
    assert "no_new_best" in v["signals"] and v["action"] == "HOLD"        # one signal alone never acts
    v2 = sm.evaluate(bars_1m=_bars(140, f), entry=98.0, stop=95.0, ltp=104.0, qty=10, opened_at=DAY - timedelta(days=1), now=now, mode_="tighten")
    assert v2["action"] != "BOOK"


def _climax(n=140):
    b = _bars(n, lambda i: 100.0 + min(i, 40) * 0.1)
    for k in range(n - 5, n):                                       # last complete 5-minute bar: huge volume, long upper wick, no new high
        t = b[k][0]
        b[k] = (t, 103.9, 104.15, 103.85, 103.9, 20000.0)
    return b


def test_no_new_best_plus_climax_tightens():
    now = DAY + timedelta(minutes=140)
    v = sm.evaluate(bars_1m=_climax(), entry=98.0, stop=95.0, ltp=104.0, qty=10, opened_at=DAY - timedelta(days=1), now=now, mode_="exit")
    assert "no_new_best" in v["signals"] and "climax_bar" in v["signals"]
    assert v["action"] == "TIGHTEN" and 95.0 < v["new_stop"] < 104.0
    v2 = sm.evaluate(bars_1m=_climax(), entry=98.0, stop=103.9, ltp=104.0, qty=10, opened_at=DAY - timedelta(days=1), now=now, mode_="exit")
    assert v2["action"] in ("HOLD", "TIGHTEN") and (v2["new_stop"] is None or v2["new_stop"] > 103.9)


def test_off_and_bad_input_safe():
    assert sm.evaluate(bars_1m=None, entry=98.0, stop=95.0, ltp=100.0, qty=1, opened_at=None, now=DAY, mode_="off")["action"] == "HOLD"
    assert sm.evaluate(bars_1m="x", entry="bad", stop=None, ltp=None, qty=1, opened_at=None, now="bad")["action"] == "HOLD"


def test_due_throttle():
    sm.reset()
    assert sm.due(1, 1000.0) and not sm.due(1, 1030.0) and sm.due(1, 1061.0) and sm.due(2, 1030.0)


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1
    print(f"{n} passed")
