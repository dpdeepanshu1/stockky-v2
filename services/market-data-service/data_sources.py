"""group278: one place to see which price source is serving and why the others are not (plan Phase A, item 4).

`GET /internal/data-sources` returns, for quotes and for history, the configured provider order (QUOTE_PROVIDER_ORDER /
HISTORY_PROVIDER_ORDER, default dhan,angelone,yfinance), the health of every source (state, last error and when,
cooldown seconds left) and `serving` = the first source in the order that is healthy right now. A request still walks
the order itself and falls to the next source on any failure; this report only explains it. Pure function over plain
dicts so it is testable without Dhan, AngelOne or Yahoo; never raises.
"""
from __future__ import annotations

from typing import Callable, Optional

_BAD = ("paused", "cooling", "not_configured", "disabled", "unknown")


def _dhan_entry(dhan: Optional[dict]) -> dict:
    if not isinstance(dhan, dict) or not dhan.get("enabled", False):
        return {"state": "disabled", "detail": "DHAN_DATA_ENABLED=0 or dhan_data not loaded"}
    creds = dhan.get("credentials") or {}
    client = dhan.get("client") or {}
    out = {"last_error": client.get("last_error"), "last_error_at": client.get("last_error_at"),
           "last_ok_at": client.get("last_ok_at"), "calls_last_minute": client.get("calls_last_minute"),
           "errors": client.get("errors"), "auth_errors": client.get("auth_errors"),
           "subscription_errors": client.get("subscription_errors")}
    if creds and creds.get("ok") is False:
        out.update(state="not_configured", detail="Dhan credentials missing or expired")
    elif client.get("paused"):
        out.update(state="paused", detail="Dhan client is paused after errors")
    else:
        br = client.get("breaker")
        if isinstance(br, dict) and str(br.get("state", "")).lower() == "open":
            out.update(state="paused", detail="Dhan circuit breaker is open")
        else:
            out["state"] = "ok"
    return out


def _cooldown_entry(left_s: float, configured: bool = True) -> dict:
    if not configured:
        return {"state": "not_configured", "detail": "credentials not set"}
    if left_s and left_s > 0:
        return {"state": "cooling", "cooldown_seconds_left": round(float(left_s), 1)}
    return {"state": "ok"}


def build_report(*, quote_order: list, history_order: list, dhan: Optional[dict],
                 angelone_quote_cooldown_s: float = 0.0, angelone_candle_cooldown_s: float = 0.0,
                 angelone_configured: bool = True, yfinance_cooldown_s: float = 0.0) -> dict:
    ang_q = _cooldown_entry(angelone_quote_cooldown_s, angelone_configured)
    ang_h = _cooldown_entry(angelone_candle_cooldown_s, angelone_configured)
    yf = _cooldown_entry(yfinance_cooldown_s)
    dh = _dhan_entry(dhan)
    quotes = {"dhan": dh, "angelone": ang_q, "yfinance": yf}
    history = {"dhan": dh, "angelone": ang_h, "yfinance": yf}

    def serving(order, table):
        for name in order:
            if table.get(name, {}).get("state") == "ok":
                return name
        return None

    qs, hs = serving(quote_order, quotes), serving(history_order, history)
    return {
        "quotes": {"order": list(quote_order), "serving": qs, "sources": quotes},
        "history": {"order": list(history_order), "serving": hs, "sources": history},
        "note": "serving = first source in the order that is healthy now; a request still falls to the next source "
                "on any failure, and NSE / last-close answers can follow when every source fails",
    }


def collect(get_dhan_status: Callable[[], dict], in_cooldown: Callable[[str], float],
            angelone_configured: Callable[[], bool], quote_order: Callable[[], list],
            history_order: Callable[[], list]) -> dict:
    """Gather live state through small callables (so main.py passes its own helpers) and build the report."""
    def safe(fn, default):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default
    return build_report(
        quote_order=safe(quote_order, ["dhan", "angelone", "yfinance"]),
        history_order=safe(history_order, ["dhan", "angelone", "yfinance"]),
        dhan=safe(get_dhan_status, None),
        angelone_quote_cooldown_s=safe(lambda: in_cooldown("angelone_quote"), 0.0),
        angelone_candle_cooldown_s=safe(lambda: in_cooldown("angelone_candle"), 0.0),
        angelone_configured=safe(angelone_configured, True),
        yfinance_cooldown_s=safe(lambda: in_cooldown("yfinance"), 0.0),
    )
