"""tests/test_nse_holiday_awareness.py - group 139: market-data-service knows NSE holidays.

Before: the AngelOne/Yahoo feeds polled 09:05-15:35 on weekday holidays (e.g. Fri 2026-10-02) and the
quote/history caches used the open-session TTLs. Pure stdlib + the real modules; no network.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import market_hours as mh

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


@pytest.fixture(autouse=True)
def _not_always_on(monkeypatch):
    monkeypatch.setattr(mh, "_ALWAYS_ON", False)


def _ist(y, mo, d, h=10, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


class TestIsNseHolidayIst:
    def test_gandhi_jayanti_friday_is_holiday(self):
        assert _ist(2026, 10, 2).weekday() == 4  # a Friday
        assert mh.is_nse_holiday_ist(_ist(2026, 10, 2)) is True

    def test_ordinary_weekday_is_not_holiday(self):
        assert mh.is_nse_holiday_ist(_ist(2026, 10, 1)) is False

    def test_utc_instant_is_converted_to_ist_date(self):
        # 2026-10-01 20:00 UTC = 2026-10-02 01:30 IST -> the holiday
        assert mh.is_nse_holiday_ist(datetime(2026, 10, 1, 20, 0, tzinfo=UTC)) is True
        # 2026-10-02 20:00 UTC = 2026-10-03 01:30 IST -> Saturday, not on the list
        assert mh.is_nse_holiday_ist(datetime(2026, 10, 2, 20, 0, tzinfo=UTC)) is False

    def test_naive_datetime_is_read_as_ist_wall_clock(self):
        assert mh.is_nse_holiday_ist(datetime(2026, 9, 14, 10, 0)) is True

    def test_no_argument_uses_now_and_returns_bool(self):
        assert isinstance(mh.is_nse_holiday_ist(), bool)

    def test_ganesh_chaturthi_in_list(self):
        assert "2026-09-14" in mh._NSE_HOLIDAYS_2026


class TestFeedWindowOnHolidays:
    def test_holiday_mid_session_is_closed(self):
        assert mh.is_feed_window_ist(_ist(2026, 10, 2, 10, 0)) is False

    def test_holiday_inside_slack_is_closed(self):
        assert mh.is_feed_window_ist(_ist(2026, 10, 2, 9, 6)) is False

    def test_day_before_and_after_holiday_still_open(self):
        assert mh.is_feed_window_ist(_ist(2026, 10, 1, 10, 0)) is True
        assert mh.is_feed_window_ist(_ist(2026, 10, 5, 10, 0)) is True

    def test_always_on_switch_still_overrides_a_holiday(self, monkeypatch):
        monkeypatch.setattr(mh, "_ALWAYS_ON", True)
        assert mh.is_feed_window_ist(_ist(2026, 10, 2, 10, 0)) is True

    def test_unknown_date_is_treated_as_trading_day(self):
        # 2027 is not in the list: a stale list errs towards polling, never towards silence.
        assert mh.is_feed_window_ist(_ist(2027, 1, 5, 10, 0)) is True


def _load_main_is_market_open():
    """main.py needs fastapi/yfinance/etc.; pull just is_market_open out of the real source so
    this runs anywhere, with the same imports it uses."""
    import ast
    import textwrap
    from datetime import time as dtime
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "is_market_open")
    code = textwrap.dedent(ast.get_source_segment(src, fn))
    return code, {"datetime": datetime, "dtime": dtime, "ZoneInfo": ZoneInfo}


def _is_market_open_at(monkeypatch, when_ist):
    code, ns = _load_main_is_market_open()

    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when_ist.astimezone(tz) if tz else when_ist

    ns["datetime"] = _FakeDT
    exec(code, ns)
    return ns["is_market_open"]()


class TestMainIsMarketOpen:
    def test_holiday_in_session_hours_is_closed(self, monkeypatch):
        assert _is_market_open_at(monkeypatch, _ist(2026, 10, 2, 11, 0)) is False

    def test_normal_weekday_in_session_hours_is_open(self, monkeypatch):
        assert _is_market_open_at(monkeypatch, _ist(2026, 10, 1, 11, 0)) is True

    def test_normal_weekday_after_close_is_closed(self, monkeypatch):
        assert _is_market_open_at(monkeypatch, _ist(2026, 10, 1, 16, 0)) is False

    def test_weekend_is_closed(self, monkeypatch):
        assert _is_market_open_at(monkeypatch, _ist(2026, 10, 3, 11, 0)) is False

    def test_failed_holiday_lookup_falls_back_to_hours_rule(self, monkeypatch):
        def boom(_now=None):
            raise RuntimeError("calendar broke")
        monkeypatch.setattr(mh, "is_nse_holiday_ist", boom)
        # Holiday date, but the lookup fails -> old behaviour (open by clock), never an exception.
        assert _is_market_open_at(monkeypatch, _ist(2026, 10, 2, 11, 0)) is True

    def test_get_cache_ttl_uses_long_ttl_on_a_holiday_source_check(self):
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text()
        assert "return 300 if is_market_open() else 21600" in src


class TestHolidayListsStayInSync:
    def test_sync_script_passes_for_the_whole_repo(self):
        script = Path(__file__).resolve().parents[3] / "scripts" / "check_holiday_lists_sync.py"
        if not script.exists():
            pytest.skip("repo-level script not present in this checkout")
        spec = importlib.util.spec_from_file_location("check_holiday_lists_sync", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "market-data-service/market_hours.py" in mod.FILES
        assert len(mod.FILES) >= 7
        assert mod.main() == 0


# ── group 142: the sync script covers every year from 2026 on, and reminds when a year has run out ──

def _load_sync_script():
    script = Path(__file__).resolve().parents[3] / "scripts" / "check_holiday_lists_sync.py"
    if not script.exists():
        pytest.skip("repo-level script not present in this checkout")
    spec = importlib.util.spec_from_file_location("check_holiday_lists_sync_g142", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHolidaySyncScriptYears:
    def test_extracts_string_and_date_forms_for_2026_and_2027_but_not_2025(self):
        mod = _load_sync_script()
        text = (
            '_NSE_HOLIDAYS_2026 = {\n  "2026-01-26", "2027-01-26",\n  "2025-12-25",\n}\n'
            '_NSE_HOLIDAYS = {\n  date(2026, 3, 3), date(2027, 3, 22), date(2025, 2, 26),\n}\n'
        )
        assert mod._extract_dates(text) == {"2026-01-26", "2027-01-26", "2026-03-03", "2027-03-22"}

    def test_dates_in_comments_outside_the_literal_are_ignored(self):
        mod = _load_sync_script()
        text = '# old wrong date "2027-05-05"\n_NSE_HOLIDAYS_2026 = {\n  "2026-01-26",\n}\n'
        assert mod._extract_dates(text) == {"2026-01-26"}

    def test_old_helper_name_still_works(self):
        mod = _load_sync_script()
        assert mod._extract_2026_dates is mod._extract_dates

    def test_drift_in_a_2027_date_is_reported(self, tmp_path, monkeypatch, capsys):
        mod = _load_sync_script()
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text('_NSE_HOLIDAYS_2026 = {\n "2026-01-26", "2027-01-26",\n}\n')
        b.write_text('_NSE_HOLIDAYS_2026 = {\n "2026-01-26",\n}\n')
        monkeypatch.setattr(mod, "FILES", {"a": a, "b": b})
        assert mod.main() == 1
        out = capsys.readouterr().out
        assert "DRIFT in b" in out and "2027-01-26" in out

    def test_agreeing_multi_year_lists_pass(self, tmp_path, monkeypatch, capsys):
        mod = _load_sync_script()
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text('_NSE_HOLIDAYS_2026 = {\n "2026-01-26", "2027-01-26",\n}\n')
        b.write_text('HOLIDAYS_2026 = [\n "2027-01-26", "2026-01-26",\n]\n')
        monkeypatch.setattr(mod, "FILES", {"a": a, "b": b})
        assert mod.main() == 0
        assert "(2026, 2027)" in capsys.readouterr().out


class TestYearCoverageNote:
    def _d(self, y, m, d):
        from datetime import date
        return date(y, m, d)

    def test_no_note_mid_year_when_this_year_is_covered(self):
        mod = _load_sync_script()
        assert mod.year_coverage_note({"2026-01-26"}, self._d(2026, 6, 1)) is None

    def test_reminder_from_october_when_next_year_missing(self):
        mod = _load_sync_script()
        note = mod.year_coverage_note({"2026-01-26"}, self._d(2026, 10, 1))
        assert note and "2027" in note and "December" in note

    def test_no_reminder_once_next_year_is_present(self):
        mod = _load_sync_script()
        assert mod.year_coverage_note({"2026-01-26", "2027-01-26"}, self._d(2026, 12, 20)) is None

    def test_warns_when_the_current_year_has_no_dates_at_all(self):
        mod = _load_sync_script()
        note = mod.year_coverage_note({"2026-01-26"}, self._d(2027, 1, 4))
        assert note and "no holiday dates for 2027" in note and "trading day" in note

    def test_september_does_not_remind_yet(self):
        mod = _load_sync_script()
        assert mod.year_coverage_note({"2026-01-26"}, self._d(2026, 9, 30)) is None
