"""Round 279: one index that fails to analyse (odd feed shape, bad chain) must not stop the cycle's exits, nor the other index."""
from __future__ import annotations

from fno import paper
from test_engine_paper import C, NOW, P, FakeProvider, engine, make_cfg
from test_round236 import db  # noqa: F401  (db is a fixture)


class FailingIntraday(FakeProvider):
    def __init__(self, bad=("NIFTY", "SENSEX")):
        super().__init__()
        self.bad = bad

    def intraday(self, u):
        if u in self.bad:
            raise RuntimeError("odd feed shape")
        return super().intraday(u)


def test_an_open_position_is_still_squared_off_when_the_analysis_raises(db):
    e = engine(db, FailingIntraday(), make_cfg(FNO_UNDERLYINGS="NIFTY"))
    p = paper.open_position(C, 2, 100.0, NOW.timestamp() - 3600, P)
    db.insert_position(p)
    out = e.tick(NOW.replace(hour=15, minute=20))
    assert db.get_position(p.id).status == paper.CLOSED and db.get_position(p.id).exit_reason == paper.SQUAREOFF
    assert out["error"].startswith("RuntimeError") and e.last_tick["ok"] is False and out["decisions"]["NIFTY"]["action"] == "wait" and "analysis failed" in out["decisions"]["NIFTY"]["why"][0]
    ev = [x for x in db.events() if x["kind"] == "strategy_error"]
    assert ev and "RuntimeError" in ev[0]["error"] and "open positions are still managed" in ev[0]["msg"]


def test_the_other_index_is_still_analysed_and_can_enter(db):
    out = engine(db, FailingIntraday(bad=("SENSEX",)), make_cfg(FNO_UNDERLYINGS="BOTH")).tick(NOW)
    assert out["decisions"]["SENSEX"]["action"] == "wait" and out["entries"]
    assert all(db.get_position(i).underlying == "NIFTY" for i in out["entries"])
