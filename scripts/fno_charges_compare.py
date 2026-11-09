#!/usr/bin/env python3
"""fno_charges_compare.py - check the real-order probe's charges against Dhan's contract note, and save a short summary of the probe next to its file (round 265).

  summary only (no note yet):       python3 scripts/fno_charges_compare.py
  with the six lines from the note: python3 scripts/fno_charges_compare.py --brokerage 40 --stt 12.34 --exchange 5.67 --sebi 0.01 --gst 8.2 --stamp 0.45 [--total 66.9]
  an older probe:                   python3 scripts/fno_charges_compare.py --file probe_20261111_101500.json ...

Reads data/fno_real_probe/probe_<time>.json (the newest unless --file), prints what was sent, what Dhan said and what filled, compares the six charge lines (within Rs 0.05 each) and writes
probe_<time>_summary.txt beside it. It changes no rate and sends nothing. Exit code: 0 all six lines match | 1 a line differs | 2 no probe result / cannot compare | 3 note incomplete or not given.
The same check is on the F&O Overview (card "Real-order probe") and at GET /fno/probe.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fno-options-service"))
import config  # noqa: E402
from fno import probecheck  # noqa: E402
from fno.params import Params  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default="data/fno_real_probe")
    ap.add_argument("--file", default=None, help="probe_YYYYMMDD_HHMMSS.json (default: the newest)")
    for k in (*probecheck.LINES, "total"):
        ap.add_argument(f"--{k}", default=None, help=f"{k} from the contract note, in rupees")
    a = ap.parse_args(argv)
    name, probe = probecheck.load(a.dir, a.file)
    if probe is None:
        print(f"no probe result found in {a.dir} (run scripts/fno_real_order_probe.py --live first)")
        return 2
    summ = probecheck.summarize(probe, name or "")
    note = {k: getattr(a, k) for k in (*probecheck.LINES, "total") if getattr(a, k) is not None}
    cmp_ = probecheck.compare(probe, note, Params.from_config(config))
    text = probecheck.summary_text(summ, cmp_ if cmp_["verdict"] != "NOT_COMPARABLE" else None)
    print(text)
    try:
        print("saved:", probecheck.write_summary(a.dir, name or "", summ, cmp_ if cmp_["verdict"] != "NOT_COMPARABLE" else None))
    except OSError as e:
        print(f"(summary file not written: {e})")
    return {"MATCHED": 0, "DIFFERS": 1, "NOT_COMPARABLE": 2}.get(cmp_["verdict"], 3)


if __name__ == "__main__":
    sys.exit(main())
