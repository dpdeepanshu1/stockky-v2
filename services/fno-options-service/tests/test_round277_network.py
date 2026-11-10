"""Round 277: a cut-short HTTP response is a network miss (not a crash), and a failed POST /orders is flagged as "may have reached Dhan"."""
from __future__ import annotations

import http.client
import json

from fno import execution
from fno.execution import DhanBroker, OrderResult
from fno.provider import HttpProvider
from test_engine_paper import NOW
from test_real_partial import RB, db, real, entered  # noqa: F401  (db is a fixture)


def _cut(*a, **k):
    raise http.client.IncompleteRead(b"{", 10)


def test_every_broker_call_survives_a_cut_response():
    b = DhanBroker("https://x/v2", "1", "tok", opener=_cut)
    r = b.place({"a": 1})
    assert (r.ok, r.status) == (False, "NETWORK")
    assert b.order_status("1") is None and b.net_positions() is None
    c = b.cancel("1")
    assert (c.ok, c.status) == (False, "NETWORK")


def test_provider_survives_a_cut_response():
    p = HttpProvider("http://md", 1.0, opener=_cut)
    assert p.expiries("NIFTY") == [] and p.vix() is None and "IncompleteRead" in (p.last_error or "")


class NetFailBroker(RB):
    def place(self, payload):
        self.sent.append(payload)
        return OrderResult(False, status="NETWORK", message="timeout")


def test_entry_network_failure_is_flagged_and_reconciled_now(db):
    b = NetFailBroker(net={})
    e = real(db, b)
    e.tick(NOW)
    assert not db.open_positions("REAL")
    ev = [x for x in db.events("reconcile") if "may have reached Dhan" in x.get("msg", "")]
    assert ev and ev[0]["side"] == "BUY" and ev[0]["correlation_id"] == b.sent[0]["correlationId"]
    assert e.last_reconcile.get("ts") is not None                    # compared with Dhan's positions at once


def test_a_plain_rejection_is_not_flagged_as_possibly_sent(db):
    class Rej(RB):
        def place(self, payload):
            self.sent.append(payload)
            return OrderResult(False, status="HTTP 400", message="DH-905: no")
    e = real(db, Rej(net={}))
    e.tick(NOW)
    assert not [x for x in db.events("reconcile") if "may have reached Dhan" in x.get("msg", "")]
