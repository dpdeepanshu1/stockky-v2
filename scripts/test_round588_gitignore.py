"""round 588: the local VM leftovers that a blanket `git add -A` pushed on 16 Nov (real-trade records, order-probe output, backups, scratch reports) are in
.gitignore, and the secret-bearing ones were never un-ignored. Stdlib only: reads the file, does not call git."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINES = [ln.strip() for ln in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]

MUST = ["data/fno_real_probe/", "data/fno_rolling/", "data/fno_rolling_long/", "*.bak", "*.bak.*", "*.backup-*", "/rt_*.json", "/rt_*.txt",
        "/headlines.csv", "/merged-log.txt", "/ps_report.txt", "/vm_full_check.sh", "services/analysis-intelligence-service/headlines.txt"]


def test_every_leftover_pattern_is_ignored():
    for m in MUST:
        assert m in LINES, m


def test_nothing_later_un_ignores_them():
    for ln in LINES:
        if ln.startswith("!"):
            assert not re.search(r"rt_|fno_real_probe|fno_rolling|\.bak|\.backup-", ln), ln


def test_env_rules_still_present():
    assert ".env.*" in LINES and "!.env.example" in LINES


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 3")
