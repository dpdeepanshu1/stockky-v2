"""Drift guard: the six `kv_cache.py` copies may differ, but only in ways we have decided on.

There is no shared package, so each service carries its own copy of the Neon/Oracle-backed cache:

* four byte-identical "plain" copies - analysis-intelligence `fundamental/`, decision-prediction
  `decision/` and `training/`, notification-scheduler `notification/`;
* `market-data-service/kv_cache.py` = plain + the `fundamentals:` durable prefix (its own
  `/fundamentals/{symbol}` cache);
* `api-gateway/kv_cache.py` = plain + the gateway's durable feed/IPO/surprise prefixes, and the
  `kv_get_stale()` / `get_stale()` stale-read helpers.

Those differences are correct only while no other service touches the keys they exist for. A prefix
missing from a copy does not raise: `_is_durable()` just returns False and the key silently becomes
memory-only (lost on every Render restart). So this test pins both halves:

1. the copies differ only by the documented extras (nothing else drifts, and no copy loses a shared prefix);
2. no service other than the owner references an owner-only prefix or helper.

If one of these fails, either put the missing prefix/helper into the other copy, or update the
expectations below on purpose. It reads the files as source (ast) instead of importing them, because
importing would pull in each module's dependencies and side effects.
"""
import ast
import os
import re

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PLAIN_COPIES = (
    "analysis-intelligence-service/fundamental/kv_cache.py",
    "decision-prediction-service/decision/kv_cache.py",
    "decision-prediction-service/training/kv_cache.py",
    "notification-scheduler-service/notification/kv_cache.py",
)
MARKET_DATA = "market-data-service/kv_cache.py"
GATEWAY = "api-gateway/kv_cache.py"
ALL_COPIES = PLAIN_COPIES + (MARKET_DATA, GATEWAY)

MARKET_DATA_ONLY = {"fundamentals:"}
GATEWAY_ONLY = {
    "stockky:hot_premarket_job",
    "stockky:ipo:",
    "stockky:ipoalerts:",
    "system:surprise_feed",
    "system:bulk_quote_cache",
    "stockky:hot_stocks",
    "stockky:surprise_scan:",
    "stockky:market_movers_last_known",  # 2026-10-04: closed-market Movers panel survives a restart
}
GATEWAY_ONLY_HELPERS = ("kv_get_stale", "get_stale")

# Keys more than one service writes; every copy must treat them as durable.
SHARED_DURABLE_KEYS = (
    "stockky:rate_limit_stats",
    "stockky:rate_limit_events_neon",
    "system:rate_limit_stats",
    "stockky:notification_config",
    "stockky:decide_cache:RELIANCE",
    "indianapi:fundamentals:RELIANCE",
)


def _path(rel):
    return os.path.join(SERVICES, *rel.split("/"))


def _read(rel):
    with open(_path(rel), encoding="utf-8") as fh:
        return fh.read()


def _prefixes(src):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DURABLE_PREFIXES" for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError("_DURABLE_PREFIXES not found")


def _is_durable(prefixes, key):
    # Same rule as kv_cache._is_durable in every copy.
    return any(key.startswith(p) or key == p for p in prefixes)


SOURCES = {rel: _read(rel) for rel in ALL_COPIES}
PREFIXES = {rel: _prefixes(src) for rel, src in SOURCES.items()}
BASE = PREFIXES[PLAIN_COPIES[0]]


def _service_py_files():
    """Every non-test .py under services/ except the kv_cache.py copies themselves."""
    for dirpath, dirnames, filenames in os.walk(SERVICES):
        dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__", "node_modules", ".git", ".venv", "venv", "site-packages", ".tox", "build", "dist")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, SERVICES).replace(os.sep, "/")
            if rel in ALL_COPIES:
                continue
            yield rel, full


def _outside(owner_dir):
    """(rel, source) for every service file that is NOT inside `owner_dir`."""
    for rel, full in _service_py_files():
        if rel.startswith(owner_dir + "/"):
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                yield rel, fh.read()
        except (OSError, UnicodeDecodeError):
            continue


def test_the_guard_found_all_six_copies():
    assert len(ALL_COPIES) == 6
    for rel in ALL_COPIES:
        assert os.path.isfile(_path(rel)), rel
    # A seventh copy would be invisible to this guard.
    found = []
    for dirpath, dirnames, filenames in os.walk(SERVICES):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules", ".git", ".venv", "venv", "site-packages", ".tox", "build", "dist")]
        if "kv_cache.py" in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, "kv_cache.py"), SERVICES).replace(os.sep, "/"))
    assert sorted(found) == sorted(ALL_COPIES), "a kv_cache.py copy was added or moved - register it here"


@pytest.mark.parametrize("rel", PLAIN_COPIES[1:])
def test_plain_copies_are_byte_identical(rel):
    assert SOURCES[rel] == SOURCES[PLAIN_COPIES[0]], (
        f"{rel} differs from {PLAIN_COPIES[0]} - re-sync the copies, or move this one out of PLAIN_COPIES "
        "and document why it differs"
    )


@pytest.mark.parametrize("rel", ALL_COPIES)
def test_no_copy_loses_a_shared_prefix(rel):
    missing = BASE - PREFIXES[rel]
    assert not missing, f"{rel} dropped shared durable prefixes: {sorted(missing)}"


def test_market_data_extras_are_exactly_the_documented_ones():
    assert PREFIXES[MARKET_DATA] - BASE == MARKET_DATA_ONLY


def test_gateway_extras_are_exactly_the_documented_ones():
    assert PREFIXES[GATEWAY] - BASE == GATEWAY_ONLY


@pytest.mark.parametrize("rel", ALL_COPIES)
@pytest.mark.parametrize("key", SHARED_DURABLE_KEYS)
def test_keys_written_by_several_services_are_durable_everywhere(rel, key):
    assert _is_durable(PREFIXES[rel], key), f"{key!r} would be memory-only in {rel}"


def test_stale_read_helpers_exist_only_in_the_gateway_copy():
    for name in GATEWAY_ONLY_HELPERS:
        assert re.search(rf"^def {name}\(", SOURCES[GATEWAY], re.M), f"{name} missing from the gateway copy"
        for rel in PLAIN_COPIES + (MARKET_DATA,):
            assert not re.search(rf"^def {name}\(", SOURCES[rel], re.M), (
                f"{name} appeared in {rel}; the guard's premise (gateway-only) changed - update it deliberately"
            )


@pytest.mark.parametrize("name", GATEWAY_ONLY_HELPERS)
def test_no_other_service_calls_the_gateway_only_stale_helpers(name):
    pat = re.compile(rf"\b{name}\b")
    offenders = [rel for rel, src in _outside("api-gateway") if pat.search(src)]
    assert not offenders, (
        f"{offenders} reference {name}, which only api-gateway/kv_cache.py defines - copy it into that "
        "service's kv_cache.py first"
    )


@pytest.mark.parametrize("prefix", sorted(GATEWAY_ONLY))
def test_no_other_service_uses_a_gateway_only_durable_prefix(prefix):
    offenders = [rel for rel, src in _outside("api-gateway") if prefix in src]
    assert not offenders, (
        f"{offenders} use {prefix!r}, which is durable only in api-gateway/kv_cache.py - in that service "
        "the key would silently be memory-only. Add the prefix to its kv_cache.py copy."
    )


def test_gateway_does_not_use_the_market_data_fundamentals_cache_key():
    pat = re.compile(r"""["']fundamentals:""")
    offenders = [rel for rel, src in _service_py_files_in("api-gateway") if pat.search(src)]
    assert not offenders, (
        f"{offenders} use a bare 'fundamentals:' key, which is durable only in market-data-service's copy"
    )


def _service_py_files_in(owner_dir):
    for rel, full in _service_py_files():
        if rel.startswith(owner_dir + "/"):
            try:
                with open(full, encoding="utf-8") as fh:
                    yield rel, fh.read()
            except (OSError, UnicodeDecodeError):
                continue
