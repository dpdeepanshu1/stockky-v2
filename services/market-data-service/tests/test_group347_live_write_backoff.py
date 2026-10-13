"""group347 (13 Oct 09:39 log): a live_quotes timeout opens a short back-off in which only held-position rows are written."""
import pytest

from dhan_data import live_store
from tests.test_group275_dhan_followups import _Engine, _rows, store  # noqa: F401


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE", "on")
    monkeypatch.setenv("DHAN_LIVE_WRITE_REFRESH_S", "0")


def test_timeout_opens_backoff_and_next_write_is_priority_only(store, monkeypatch):
    store.fail_on = {1, 2, 3}                      # chunk and both halves time out
    live_store._write_sync(_rows(100))
    assert live_store.in_backoff()
    store.fail_on = set()
    live_store.mark_priority(["S3", "S4"])
    n_before = len(store.executed)
    assert live_store._write_sync(_rows(100)) == 2
    assert store.executed[n_before:] == [2]


def test_nothing_is_written_in_backoff_without_priority_rows(store):
    live_store._backoff_until = 1e18
    assert live_store._write_sync(_rows(50)) == 0
    assert store.executed == []
    assert live_store.status()["backoff_skipped_batches"] == 1


def test_backoff_zero_disables_it(store, monkeypatch):
    monkeypatch.setenv("DHAN_LIVE_WRITE_BACKOFF_S", "0")
    store.fail_on = {1, 2, 3}
    live_store._write_sync(_rows(100))
    assert not live_store.in_backoff()


def test_rows_skipped_in_backoff_go_out_after_it_ends(store):
    live_store._backoff_until = 1e18
    live_store._write_sync(_rows(10))
    live_store._backoff_until = 0.0
    assert live_store._write_sync(_rows(10)) == 10
