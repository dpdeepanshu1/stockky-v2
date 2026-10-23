"""control/catalog.py (notification-scheduler-service, E51): the alert switches the Real Money Calibration page may change. They only decide which Telegram / chat
alerts are sent; none of them places or blocks a trade, so every `looser` is neutral. All are read at call time from the environment (env_view)."""
from __future__ import annotations

from control.kit import Flag

SERVICE = "notification-scheduler-service"
_V = "control.env_view"
_G1, _G2 = "Chart-pattern digest (E29)", "Watchlist move alert (E28)"

CATALOG = (
    Flag("PATTERN_ALERTS_ENABLED", "Daily chart-pattern digest", _G1, "bool", looser="neutral", targets=((_V, "PATTERN_ALERTS_ENABLED"),), live=True, shipped="true",
         help="One message a day after the close listing breakouts, golden / death crosses and squeezes on your watchlist. Alert only."),
    Flag("PATTERN_ALERTS_TIME", "Digest time (IST)", _G1, "time", looser="neutral", targets=((_V, "PATTERN_ALERTS_TIME"),), live=True, shipped="16:05",
         help="When the digest is sent (HH:MM IST, after the 15:30 close)."),
    Flag("PATTERN_ALERTS_MAX_LINES", "Digest max lines", _G1, "int", looser="neutral", lo=3, hi=100, targets=((_V, "PATTERN_ALERTS_MAX_LINES"),), live=True, shipped="25",
         help="Longest digest, in lines."),
    Flag("PATTERN_ALERTS_MAX_SYMBOLS", "Digest max stocks scanned", _G1, "int", looser="neutral", lo=1, hi=500, targets=((_V, "PATTERN_ALERTS_MAX_SYMBOLS"),), live=True, shipped="80",
         help="How many watchlist stocks are checked per run."),
    Flag("WATCHLIST_MOVE_ALERTS_ENABLED", "Watchlist move alert", _G2, "bool", looser="neutral", targets=((_V, "WATCHLIST_MOVE_ALERTS_ENABLED"),), live=True, shipped="true",
         help="A message when a watchlist stock has moved the set percent since it was added. Alert only."),
    Flag("WATCHLIST_MOVE_ALERT_PCT", "Move that triggers the alert (%)", _G2, "float", looser="neutral", lo=0.5, hi=50, targets=((_V, "WATCHLIST_MOVE_ALERT_PCT"),), live=True, shipped="5.0",
         help="Percent move since added (up or down)."),
    Flag("WATCHLIST_MOVE_CHECK_MIN", "Check every (minutes)", _G2, "int", looser="neutral", lo=1, hi=240, targets=((_V, "WATCHLIST_MOVE_CHECK_MIN"),), live=True, shipped="15",
         help="How often prices are compared with the baseline."),
    Flag("WATCHLIST_MOVE_MAX_SYMBOLS", "Move alert max stocks", _G2, "int", looser="neutral", lo=1, hi=500, targets=((_V, "WATCHLIST_MOVE_MAX_SYMBOLS"),), live=True, shipped="80",
         help="How many watchlist stocks are compared per check."),
)
