"""Round 625 (E651): scripts/add_nse_holidays.py adds a year's NSE holidays to all eight copies in one step.

Every test works on a temp copy of the eight real files, so the repo itself is never touched.
"""
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import add_nse_holidays as A

CIRC = "NSE/CMTR/99999 dated 2026-12-20"
ITEMS = ["2027-01-26=Republic Day", "2027-03-22=Holi", "2027-10-29=Diwali Laxmi Pujan"]


def _copy_repo():
    root = Path(tempfile.mkdtemp(prefix="hol_"))
    for rel in A.REL_FILES:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(A.REPO_ROOT / rel, dst)
    return root


def _dates(root, rel):
    return A.SYNC._extract_dates((root / rel).read_text(encoding="utf-8"))


def _run(root, items=ITEMS, apply=True, circular=CIRC, year=2027):
    lines = []
    rc = A.run(root, year, items, circular, apply, out=lines.append)
    return rc, "\n".join(lines)


def test_eight_copies_are_covered():
    assert len(A.REL_FILES) == 8 and len(set(A.REL_FILES)) == 8


def test_dry_run_changes_nothing():
    root = _copy_repo()
    before = {r: (root / r).read_text(encoding="utf-8") for r in A.REL_FILES}
    rc, out = _run(root, apply=False)
    assert rc == 0 and "DRY RUN" in out
    assert {r: (root / r).read_text(encoding="utf-8") for r in A.REL_FILES} == before


def test_apply_adds_the_same_dates_to_every_copy_in_its_own_format():
    root = _copy_repo()
    rc, out = _run(root)
    assert rc == 0 and "all 8 copies agree" in out
    want = {"2027-01-26", "2027-03-22", "2027-10-29"}
    for rel in A.REL_FILES:
        assert want <= _dates(root, rel), rel
    gw = (root / "services/api-gateway/nse_holidays.py").read_text(encoding="utf-8")
    assert "date(2027, 1, 26)," in gw and '"2027-01-26"' not in gw      # the gateway keeps date() tuples
    rt = (root / "services/real-trade-service/tz_utils.py").read_text(encoding="utf-8")
    assert '"2027-01-26",  # Republic Day' in rt
    assert CIRC in rt and "# 2028 PENDING" in rt and "# 2027 PENDING" not in rt
    # the sync script itself agrees on the temp copy: every file holds the identical date set
    sets = [_dates(root, r) for r in A.REL_FILES]
    assert all(s == sets[0] for s in sets)


def test_second_run_is_a_no_op_and_the_next_year_marker_works():
    root = _copy_repo()
    assert _run(root)[0] == 0
    rc, out = _run(root)
    assert rc == 0 and "Nothing to do" in out
    rc, out = _run(root, ["2028-01-26=Republic Day"], year=2028)
    assert rc == 0
    for rel in A.REL_FILES:
        assert {"2027-01-26", "2028-01-26"} <= _dates(root, rel)


def test_no_circular_no_write():
    root = _copy_repo()
    before = (root / "services/real-trade-service/tz_utils.py").read_text(encoding="utf-8")
    for circ in ("", "   "):
        rc, out = _run(root, circular=circ)
        assert rc == 1 and "--circular is required" in out
    assert (root / "services/real-trade-service/tz_utils.py").read_text(encoding="utf-8") == before


def test_bad_input_is_refused_before_anything_is_written():
    root = _copy_repo()
    for items, why in ((["2026-12-25=Christmas"], "is not in 2027"), (["2027-02-30=x"], "not a real date"),
                       (["Republic Day"], "not a date line")):
        rc, out = _run(root, items)
        assert rc == 1 and why in out
    assert not any("2027" in " ".join(sorted(_dates(root, r))) for r in A.REL_FILES)


def test_weekend_dates_are_skipped_and_weekday_only_input_is_refused():
    root = _copy_repo()
    sat = date(2027, 10, 2)
    assert sat.weekday() == 5
    rc, out = _run(root, ["2027-10-02=Gandhi Jayanti", "2027-01-26=Republic Day"])
    assert rc == 0 and "skipped 2027-10-02 (Saturday)" in out
    assert "2027-10-02" not in _dates(root, A.REL_FILES[0])
    root2 = _copy_repo()
    rc, out = _run(root2, ["2027-10-02=Gandhi Jayanti"])
    assert rc == 1 and "no weekday dates" in out


def test_all_or_nothing_when_one_file_lost_its_marker():
    root = _copy_repo()
    bad = root / "services/market-data-service/market_hours.py"
    bad.write_text(bad.read_text(encoding="utf-8").replace("2027 PENDING", "xx"), encoding="utf-8")
    before = {r: (root / r).read_text(encoding="utf-8") for r in A.REL_FILES}
    rc, out = _run(root)
    assert rc == 1 and "CANNOT UPDATE services/market-data-service/market_hours.py" in out and "Nothing was written" in out
    assert {r: (root / r).read_text(encoding="utf-8") for r in A.REL_FILES} == before


def test_label_cannot_break_the_bracket_matching():
    assert A.clean_label('Holi [x] {y} "z"\nnext') == "Holi x y z next"
    root = _copy_repo()
    rc, _ = _run(root, ['2027-03-22=Holi ] } ) evil'])
    assert rc == 0
    sets = [_dates(root, r) for r in A.REL_FILES]
    assert all(s == sets[0] for s in sets) and "2027-03-22" in sets[0]


def test_the_real_repo_files_all_carry_the_marker_today():
    """If this fails the helper cannot run in December: a file lost its '2027 PENDING' marker (or 2027 was already added)."""
    for rel in A.REL_FILES:
        text = (A.REPO_ROOT / rel).read_text(encoding="utf-8")
        has_marker = "2027 PENDING" in text
        has_2027 = any(d.startswith("2027") for d in A.SYNC._extract_dates(text))
        assert has_marker or has_2027, rel
