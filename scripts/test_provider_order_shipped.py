"""E163: the env files we ship must list the price providers as Dhan -> AngelOne -> yfinance (the order the plan asks for).
Reads the files only; the waterfall itself is tested in market-data-service/tests."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = [".env.example", ".env.oracle.recommended"]


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("var", ["QUOTE_PROVIDER_ORDER", "HISTORY_PROVIDER_ORDER"])
def test_shipped_order_is_dhan_angelone_yfinance(name, var):
    text = (ROOT / name).read_text(encoding="utf-8")
    m = re.search(rf"^{var}=(\S+)\s*$", text, re.M)
    assert m, f"{name}: {var} line missing"
    assert [p.strip() for p in m.group(1).split(",")] == ["dhan", "angelone", "yfinance"], f"{name}: {var}={m.group(1)}"


def test_code_default_matches_the_shipped_order():
    src = (ROOT / "services/market-data-service/dhan_data/config.py").read_text(encoding="utf-8")
    assert 'env_str(var, "dhan,angelone,yfinance")' in src
