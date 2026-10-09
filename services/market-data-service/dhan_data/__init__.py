"""dhan_data - Dhan Data API as the primary market-data source (group 270).

Provider order is configuration: QUOTE_PROVIDER_ORDER / HISTORY_PROVIDER_ORDER (default "dhan,angelone,yfinance").
Everything here is called from market-data-service/main.py and every failure falls through to the next provider.

    quotes.get_quotes([...]) / quotes.get_quote(sym)   one Dhan call per second, up to 1000 symbols, shared by all callers
    history.fetch_candles(sym, interval, from, to)     daily / weekly (built from daily) / intraday
    start_background(get_universe)                     scrip master load, live poller, optional websocket
    status()                                           for GET /internal/dhan-status
"""
from __future__ import annotations

import threading
from typing import Callable, List

from . import (client, config, creds, depth20, history, live_poller, live_store, quotes, scrip_master,  # noqa: F401
               ws_feed)
from .errors import (DhanApiError, DhanAuthError, DhanError, DhanNoDataError, DhanNotConfigured,  # noqa: F401
                     DhanRateLimitError, DhanSubscriptionError)

_started = False
_start_lock = threading.Lock()


def history_available() -> bool:
    return client.available()


def start_background(get_universe: Callable[[], List[str]]) -> None:
    """Idempotent. Never raises and never blocks: the scrip master loads on its own thread."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    if not config.enabled():
        return
    scrip_master.ensure_loaded(block=False)
    if config.live_poller_enabled():
        live_poller.start(get_universe)
    ws_feed.start(get_universe)
    depth20.start()


def status() -> dict:
    return {
        "enabled": config.enabled(),
        "quote_order": config.quote_order(),
        "history_order": config.history_order(),
        "quote_position": config.quote_position(),
        "history_position": config.history_position(),
        "credentials": creds.status(),
        "scrip_master": scrip_master.status(),
        "client": client.stats(),
        "quotes": quotes.status(),
        "live_poller": live_poller.status(),
        "live_store": live_store.status(),
        "websocket": ws_feed.status(),
        "depth20": depth20.status(),
    }
