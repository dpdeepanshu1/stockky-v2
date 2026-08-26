"""tz_utils.py — single place to fix the offset-naive vs offset-aware
datetime crash across this service.

Root cause: models.py declares timestamp columns as plain `DateTime`
(no `timezone=True`). Every write goes in as tz-aware UTC
(`datetime.now(timezone.utc)`), but most DB drivers (SQLite always,
Postgres unless the column is TIMESTAMPTZ) hand it back on read as a
*naive* datetime. Comparing that naive value against a fresh
`datetime.now(timezone.utc)` raises:
    TypeError: can't compare offset-naive and offset-aware datetimes

This is exactly what was crashing GET /status/REAL and POST /dhan/connect
(_check_and_expire_gates in main.py and connection_status/is_token_valid
in auth/dhan_credentials.py).

Fix: always pass DB-sourced datetimes through `as_aware()` before
comparing them to `datetime.now(timezone.utc)`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Return dt with UTC tzinfo attached if it's naive. Every timestamp
    this service writes is UTC, so a naive value read back from the DB is
    assumed to be naive-UTC, not local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
