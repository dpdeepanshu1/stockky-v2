# Exact integration into fundamental/main.py

Your service already has peer relative + multi-quarter.
This only adds ranking table + unified fields for prediction.

## 1. Copy these 3 files into the same folder as main.py
- peer_multi_quarter.py
- peer_ranking.py
- wire_peer_multi_quarter.py

## 2. At the TOP of main.py (after other imports) add:
```python
from wire_peer_multi_quarter import apply_to_analyze_response
```

## 3. At the END of analyze(), replace the final `return { ... }` with:

```python
    result = {
        "symbol": symbol.upper(),
        "fundamental_score": score,
        "valuation": valuation_note,
        "sector": f.get("sector"),
        "peer_relative_score": peer_rel.get("score") if isinstance(peer_rel, dict) else None,
        "peer_relative": peer_rel if isinstance(peer_rel, dict) else None,
        "peer_list": peer_list,
        "sector_normalized": sector_norm or sector,
        "multi_quarter_score": multi_q_score,
        "multi_quarter_ok": multi_q_ok,
        "multi_quarter_detail": {
            "score": multi_q_score,
            "ok": multi_q_ok,
            "quarters_used": len(quarterly_earnings) if isinstance(quarterly_earnings, list) else 0,
        },
        "quality_score": quality_score,
        "industry": f.get("industry"),
        "reasons": reasons,
        "metrics": metrics,
        "raw": f,
        "fallback_used": fallback_used,
    }
    result = apply_to_analyze_response(
        symbol=symbol,
        analyze_payload=result,
        market_data_url=MARKET_DATA_URL,
    )
    return result
```

That is the complete wiring.
