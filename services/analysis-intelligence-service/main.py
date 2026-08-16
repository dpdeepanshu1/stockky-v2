"""
Stockky Analysis Intelligence Service
Merges: technical-analysis + fundamental-analysis + market-sentiment + news-intelligence + event-tracker
"""
import os
import sys
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Add subdirs to path
BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "technical"))
sys.path.insert(0, os.path.join(BASE, "fundamental"))
sys.path.insert(0, os.path.join(BASE, "news"))
sys.path.insert(0, os.path.join(BASE, "event"))
sys.path.insert(0, os.path.join(BASE, "sentiment"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("analysis-intelligence-service")

app = FastAPI(
    title="Stockky Analysis Intelligence Service",
    version="1.0.0",
    description="Merged technical, fundamental, news, event, sentiment analysis"
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Import and mount the original apps' routes where possible
try:
    from technical.main import app as tech_app
    app.mount("/technical", tech_app)
    logger.info("Mounted technical analysis")
except Exception as e:
    logger.warning(f"Could not mount technical: {e}")

try:
    from fundamental.main import app as fund_app
    app.mount("/fundamental", fund_app)
    logger.info("Mounted fundamental analysis")
except Exception as e:
    logger.warning(f"Could not mount fundamental: {e}")

try:
    from news.main import app as news_app
    app.mount("/news", news_app)
    logger.info("Mounted news intelligence")
except Exception as e:
    logger.warning(f"Could not mount news: {e}")

try:
    from event.main import app as event_app
    app.mount("/event", event_app)
    logger.info("Mounted event tracker")
except Exception as e:
    logger.warning(f"Could not mount event: {e}")

try:
    from sentiment.main import app as sent_app
    app.mount("/sentiment", sent_app)
    logger.info("Mounted market sentiment")
except Exception as e:
    logger.warning(f"Could not mount sentiment: {e}")

@app.get("/")
def root():
    return {
        "service": "Stockky Analysis Intelligence Service",
        "version": "1.0.0",
        "status": "running",
        "modules": ["technical", "fundamental", "news", "event", "sentiment"],
        "endpoints": {
            "/technical/analyze/{symbol}": "Technical analysis",
            "/fundamental/analyze/{symbol}": "Fundamental analysis",
            "/news/analyze/{symbol}": "News sentiment",
            "/event/events/{symbol}": "Event tracker",
            "/sentiment/...": "Market sentiment",
            "/health": "Health check"
        }
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "analysis-intelligence-service"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8002)))
