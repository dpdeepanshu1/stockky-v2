"""group257: the "regime WEAK ... BUYs blocked" line names the top-N override that can still let one BUY through."""
import os
import re

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entry_engine", "entry.py")).read()


def test_log_mentions_override_and_compiles():
    assert "BUYs blocked%s." in SRC
    assert "ENTRY_REGIME_OVERRIDE_TOP_N" in SRC
    compile(SRC, "entry.py", "exec")


def test_note_only_when_override_enabled():
    m = re.search(r"_ovr_note = \((.*?)\) if _ovr_n > 0 else \"\"", SRC, re.S)
    assert m is not None
