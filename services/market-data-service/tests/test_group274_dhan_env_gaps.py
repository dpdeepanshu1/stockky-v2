"""group 274 (merge of the Dhan branch): env reads are blank-safe and every Dhan variable the code reads is documented."""
import os
import re

import pytest

from dhan_data import config

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SERVICE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.mark.parametrize("raw,expected", [("", 60), ("   ", 60), ("abc", 60), ("nan", 60), ("0", 1), ("30", 30), (" 15 ", 15)])
def test_hourly_max_days_is_blank_safe(monkeypatch, raw, expected):
    monkeypatch.setenv("DHAN_HOURLY_MAX_DAYS", raw)
    assert config.hourly_max_days() == expected


def test_hourly_max_days_unset_uses_default(monkeypatch):
    monkeypatch.delenv("DHAN_HOURLY_MAX_DAYS", raising=False)
    assert config.hourly_max_days() == 60


def test_main_has_no_bare_int_or_float_env_read_for_dhan_vars():
    src = open(os.path.join(SERVICE, "main.py"), encoding="utf-8").read()
    bad = re.findall(r"(?:int|float)\(\s*os\.getenv\(\s*[\"']DHAN_[A-Z_]+", src)
    assert not bad, f"bare int()/float() on a DHAN_ env var in main.py: {bad}"


def _vars_read_by_dhan_code():
    names = set()
    files = [os.path.join(SERVICE, "dhan_data", f) for f in os.listdir(os.path.join(SERVICE, "dhan_data")) if f.endswith(".py")]
    files.append(os.path.join(SERVICE, "main.py"))
    for f in files:
        names |= set(re.findall(r"[\"'](DHAN_[A-Z0-9_]+|QUOTE_PROVIDER_ORDER|HISTORY_PROVIDER_ORDER)[\"']", open(f, encoding="utf-8").read()))
    return names


@pytest.mark.parametrize("envfile", [".env.example", ".env.oracle.recommended"])
def test_every_dhan_variable_the_code_reads_is_documented(envfile):
    text = open(os.path.join(ROOT, envfile), encoding="utf-8").read()
    missing = sorted(n for n in _vars_read_by_dhan_code() if not re.search(rf"^#?\s*{n}=", text, re.M))
    assert not missing, f"{envfile} does not mention: {missing}"
