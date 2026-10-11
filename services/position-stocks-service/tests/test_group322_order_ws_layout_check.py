"""group322: order websocket keeps raw message samples and reports a layout verdict."""
from __future__ import annotations

import asyncio
import json

from execution import order_ws

GOOD = json.dumps({"Type": "order_alert", "Data": {"OrderNo": "1", "Status": "TRADED", "TradedQty": 1}})


class FakeWS:
    def __init__(self, msgs):
        self.msgs = list(msgs)

    async def send(self, m):
        pass

    async def close(self):
        pass

    def __aiter__(self):
        return self._it()

    async def _it(self):
        for m in self.msgs:
            yield m


def _run(msgs):
    sockets = [FakeWS(msgs)]
    holder = {}

    async def connect(url):
        if not sockets:
            holder["lst"].stop()
            raise ConnectionError("done")
        return sockets.pop(0)
    lst = order_ws.OrderUpdateListener(lambda: ("1", "SECRET-TOKEN"), connect=connect,
                                      event_store=order_ws.OrderEventStore(), backoff_min=0.0, backoff_max=0.0)
    holder["lst"] = lst
    asyncio.run(lst.run())
    return lst.status()


def test_good_stream_is_ok_and_samples_first_three():
    st = _run([GOOD] * 5)
    assert st["layout_verdict"] == "ok" and st["unparsed"] == 0
    assert len(st["samples"]) == 3 and st["samples"][0]["tag"] == "message"
    assert "SECRET-TOKEN" not in json.dumps(st)


def test_pile_of_unparsed_is_suspect_and_samples_kept():
    st = _run([json.dumps({"foo": i}) for i in range(7)])
    assert st["order_events"] == 0 and st["unparsed"] == 7 and st["layout_verdict"] == "suspect"
    assert any(s["tag"] == "unparsed" for s in st["samples"])
    assert len(st["samples"]) <= 6


def test_one_ack_message_is_unverified_not_suspect():
    st = _run([json.dumps({"Type": "login_ack"})])
    assert st["layout_verdict"] == "unverified"


def test_mostly_unparsed_with_some_events_is_suspect():
    st = _run([GOOD] + ["junk"] * 6)
    assert st["layout_verdict"] == "suspect"


def test_no_messages_is_unverified():
    assert _run([])["layout_verdict"] == "unverified"


def test_text_cut_to_400_chars():
    st = _run(["x" * 5000])
    assert all(len(s["text"]) <= 400 for s in st["samples"])
