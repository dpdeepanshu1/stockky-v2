"""group470: TimesFM preflight (worker not started), G1 footer wording, slippage wording. Offline, stdlib only."""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import urllib.error
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import entry_slippage_review as E  # noqa: E402
import ms_g1_check as G  # noqa: E402
import tsfm_nightly as N  # noqa: E402
import tsfm_probe as P  # noqa: E402


class _Boom:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *a, **k):
        raise self.exc


def _patch(mod, exc):
    old = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = _Boom(exc)
    return old


def test_preflight_recognises_a_name_that_does_not_resolve_and_nothing_else():
    for mod in (N, P):
        old = _patch(mod, urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")))
        try:
            assert "does not resolve" in mod.service_not_running("http://timesfm-forecast-service:8011")
        finally:
            mod.urllib.request.urlopen = old
        for other in (urllib.error.URLError(ConnectionRefusedError(111, "refused")), TimeoutError("t"), urllib.error.HTTPError("u", 500, "x", {}, None)):
            old = _patch(mod, other)
            try:
                assert mod.service_not_running("http://x:1") == "", other        # a real failure keeps its normal failure path
            finally:
                mod.urllib.request.urlopen = old


def test_nightly_skips_cleanly_when_the_worker_is_not_started_and_fails_when_required():
    old = _patch(N, urllib.error.URLError(socket.gaierror(-3, "x")))
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = N.main(["--url", "http://timesfm-forecast-service:8011", "--symbols", "AAA,BBB"])
        out = json.loads(buf.getvalue())
        assert rc == 0 and out["skipped"] is True and "timesfm" in out["reason"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = N.main(["--url", "http://timesfm-forecast-service:8011", "--symbols", "AAA", "--retries", "1", "--retry-wait", "0", "--require-service"])
        assert rc == 1 and "reason" not in json.loads(buf.getvalue())      # --require-service: the normal run, which fails
    finally:
        N.urllib.request.urlopen = old


def test_probe_skips_before_reading_any_history_when_the_worker_is_not_started():
    old = _patch(P, urllib.error.URLError(socket.gaierror(-3, "x")))
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = P.main(["/nonexistent-hist", "--tsfm-url", "http://timesfm-forecast-service:8011"])
        assert rc == 0 and "SKIPPED" in buf.getvalue()
        with redirect_stdout(io.StringIO()):
            assert P.main(["/nonexistent-hist", "--tsfm-url", "http://t:1", "--require-service"]) == 2
    finally:
        P.urllib.request.urlopen = old


def _g1(lat, trunc=G.PASS, ca=G.PASS):
    return {"overall": G.FAIL, "symbols": 355, "manual": ["m"], "checks": {
        "truncation_invariance": {"verdict": trunc, "violation_count": 0, "needs_symbols": 20},
        "corporate_actions": {"verdict": ca, "verified": 12, "cases": 12, "broken": [], "needs_cases": 10},
        "latency": {"verdict": lat, "p50_ms": 6.9, "p95_ms": 57.4, "budget_ms": 15.0}}}


def test_g1_latency_only_fail_does_not_claim_future_data_or_a_split_bug():
    md = G.to_md(_g1(G.FAIL))
    assert "SPEED finding" in md and "57.4" in md and "15.0 ms budget" in md
    assert "future data" not in md
    md2 = G.to_md(_g1(G.PASS, trunc=G.FAIL))
    assert "future data" in md2 and "SPEED" not in md2
    assert "future data" in G.to_md(_g1(G.WARN, ca=G.FAIL)) and "SPEED" in G.to_md(_g1(G.WARN, ca=G.FAIL))


def test_g1_budget_default_is_15_and_follows_the_env():
    assert 'os.environ.get("G1_BUDGET_MS", "15")' in open(G.__file__).read()


def test_slippage_line_says_which_sign_is_a_gain():
    recs = [{"entry_slippage_pct": -0.1, "entry_price": 99.9, "quantity": 10, "gross_pnl": 5.0, "entry_hour_ist": "10", "source_tab": "x"} for _ in range(3)]
    txt = E.to_markdown(E.review(recs, min_n=1))
    assert "paid above the signal in total" not in txt
    assert "net rupees against the signal" in txt and "- paid below it" in txt
