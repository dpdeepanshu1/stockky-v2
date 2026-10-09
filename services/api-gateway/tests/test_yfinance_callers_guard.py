"""Guard (group 274, plan item from the Dhan data work): direct yfinance use is limited to a known list of files.

market-data-service is the single gateway for prices; Dhan -> AngelOne -> yfinance is decided there. A NEW file that imports
yfinance or calls yf.download / yf.Ticker bypasses that order (and the Yahoo 429 protections), so it fails here. If the new
use is deliberate (fundamentals / news / index data nothing else carries), add the file to ALLOWED on purpose.
"""
import os
import re

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SKIP_DIRS = {"tests", "__pycache__", ".venv", "venv", "node_modules", ".git", "site-packages"}
PAT = re.compile(r"^\s*(?:import yfinance|from yfinance\b)|\byf\.(?:download|Ticker)\b", re.M)

ALLOWED = {
    # fundamentals / news / events / index sentiment (Dhan carries none of these)
    "analysis-intelligence-service/event/main.py",
    "analysis-intelligence-service/fundamental/rate_limiter.py",
    "analysis-intelligence-service/news/main.py",
    "analysis-intelligence-service/sentiment/main.py",     # NIFTY/SENSEX now ask market-data first (group 270), yfinance for the rest
    "analysis-intelligence-service/technical/main.py",     # asks market-data first, yfinance fallback
    # api-gateway
    "api-gateway/data_feed.py",
    "api-gateway/ipo_scanner.py",
    "api-gateway/main.py",
    "api-gateway/rate_limiter.py",
    "api-gateway/surprise_premarket.py",
    "api-gateway/surprise_scanner.py",
    "api-gateway/symbol_aliases.py",
    # training / prediction (optional market-data source behind TRAINING_DATA_VIA_MARKET_DATA, off by default)
    "decision-prediction-service/prediction/pred_train.py",
    "decision-prediction-service/training/app.py",
    "decision-prediction-service/training/evaluate.py",
    "decision-prediction-service/training/trades.py",
    "decision-prediction-service/training/train.py",
    # market-data-service itself
    "market-data-service/main.py",
    "market-data-service/rate_limiter.py",
    "market-data-service/surprise_premarket.py",
    "market-data-service/yahoo_ws_feed.py",
    # others
    "notification-scheduler-service/scheduler/rate_limiter.py",
    "real-trade-service/candidate_engine/candidates.py",
    "real-trade-service/market_context/sector_signal.py",   # US sector ETFs - Dhan does not carry them
}


def _users():
    found = set()
    for d, dirs, files in os.walk(SERVICES):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(d, f)
            try:
                src = open(full, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if PAT.search(src):
                found.add(os.path.relpath(full, SERVICES).replace(os.sep, "/"))
    return found


def test_no_new_direct_yfinance_callers():
    new = sorted(_users() - ALLOWED)
    assert not new, f"new direct yfinance use outside the allowed list: {new} - go through market-data-service instead"


def test_allowed_list_has_no_stale_entries():
    stale = sorted(ALLOWED - _users())
    assert not stale, f"no longer use yfinance, remove from ALLOWED: {stale}"
