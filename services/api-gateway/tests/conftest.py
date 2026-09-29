"""Shared pytest setup for services/api-gateway.

* Puts the service directory on sys.path (the gateway is a flat set of top-level modules).
* Isolates every test from the developer's / VM's real environment: no real Redis, DB or
  QStash credentials can leak in, so nothing here ever touches the network or a database.
"""
import os
import sys

import pytest

_SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)

_ISOLATED_ENV = (
    "USE_REDIS", "DISABLE_REDIS", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
    "DATABASE_URL", "ORACLE_DSN", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_WALLET_PASSWORD",
    "QSTASH_TOKEN", "QSTASH_CURRENT_SIGNING_KEY", "QSTASH_NEXT_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in _ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
