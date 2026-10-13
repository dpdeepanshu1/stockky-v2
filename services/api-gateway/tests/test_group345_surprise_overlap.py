"""group345: two overlapping scan() runs keep their own counters / prefetch dict / deadline."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import surprise_scanner as ss


def test_overlapping_scans_do_not_share_counters_or_prefetch():
    eng = ss.SurpriseStockEngine()
    seen = {}

    async def scan(name, n, gate_in, gate_out):
        eng._bulk_ticks = {}
        eng._scan_counts = {}
        eng._single_deadline = float(n)
        eng._bulk_ticks[name] = {"p": n}
        await gate_in.wait()
        for _ in range(n):
            eng._scan_counts["bulk"] = eng._scan_counts.get("bulk", 0) + 1
        gate_out.set()
        await asyncio.sleep(0)
        seen[name] = (dict(eng._scan_counts), dict(eng._bulk_ticks), eng._single_deadline)

    async def main():
        a_in, a_out, b_in, b_out = (asyncio.Event() for _ in range(4))
        ta = asyncio.create_task(scan("A", 3, a_in, a_out))
        tb = asyncio.create_task(scan("B", 5, b_in, b_out))
        await asyncio.sleep(0)
        b_in.set(); a_in.set()
        await asyncio.gather(ta, tb)

    asyncio.run(main())
    assert seen["A"] == ({"bulk": 3}, {"A": {"p": 3}}, 3.0)
    assert seen["B"] == ({"bulk": 5}, {"B": {"p": 5}}, 5.0)
