"""round 581: three CI-warning fixes stay in place. (1) position-stocks pins pyotp like the other two services; (2) the service-test workflow sets
pytest-asyncio's loop-scope option on the command line (NOT in a pytest.ini, which the VM script without pytest-asyncio would flag as unknown);
(3) test_e336 builds its failing row with a function, not a new subclass of the declarative model on every call (SAWarning)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pyotp_is_pinned_in_all_three_services():
    for svc in ("position-stocks-service", "real-trade-service", "market-data-service"):
        txt = (ROOT / "services" / svc / "requirements.txt").read_text(encoding="utf-8")
        assert re.search(r"^pyotp==\d+\.\d+\.\d+", txt, re.M), svc


def test_workflow_sets_the_asyncio_loop_scope_on_every_service_pytest_call():
    wf = (ROOT / ".github/workflows/service-tests.yml").read_text(encoding="utf-8")
    calls = [ln for ln in wf.splitlines() if "--timeout=120" in ln]
    assert calls and all("asyncio_default_fixture_loop_scope=function" in ln for ln in calls)


def test_no_pytest_ini_sets_the_option_where_the_vm_script_would_not_know_it():
    assert not list((ROOT / "services").glob("*/pytest.ini"))


def test_e336_row_helper_does_not_subclass_the_model():
    src = (ROOT / "services/position-stocks-service/tests/test_e336_log_write_after_buy.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*class _Row\(", src, re.M) and "def _row(" in src


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 4")
