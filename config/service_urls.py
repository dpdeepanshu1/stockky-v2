"""
Centralized service URL configuration for Stockky 5-service architecture.
Env vars override defaults. Path suffixes match the multi-app mounts on each host.
"""
import os

def _base(url: str) -> str:
    return (url or "").rstrip("/")


# ── Root hosts (5 Render services) ──────────────────────────────────────────
API_GATEWAY_URL = _base(os.getenv("API_GATEWAY_URL", "https://api-gateway-puwd.onrender.com"))
MARKET_DATA_URL = _base(os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com"))
ANALYSIS_INTELLIGENCE_URL = _base(
    os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
)
DECISION_PREDICTION_URL = _base(
    os.getenv("DECISION_PREDICTION_URL", "https://decision-prediction-service.onrender.com")
)
_notif_raw = os.getenv(
    "NOTIFICATION_SCHEDULER_URL",
    os.getenv("NOTIFICATION_URL", "https://notification-scheduler-service-x8vc.onrender.com/notification"),
)
NOTIFICATION_SCHEDULER_URL = _base(_notif_raw)

# ── Path-mounted micro-routes (override with full URL env if set) ───────────
TECHNICAL_URL = _base(
    os.getenv("TECHNICAL_URL", f"{ANALYSIS_INTELLIGENCE_URL}/technical")
)
FUNDAMENTAL_URL = _base(
    os.getenv("FUNDAMENTAL_URL", f"{ANALYSIS_INTELLIGENCE_URL}/fundamental")
)
NEWS_URL = _base(os.getenv("NEWS_URL", f"{ANALYSIS_INTELLIGENCE_URL}/news"))
EVENT_URL = _base(os.getenv("EVENT_URL", f"{ANALYSIS_INTELLIGENCE_URL}/event"))
SENTIMENT_URL = _base(
    os.getenv(
        "MARKET_SENTIMENT_URL",
        os.getenv("SENTIMENT_URL", f"{ANALYSIS_INTELLIGENCE_URL}/sentiment"),
    )
)
DECISION_URL = _base(os.getenv("DECISION_URL", f"{DECISION_PREDICTION_URL}/decision"))
PREDICTION_URL = _base(os.getenv("PREDICTION_URL", f"{DECISION_PREDICTION_URL}/prediction"))
TRAINING_URL = _base(os.getenv("TRAINING_URL", f"{DECISION_PREDICTION_URL}/training"))

if NOTIFICATION_SCHEDULER_URL.endswith("/notification"):
    NOTIFICATION_URL = NOTIFICATION_SCHEDULER_URL
else:
    NOTIFICATION_URL = _base(
        os.getenv("NOTIFICATION_URL", f"{NOTIFICATION_SCHEDULER_URL}/notification")
    )
SCHEDULER_URL = NOTIFICATION_URL

DATABASE_URL = os.getenv("DATABASE_URL", "")
TRAINING_DATABASE_URL = os.getenv("TRAINING_DATABASE_URL", DATABASE_URL)
VITE_API_URL = _base(os.getenv("VITE_API_URL", API_GATEWAY_URL))


def get_all_urls():
    return {
        "api_gateway": API_GATEWAY_URL,
        "market_data": MARKET_DATA_URL,
        "analysis_intelligence": ANALYSIS_INTELLIGENCE_URL,
        "decision_prediction": DECISION_PREDICTION_URL,
        "notification_scheduler": NOTIFICATION_URL,
        "technical": TECHNICAL_URL,
        "fundamental": FUNDAMENTAL_URL,
        "news": NEWS_URL,
        "event": EVENT_URL,
        "sentiment": SENTIMENT_URL,
        "decision": DECISION_URL,
        "prediction": PREDICTION_URL,
        "training": TRAINING_URL,
        "notification": NOTIFICATION_URL,
        "vite_api": VITE_API_URL,
    }
