"""E285 Batch 1 item 5: the candidate-log readers aggregate / select columns in the database instead of loading whole rows.
Needs sqlalchemy (sqlite in memory); the SQL itself is what is under test."""
from datetime import datetime, timedelta, timezone

import pytest

import config


@pytest.fixture()
def factory():
    import models
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(eng, tables=[models.ScalpCandidateLog.__table__])
    return sessionmaker(bind=eng)


def _add(factory, rows):
    import models
    s = factory()
    for sym, pct, age_h in rows:
        s.add(models.ScalpCandidateLog(symbol=sym, window_source="5m", pct_change=pct, decision="SKIPPED", reason="X",
                                       created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=age_h)))
    s.commit()
    s.close()


def test_preopen_movers_are_the_top_abs_move_per_symbol_in_sql(factory, monkeypatch):
    from screening import quality_gate as qg
    monkeypatch.setattr(config, "QUALITY_GATE_PREOPEN_WARM_MOVERS", 2, raising=False)
    monkeypatch.setattr(config, "QUALITY_GATE_PREOPEN_WARM_MAX", 0, raising=False)
    _add(factory, [("AAA", 1.0, 1), ("AAA", -6.0, 2), ("BBB", 4.0, 1), ("CCC", 3.0, 1), ("DDD", 9.0, 100), ("EEE", None, 1)])
    s = factory()
    try:
        assert qg.preopen_warm_symbols(s) == ["AAA", "BBB"]      # AAA |-6|, BBB 4; CCC cut by the cap, DDD older than 3 days, EEE has no move
    finally:
        s.close()


def test_rows_from_log_returns_the_same_dicts_from_four_columns(factory):
    from orders import candidate_outcomes as co
    import models
    _add(factory, [("AAA", 1.0, 0)])
    s = factory()
    try:
        day = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
        rows = co.rows_from_log(s, models, day)
        assert rows and set(rows[0]) == {"symbol", "decision", "reason", "created_ts", "side"} and rows[0]["side"] == "LONG"
    finally:
        s.close()
