"""round 205: /internal/feed-coverage says None (unknown), not False, while no scrip master is loaded."""
import main
import angelone_scrip_master as asm
from dhan_data import scrip_master as dsm


def _run(monkeypatch, ao_tok, ao_listed, dhan_loaded, dhan_id):
    monkeypatch.setattr(asm, "get_token", lambda s: ao_tok)
    monkeypatch.setattr(asm, "is_listed", lambda s: ao_listed)
    monkeypatch.setattr(dsm, "ensure_loaded", lambda block=False: dhan_loaded)
    monkeypatch.setattr(dsm, "security_id", lambda s: dhan_id)
    monkeypatch.setattr(main, "is_known_delisted", lambda s: False)
    return main.feed_coverage(main.FeedCoverageRequest(symbols=["HEG"]))["symbols"][0]


def test_nothing_loaded_everything_unknown(monkeypatch):
    r = _run(monkeypatch, None, None, False, None)
    assert r["angelone_token"] is None and r["dhan_security_id"] is None and r["primary_feed"] is None


def test_angelone_loaded_and_misses_is_false(monkeypatch):
    r = _run(monkeypatch, None, False, False, None)
    assert r["angelone_token"] is False and r["primary_feed"] is False


def test_one_feed_with_a_token_is_primary(monkeypatch):
    r = _run(monkeypatch, "99", True, False, None)
    assert r["angelone_token"] is True and r["primary_feed"] is True


def test_dhan_alone_is_primary(monkeypatch):
    r = _run(monkeypatch, None, None, True, ("1", 1))
    assert r["dhan_security_id"] is True and r["primary_feed"] is True


def test_delisted_is_never_primary(monkeypatch):
    monkeypatch.setattr(asm, "get_token", lambda s: "99")
    monkeypatch.setattr(asm, "is_listed", lambda s: True)
    monkeypatch.setattr(dsm, "ensure_loaded", lambda block=False: False)
    monkeypatch.setattr(main, "is_known_delisted", lambda s: True)
    assert main.feed_coverage(main.FeedCoverageRequest(symbols=["X"]))["symbols"][0]["primary_feed"] is False
