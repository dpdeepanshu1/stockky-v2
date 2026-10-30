"""E267: the first AngelOne socket failure after a good connection is a WARNING; a repeat (backoff already grown) is an ERROR."""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "feed" / "ws_client.py").read_text(encoding="utf-8")


def test_first_failure_is_a_warning_and_a_repeat_is_an_error():
    assert "_lvl = logging.ERROR if backoff > config.ANGELONE_WS_RECONNECT_BACKOFF_S else logging.WARNING" in SRC
    assert 'logger.log(_lvl, "position-stocks WS: error (%s) — reconnecting in %ss", e, backoff)' in SRC
    assert 'logger.error("position-stocks WS: error (%s)' not in SRC


def test_backoff_is_reset_to_the_start_value_on_a_good_connection():
    assert "backoff = config.ANGELONE_WS_RECONNECT_BACKOFF_S  # reset on success" in SRC
