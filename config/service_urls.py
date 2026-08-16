"""
Centralized service URL configuration for Stockky 5-service architecture.
Replace the placeholder URLs after deploying to your 5 separate Render accounts.
"""
import os

# Placeholder URLs — easy global search & replace
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "https://STOCKKY-API-GATEWAY.onrender.com")
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com")
ANALYSIS_INTELLIGENCE_URL = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://STOCKKY-ANALYSIS-INTELLIGENCE.onrender.com")
DECISION_PREDICTION_URL = os.getenv("DECISION_PREDICTION_URL", "https://STOCKKY-DECISION-PREDICTION.onrender.com")
NOTIFICATION_SCHEDULER_URL = os.getenv("NOTIFICATION_SCHEDULER_URL", "https://STOCKKY-NOTIFICATION-SCHEDULER.onrender.com")

# Convenience aliases used by original code
TECHNICAL_URL = ANALYSIS_INTELLIGENCE_URL
FUNDAMENTAL_URL = ANALYSIS_INTELLIGENCE_URL
NEWS_URL = ANALYSIS_INTELLIGENCE_URL
EVENT_URL = ANALYSIS_INTELLIGENCE_URL
SENTIMENT_URL = ANALYSIS_INTELLIGENCE_URL
DECISION_URL = DECISION_PREDICTION_URL
PREDICTION_URL = DECISION_PREDICTION_URL
TRAINING_URL = DECISION_PREDICTION_URL
NOTIFICATION_URL = NOTIFICATION_SCHEDULER_URL
SCHEDULER_URL = NOTIFICATION_SCHEDULER_URL

def get_all_urls():
    return {
        "api_gateway": API_GATEWAY_URL,
        "market_data": MARKET_DATA_URL,
        "analysis_intelligence": ANALYSIS_INTELLIGENCE_URL,
        "decision_prediction": DECISION_PREDICTION_URL,
        "notification_scheduler": NOTIFICATION_SCHEDULER_URL,
    }
