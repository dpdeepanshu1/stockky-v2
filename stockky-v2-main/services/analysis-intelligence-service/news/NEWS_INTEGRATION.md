# News quality integration

1. Copy `news_quality.py` next to `news/main.py`.

2. In `news/main.py` analyze/news endpoint, prefer:

```python
from news_quality import build_news_response, expand_keywords

# optional: pass your existing LLM helper
def _llm(system, user):
    # reuse Groq/Gemini/HF already in the service
    ...

@app.get("/analyze/{symbol}")
def analyze(symbol: str, company_name: str | None = None):
    # resolve company name from market-data if needed
    payload = build_news_response(symbol, company_name=company_name, llm_summarizer=_llm)
    return payload
```

Or merge: call `expand_keywords` inside your existing `_match_keywords`, and `summarize_headlines` for the summary field.
