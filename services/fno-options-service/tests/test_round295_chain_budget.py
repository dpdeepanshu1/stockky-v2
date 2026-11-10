"""F&O round 295: Dhan allows one option-chain call per ~3 s, so every chain fetch counts. Open positions' chains are fetched FIRST (entry analysis used to queue ahead of the stop checks), each chain once
per cycle, and a position whose fresh chain cannot be had is managed on the last chain seen (at most 30 s old) instead of not at all. No network."""
from __future__ import annotations

from datetime import timedelta

import pytest

from fno import engine as eng, paper, store as st
from fno.models import ContractRef
from test_engine_paper import NOW, FakeProvider, make_cfg


@pytest.fixture
def db():
    s = st.Store(":memory:")
    yield s
    s.close()


class Logging(FakeProvider):
    def __init__(self):
        super().__init__()
        self.calls, self.down = [], False

    def chain(self, u, e):
        self.calls.append((u, e))
        return None if self.down else super().chain(u, e)


def _engine(db, prov):
    return eng.Engine(db, prov, make_cfg(FNO_UNDERLYINGS="NIFTY"))


def _open_nifty(db, e, expiry):
    c = ContractRef("NIFTY", expiry, 24500.0, "CE", 65, 111, "NSE_FNO", 0.05)
    p = paper.open_position(c, 1, 100.0, NOW.timestamp(), e.params())
    db.insert_position(p)
    return p


def test_the_chain_of_an_open_position_is_fetched_before_the_entry_analysis(db):
    prov = Logging(); e = _engine(db, prov)
    _open_nifty(db, e, "2026-11-24")                    # the engine analyses 2026-11-17: a different chain
    e.tick(NOW)
    assert prov.calls and prov.calls[0] == ("NIFTY", "2026-11-24")
    assert ("NIFTY", "2026-11-17") in prov.calls


def test_each_chain_is_fetched_once_per_cycle(db):
    prov = Logging(); e = _engine(db, prov)
    _open_nifty(db, e, "2026-11-17")                    # the same chain the analysis uses
    e.tick(NOW)
    assert prov.calls.count(("NIFTY", "2026-11-17")) == 1


def test_a_failed_fetch_uses_the_last_chain_seen_when_it_is_recent(db):
    prov = Logging(); e = _engine(db, prov)
    p = _open_nifty(db, e, "2026-11-17")
    e.tick(NOW)                                         # fills the last-seen store
    prov.down = True
    e.manage_open(NOW + timedelta(seconds=10), e.params(), {}, {})
    kinds = [x["kind"] for x in db.events()]
    assert "stale_chain" in kinds and "stale" not in kinds
    assert db.get_position(p.id).max_premium >= 100.0   # the premium was read, so the high / trail were updated


def test_an_old_chain_is_not_used(db):
    prov = Logging(); e = _engine(db, prov)
    _open_nifty(db, e, "2026-11-17")
    e.tick(NOW)
    prov.down = True
    e.manage_open(NOW + timedelta(seconds=120), e.params(), {}, {})
    kinds = [x["kind"] for x in db.events()]
    assert "stale" in kinds and "stale_chain" not in kinds


def test_a_fetch_that_raises_for_an_open_position_is_a_missing_chain_not_an_aborted_cycle(db):
    class Boom(Logging):
        def chain(self, u, e):
            if e == "2026-11-24":
                raise RuntimeError("upstream down")
            return super().chain(u, e)
    prov = Boom(); e = _engine(db, prov)
    _open_nifty(db, e, "2026-11-24")
    r = e.tick(NOW)
    assert "error" not in r                              # the analysis of 2026-11-17 and the rest of the cycle still ran


def test_recent_chain_respects_its_age_limit(db):
    prov = Logging(); e = _engine(db, prov)
    e.tick(NOW)
    assert e.recent_chain("NIFTY", "2026-11-17", NOW.timestamp() + 5, max_age_s=10) is not None
    assert e.recent_chain("NIFTY", "2026-11-17", NOW.timestamp() + 11, max_age_s=10) is None
    assert e.recent_chain("NIFTY", "2026-12-31", NOW.timestamp()) is None
