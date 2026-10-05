"""group150: boolean column filters must compile to `= 1` on Oracle, never `IS 1` (ORA-00908).

Oracle has no native BOOLEAN here, so SQLAlchemy renders `col.is_(True)` as `col IS 1`. SQLite accepts
that, which is why the SQLite-backed tests never caught it; the VM log (2026-10-05) showed the
intraday news tick failing with ORA-00908 on every run.
"""
import os
import re

from sqlalchemy import select
from sqlalchemy.dialects import oracle

import models

_SERVICES_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _where_sql(expr) -> str:
    q = select(models.TradeGateState.id).where(expr)
    return str(q.compile(dialect=oracle.dialect())).split("WHERE", 1)[1].strip()


def test_equality_filter_compiles_to_equals_one_on_oracle():
    col = models.TradeGateState.afterhours_news_scan_enabled
    assert _where_sql(col == True).endswith("= 1")  # noqa: E712


def test_is_true_would_have_been_the_bug():
    col = models.TradeGateState.afterhours_news_scan_enabled
    assert " IS 1" in _where_sql(col.is_(True))


def test_intraday_news_body_uses_the_oracle_safe_form():
    src = open(os.path.join(_SERVICES_ROOT, "real-trade-service", "execution", "auto_pilot.py"), encoding="utf-8").read()
    assert "afterhours_news_scan_enabled.is_(True)" not in src
    assert "afterhours_news_scan_enabled == True" in src


def test_no_service_source_filters_a_column_with_is_true_or_false():
    pat = re.compile(r"\.(is_|isnot|is_not)\((True|False)\)")
    offenders = []
    for dirpath, dirs, files in os.walk(_SERVICES_ROOT):
        dirs[:] = [d for d in dirs if d not in ("tests", "node_modules", "__pycache__", ".git")]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                for n, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
                    if pat.search(line) and not line.lstrip().startswith("#"):
                        offenders.append(f"{os.path.relpath(path, _SERVICES_ROOT)}:{n}")
    assert not offenders, f"`.is_(True/False)` renders `IS 1`/`IS 0` on Oracle (ORA-00908): {offenders}"
