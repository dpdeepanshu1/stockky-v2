"""E39: real daily cap on new automatic entries."""
from datetime import datetime, timedelta, timezone

import pytest

import config
from orders import entry_cap
from tz_utils import IST


def test_default_is_a_real_cap():
    assert 0 < config.MAX_ENTRIES_PER_DAY < 300


def test_reason_follows_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_ENTRIES_PER_DAY", 3, raising=False)
    assert entry_cap.reason_from(2) is None
    assert entry_cap.reason_from(3).startswith("ENTRY_CAP:")
    assert entry_cap.reason_from(9).startswith("ENTRY_CAP:")


def test_zero_is_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_ENTRIES_PER_DAY", 0, raising=False)
    assert entry_cap.reason_from(500) is None and entry_cap.entry_reason(None) is None


def test_garbage_config_fails_open(monkeypatch):
    monkeypatch.setattr(config, "MAX_ENTRIES_PER_DAY", "abc", raising=False)
    assert entry_cap.limit() == 0 and entry_cap.reason_from(99) is None


def test_db_error_fails_open(monkeypatch):
    monkeypatch.setattr(config, "MAX_ENTRIES_PER_DAY", 1, raising=False)

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert entry_cap.entries_today(Boom()) == 0
    assert entry_cap.entry_reason(Boom()) is None


def test_counts_today_only_and_skips_error_rows(monkeypatch):
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.orm import sessionmaker
    import models
    eng = sa.create_engine("sqlite://")
    models.ScalpPosition.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    now = datetime(2026, 10, 23, 11, 0, tzinfo=IST)
    def add(status, opened):
        db.add(models.ScalpPosition(symbol="X", dhan_security_id="1", window_source="5m", status=status, entry_price=1.0, quantity=1,
                                    target_price=2.0, stop_price=0.5, adaptive_target_pct=1.0, adaptive_stop_pct=1.0, capital_risked=0.0, opened_at=opened.astimezone(timezone.utc)))
    add("OPEN", now - timedelta(hours=1))
    add("TARGET_HIT", now - timedelta(hours=1, minutes=5))
    add("ERROR", now - timedelta(minutes=30))
    add("OPEN", now - timedelta(days=1))
    db.commit()
    assert entry_cap.entries_today(db, now) == 2
    monkeypatch.setattr(config, "MAX_ENTRIES_PER_DAY", 2, raising=False)
    assert entry_cap.entry_reason(db, now).startswith("ENTRY_CAP:")
