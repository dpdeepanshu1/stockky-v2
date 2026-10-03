"""tests/test_env_blank_sweep.py — group 67: whitespace-only / padded URL and credential env vars.

`os.getenv("A") or os.getenv("B")` only skips a variable that is unset or "". A whitespace-only value
(`CACHE_DATABASE_URL="  "`, a stray space in an env_file or a dashboard field) is truthy, so it beat a
valid DATABASE_URL and the cache connection failed; a padded UPSTASH_REDIS_REST_URL was passed to the
Redis client untrimmed, and a padded MARKET_DATA_URL / DECISION_URL was used as-is. Every such read
now goes through `(os.getenv("NAME") or "").strip()`.

This is a source-level guard over every service (the services share no package, so the same one-liner
is repeated at each read) plus behavioural cases on the shared kv_cache copy, which the drift guard
(test_kv_cache_drift.py) keeps identical in the other services.

Run from services/api-gateway:
    python3 -m pytest tests/test_env_blank_sweep.py -v
"""
from __future__ import annotations

import os
import re

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SKIP_DIRS = {"tests", "__pycache__", ".venv", "venv", "node_modules", ".git"}
_NAMES = (
    "CACHE_DATABASE_URL", "KV_DATABASE_URL", "DATABASE_URL", "TRAINING_DATABASE_URL",
    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
)
_RAW = re.compile(r'_?os\.(?:getenv|environ\.get)\(\s*"(%s)"\s*(,[^)]*)?\)' % "|".join(_NAMES))


def _sources():
    for cur, dirs, files in os.walk(SERVICES):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(cur, f)
                with open(p, encoding="utf-8", errors="ignore") as fh:
                    yield os.path.relpath(p, SERVICES), fh.read()


def test_every_database_and_upstash_read_is_stripped():
    offenders = []
    for rel, text in _sources():
        for m in _RAW.finditer(text):
            wrapped = text[max(0, m.start() - 1):m.end() + len(' or "").strip()')]
            if wrapped != '(' + m.group(0) + ' or "").strip()':
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{rel}:{line}: {m.group(0)}")
    assert offenders == []


@pytest.mark.parametrize("rel,needle", [
    ("api-gateway/main.py", '((os.getenv("MARKET_DATA_URL") or "").strip() or MARKET_DATA_URL or "").strip().rstrip("/")'),
    ("api-gateway/main.py", '((os.getenv("TECHNICAL_URL") or "").strip() or TECHNICAL_URL or "").strip().rstrip("/")'),
    ("api-gateway/main.py", '((os.getenv("FUNDAMENTAL_URL") or "").strip() or FUNDAMENTAL_URL or "").strip().rstrip("/")'),
    ("api-gateway/main.py", '((os.getenv("NEWS_URL") or "").strip() or NEWS_URL or "").strip().rstrip("/")'),
    ("api-gateway/surprise_scanner.py", '(os.getenv("MARKET_DATA_URL") or "").strip()'),
    ("api-gateway/hotpicks_store.py", '(os.getenv("MARKET_DATA_URL") or "").strip()'),
    ("api-gateway/hotpicks_store.py", '(os.getenv("DECISION_URL") or "").strip()'),
    ("notification-scheduler-service/scheduler/governance_check.py", '(os.getenv("REAL_TRADE_URL") or "").strip().rstrip("/")'),
    ("analysis-intelligence-service/fundamental/wire_peer_multi_quarter.py", '(os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/")'),
    ("real-trade-service/scripts/historical_backtest_calibration.py",
     '(os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/") or "http://market-data-service:8001"'),
])
def test_service_url_reads_are_trimmed(rel, needle):
    with open(os.path.join(SERVICES, *rel.split("/")), encoding="utf-8") as fh:
        assert needle in fh.read()
