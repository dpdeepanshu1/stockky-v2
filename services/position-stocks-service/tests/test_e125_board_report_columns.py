"""E125: GET /board/shadow-report reads only the columns the report uses (tuples), not up to 60,000 full ORM rows. Source check (no database needed)."""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "main.py").read_text()


def _route():
    i = SRC.index("def _board_report_compute(")  # E237: the query moved into this helper, still ahead of the route
    return SRC[i:SRC.index('@app.get("/board")', i)]


def test_shadow_report_selects_columns_not_entities():
    body = _route()
    assert "db.query(ScalpBoardLog)" not in body
    cols = ["kind", "day", "symbol", "side", "rank", "conviction", "note", "zone_status", "ts", "params_version", "fwd15_pct", "fwd30_pct", "fwd60_pct", "fwd_close_pct"]
    q = body[body.index("db.query("):body.index(".limit(60000)")]
    for c in cols:
        assert f"_L.{c}" in q, c


def test_every_attribute_the_dict_reads_is_selected():
    body = _route()
    q = body[body.index("db.query("):body.index(".limit(60000)")]
    used = set(re.findall(r"\br\.(\w+)", body[body.index("dicts = ["):body.index("out = board_report")]))
    assert used and all(f"_L.{u}" in q for u in used), used - set(re.findall(r"_L\.(\w+)", q))
