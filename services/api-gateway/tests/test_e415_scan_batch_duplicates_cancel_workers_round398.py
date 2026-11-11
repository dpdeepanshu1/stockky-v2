"""E415: POST /scan/batch analyses a repeated symbol once (still one row per requested entry), turns a cancelled child into an ERROR row instead of an
object in the JSON, and sizes its semaphore from MAX_PARALLEL_WORKERS."""
from __future__ import annotations

import asyncio

import pytest

from test_main_stock_scan_routes import TestScanBatch as _Base, _Req, _run, gw  # noqa: F401


class TestE415(_Base):
    pass


# keep only the new tests from the subclass: pytest would otherwise re-run every inherited test
for _n in [n for n in dir(_Base) if n.startswith("test_")]:
    setattr(TestE415, _n, None)


def _t(name):
    def deco(fn):
        setattr(TestE415, name, fn)
        return fn
    return deco


@_t("test_a_repeated_symbol_is_analysed_once_and_still_gets_a_row_each")
def _dup(self):
    out = self.batch(["TCS", "infy", "tcs.ns", "TCS"])
    assert [a[0] for a in self.analysed] == ["TCS", "INFY"]
    assert [r["symbol"] for r in out["results"]] == ["TCS", "INFY", "TCS", "TCS"]
    assert out["results"][0] is not out["results"][2] and out["results"][2] == out["results"][3]


@_t("test_a_cancelled_child_becomes_an_error_row")
def _cancel(self):
    self.per_symbol["INFY"] = asyncio.CancelledError()
    out = self.batch(["TCS", "INFY"])
    assert out["results"][0]["decision"] == "BUY"
    assert out["results"][1] == {"symbol": "INFY", "decision": "ERROR", "error": "CancelledError"}


@_t("test_the_semaphore_follows_max_parallel_workers")
def _sem(self):
    old = gw.MAX_PARALLEL_WORKERS
    gw.MAX_PARALLEL_WORKERS = 3
    try:
        self.batch(["TCS"])
    finally:
        gw.MAX_PARALLEL_WORKERS = old
    assert self.analysed[0][2]._value == 3


@_t("test_a_bad_worker_setting_falls_back_to_8")
def _bad(self):
    old = gw.MAX_PARALLEL_WORKERS
    gw.MAX_PARALLEL_WORKERS = "x"
    try:
        self.batch(["TCS"])
    finally:
        gw.MAX_PARALLEL_WORKERS = old
    assert self.analysed[0][2]._value == 8
