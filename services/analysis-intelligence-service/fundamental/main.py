from peers import peers_for, normalize_sector, peer_relative_score, average_metrics, SYMBOL_SECTOR
"""
Fundamental Analysis Service
------------------------------
Single responsibility: turn raw fundamental data (fetched from Market Data
Service) into a fundamental quality score (0-100) and readable reasons.
"""
import os
import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fundamental-analysis-service")

# MUST point to your market-data-service
MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")

# ── Sector-relative valuation (from first version) ───────────────────
# A P/E of 35 is expensive for a bank, ordinary for FMCG, and cheap for a
# high-growth software company. Scoring P/E against one fixed threshold
# for every stock (the previous behavior) systematically over-penalizes
# expensive-by-nature sectors and under-penalizes cheap-by-nature ones.
#
# These are typical/approximate historical NSE sector P/E ranges, not a
# live-computed median across actual current sector constituents — that
# would need a proper sector-constituent price feed this service doesn't
# have. Treat this as a reasonable prior that replaces one-size-fits-all
# thresholds, not a precise live benchmark. yfinance's `sector` field
# values are the dict keys.
SECTOR_TYPICAL_PE = {
    "Technology": 26,
    "Financial Services": 17,
    "Healthcare": 30,
    "Consumer Defensive": 45,
    "Consumer Cyclical": 28,
    "Industrials": 24,
    "Basic Materials": 12,
    "Energy": 11,
    "Utilities": 16,
    "Real Estate": 22,
    "Communication Services": 20,
}
DEFAULT_TYPICAL_PE = 22  # broad-market fallback when sector is unknown or not in the table

def _sector_relative_pe_score(pe_ratio, sector: str | None):
    """Returns (score_delta, reason). Compares P/E to the sector's typical
    range instead of one fixed threshold for every stock."""
    if pe_ratio is None or pe_ratio <= 0:
        return 0, None
    typical = SECTOR_TYPICAL_PE.get(sector, DEFAULT_TYPICAL_PE)
    ratio = pe_ratio / typical
    sector_label = sector or "broad market"
    if ratio < 0.7:
        return 8, f"P/E at {pe_ratio:.1f} is well below the {sector_label} typical range (~{typical}) — attractive for the sector"
    elif ratio < 0.9:
        return 4, f"P/E at {pe_ratio:.1f} is below the {sector_label} typical range (~{typical})"
    elif ratio <= 1.15:
        return 0, f"P/E at {pe_ratio:.1f} is in line with the {sector_label} typical range (~{typical})"
    elif ratio <= 1.5:
        return -4, f"P/E at {pe_ratio:.1f} is above the {sector_label} typical range (~{typical})"
    else:
        return -8, f"P/E at {pe_ratio:.1f} is well above the {sector_label} typical range (~{typical}) — priced for a lot of growth"

def _multi_quarter_consistency(earnings_list):
    """Return (score 0-100, ok_flag) if 2-3 consecutive quarters show positive growth."""
    if not earnings_list or len(earnings_list) < 2:
        return 50.0, False
    grows = []
    for i in range(1, min(4, len(earnings_list))):
        try:
            prev, cur = float(earnings_list[i]), float(earnings_list[i-1])
            if prev == 0:
                grows.append(0)
            else:
                grows.append(1 if cur > prev else -1)
        except Exception:
            grows.append(0)
    pos = sum(1 for g in grows if g > 0)
    if pos >= 3:
        return 85.0, True
    if pos >= 2:
        return 70.0, True
    if pos == 1:
        return 45.0, False
    return 30.0, False

app = FastAPI(title="Stockky Fundamental Analysis Service", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
def root():
    return {
        "service": "Stockky Fundamental Analysis Service",
        "status": "running",
        "endpoints": {
            "/health": "GET – health check",
            "/analyze/{symbol}": "GET – fundamental score for a symbol",
            "/docs": "Swagger UI documentation",
        },
    }

@app.get("/health")
def health():
    return {"status": "ok", "service": "fundamental-analysis-service"}

def _pct(x):
    if x is None:
        return None
    return x * 100 if abs(x) < 5 else x

@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    f = {}
    fallback_used = False
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/fundamentals/{symbol}", timeout=60)
        resp.raise_for_status()
        f = resp.json()
        if not f or not isinstance(f, dict):
            f = {}
    except httpx.TimeoutException:
        logger.warning(f"Market data service timed out for {symbol}")
        fallback_used = True
    except httpx.HTTPStatusError as e:
        logger.error(f"Market data service error for {symbol}: {e}")
        if e.response.status_code >= 500:
            fallback_used = True
        else:
            raise HTTPException(status_code=e.response.status_code, detail=f"Market data service error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        fallback_used = True

    if not f or not isinstance(f, dict):
        f = {}

    # Extract metrics (including new ones)
    pe_ratio = f.get("pe_ratio")
    forward_pe = f.get("forward_pe")
    revenue_growth = f.get("revenue_growth")
    earnings_growth = f.get("earnings_growth")
    roe = f.get("roe")
    roce = f.get("roce")
    debt_to_equity = f.get("debt_to_equity")
    free_cashflow = f.get("free_cashflow")
    profit_margins = f.get("profit_margins")
    opm = f.get("opm")
    current_ratio = f.get("current_ratio")
    interest_coverage = f.get("interest_coverage")
    held_percent_institutions = f.get("held_percent_institutions")
    price_to_book = f.get("price_to_book")
    pe_growth = f.get("pe_growth")
    ev_ebitda = f.get("ev_ebitda")
    promoter_holding = f.get("promoter_holding")
    promoter_pledging = f.get("promoter_pledging")
    sector = f.get("sector")
    market_cap = f.get("market_cap")
    # Additional for multi-quarter
    quarterly_earnings = f.get("quarterly_earnings") or f.get("earnings_quarters")

    metrics = {
        "pe_ratio": pe_ratio,
        "forward_pe": forward_pe,
        "revenue_growth": revenue_growth,
        "earnings_growth": earnings_growth,
        "roe": roe,
        "roce": roce,
        "debt_to_equity": debt_to_equity,
        "free_cashflow": free_cashflow,
        "profit_margins": profit_margins,
        "opm": opm,
        "current_ratio": current_ratio,
        "interest_coverage": interest_coverage,
        "institutional_holding": held_percent_institutions,
        "price_to_book": price_to_book,
        "pe_growth": pe_growth,
        "ev_ebitda": ev_ebitda,
        "promoter_holding": promoter_holding,
        "promoter_pledging": promoter_pledging,
    }

    # --- Determine fallback ---
    has_any_data = False
    if any(v is not None for v in [pe_ratio, sector, market_cap, revenue_growth, roe]):
        has_any_data = True
    if not has_any_data and not any(v is not None for v in [revenue_growth, earnings_growth, roe, debt_to_equity, free_cashflow, profit_margins]):
        fallback_used = True
    else:
        fallback_used = False

    score = 50
    reasons = []
    valuation_note = "fair"

    # --- Valuation Multiples (using sector-relative PE from first version) ---
    if pe_ratio is not None:
        if pe_ratio < 0:
            score -= 10
            valuation_note = "unprofitable (negative P/E)"
            reasons.append("Negative P/E — company currently unprofitable")
        else:
            pe_delta, pe_reason = _sector_relative_pe_score(pe_ratio, sector)
            score += pe_delta
            if pe_reason:
                reasons.append(pe_reason)
            # Determine valuation_note based on pe_delta
            if pe_delta > 4:
                valuation_note = "attractive"
            elif pe_delta < -4:
                valuation_note = "expensive"
            else:
                valuation_note = "fair"

        if forward_pe and pe_ratio and forward_pe < pe_ratio:
            score += 4
            reasons.append("Forward P/E lower than trailing P/E — earnings expected to grow into valuation")

    # PEG (P/E to Growth)
    if pe_growth is not None:
        if pe_growth < 1.0:
            score += 6
            reasons.append(f"PEG at {pe_growth:.2f} (<1.0) — undervalued relative to growth")
        elif pe_growth < 2.0:
            score += 2
            reasons.append(f"PEG at {pe_growth:.2f} — reasonably valued")
        else:
            score -= 2
            reasons.append(f"PEG at {pe_growth:.2f} (>2.0) — overvalued relative to growth")

    # EV/EBITDA
    if ev_ebitda is not None:
        if ev_ebitda < 10:
            score += 4
            reasons.append(f"EV/EBITDA at {ev_ebitda:.1f} — attractively valued")
        elif ev_ebitda > 20:
            score -= 4
            reasons.append(f"EV/EBITDA at {ev_ebitda:.1f} — richly valued")

    # P/B
    if price_to_book is not None:
        if price_to_book < 1.5:
            score += 3
            reasons.append(f"P/B at {price_to_book:.2f} — low relative to book value")
        elif price_to_book > 5:
            score -= 3
            reasons.append(f"P/B at {price_to_book:.2f} — high relative to book value")

    # --- Profitability ---
    if revenue_growth is not None:
        if revenue_growth > 15:
            score += 12
            reasons.append(f"Revenue growing {revenue_growth:.1f}% YoY — strong expansion")
        elif revenue_growth > 5:
            score += 5
            reasons.append(f"Revenue growing {revenue_growth:.1f}% YoY — steady growth")
        elif revenue_growth < 0:
            score -= 12
            reasons.append(f"Revenue declining {revenue_growth:.1f}% YoY — red flag")
        else:
            reasons.append(f"Revenue growth flat at {revenue_growth:.1f}%")

    if earnings_growth is not None:
        if earnings_growth > 15:
            score += 12
            reasons.append(f"Earnings growing {earnings_growth:.1f}% YoY — profitable expansion")
        elif earnings_growth < 0:
            score -= 12
            reasons.append(f"Earnings declining {earnings_growth:.1f}% YoY — margin or demand pressure")

    if roe is not None:
        if roe > 20:
            score += 10
            reasons.append(f"ROE at {roe:.1f}% — excellent capital efficiency")
        elif roe > 12:
            score += 5
            reasons.append(f"ROE at {roe:.1f}% — healthy capital efficiency")
        elif roe < 8:
            score -= 8
            reasons.append(f"ROE at {roe:.1f}% — weak returns on equity")

    if roce is not None:
        if roce > 20:
            score += 8
            reasons.append(f"ROCE at {roce:.1f}% — excellent return on capital employed")
        elif roce > 12:
            score += 4
            reasons.append(f"ROCE at {roce:.1f}% — healthy capital efficiency")
        else:
            score -= 4
            reasons.append(f"ROCE at {roce:.1f}% — weak return on capital")

    if profit_margins is not None:
        if profit_margins > 15:
            score += 8
            reasons.append(f"Net margin at {profit_margins:.1f}% — strong pricing power/efficiency")
        elif profit_margins < 5:
            score -= 8
            reasons.append(f"Net margin at {profit_margins:.1f}% — thin profitability")

    if opm is not None:
        if opm > 20:
            score += 6
            reasons.append(f"Operating margin at {opm:.1f}% — strong operational efficiency")
        elif opm < 8:
            score -= 6
            reasons.append(f"Operating margin at {opm:.1f}% — low operational efficiency")

    # --- Solvency & Liquidity ---
    if debt_to_equity is not None:
        if debt_to_equity < 0.5:
            score += 8
            reasons.append(f"Debt/Equity at {debt_to_equity:.2f} — low leverage, low risk")
        elif debt_to_equity > 1.5:
            score -= 12
            reasons.append(f"Debt/Equity at {debt_to_equity:.2f} — high leverage, elevated risk")
        else:
            reasons.append(f"Debt/Equity at {debt_to_equity:.2f} — moderate leverage")

    if current_ratio is not None:
        if current_ratio > 1.5:
            score += 4
            reasons.append(f"Current ratio at {current_ratio:.2f} — good short-term liquidity")
        elif current_ratio < 1.0:
            score -= 6
            reasons.append(f"Current ratio at {current_ratio:.2f} — poor short-term liquidity")

    if interest_coverage is not None:
        if interest_coverage > 3:
            score += 5
            reasons.append(f"Interest coverage at {interest_coverage:.1f} — healthy ability to service debt")
        elif interest_coverage < 1.5:
            score -= 8
            reasons.append(f"Interest coverage at {interest_coverage:.1f} — risky debt servicing")

    # --- Cash Flow (with FCF yield from first version) ---
    if free_cashflow is not None:
        if market_cap and market_cap > 0:
            fcf_yield = free_cashflow / market_cap * 100
            metrics["fcf_yield"] = round(fcf_yield, 2)
            if fcf_yield > 5:
                score += 10
                reasons.append(f"FCF yield at {fcf_yield:.1f}% — strong cash generation relative to valuation")
            elif fcf_yield > 0:
                score += 5
                reasons.append(f"FCF yield at {fcf_yield:.1f}% — positive but modest cash generation")
            else:
                score -= 10
                reasons.append(f"FCF yield at {fcf_yield:.1f}% — burning cash relative to its size")
        elif free_cashflow > 0:
            score += 8
            reasons.append("Positive free cash flow — self-funding operations and growth")
        else:
            score -= 10
            reasons.append("Negative free cash flow — relies on external financing")

    # Earnings yield (1/PE) as a complementary signal
    if pe_ratio is not None and pe_ratio > 0:
        earnings_yield = 1 / pe_ratio * 100
        metrics["earnings_yield"] = round(earnings_yield, 2)
        is_moderate_growth = revenue_growth is not None and 0 <= revenue_growth <= 15
        if is_moderate_growth:
            if earnings_yield > 6:
                score += 6
                reasons.append(f"Earnings yield at {earnings_yield:.1f}% — attractive for a moderate-growth company")
            elif earnings_yield < 3:
                score -= 4
                reasons.append(f"Earnings yield at {earnings_yield:.1f}% — low for a moderate-growth company")

    # --- Institutional / Promoter Holding ---
    if held_percent_institutions is not None and held_percent_institutions > 40:
        score += 6
        reasons.append(f"Institutions hold {held_percent_institutions:.1f}% — strong smart-money confidence")

    if promoter_holding is not None:
        if promoter_holding > 50:
            score += 4
            reasons.append(f"Promoter holding at {promoter_holding:.1f}% — strong management conviction")
        elif promoter_holding < 25:
            score -= 4
            reasons.append(f"Promoter holding at {promoter_holding:.1f}% — low management ownership")

    if promoter_pledging is not None:
        if promoter_pledging > 20:
            score -= 10
            reasons.append(f"Promoter pledging at {promoter_pledging:.1f}% — high risk of margin calls")
        elif promoter_pledging > 10:
            score -= 5
            reasons.append(f"Promoter pledging at {promoter_pledging:.1f}% — moderate risk")

    # --- Multi-quarter consistency (from second version) ---
    multi_q_score, multi_q_ok = 50.0, False
    try:
        if isinstance(quarterly_earnings, list) and len(quarterly_earnings) >= 2:
            multi_q_score, multi_q_ok = _multi_quarter_consistency(quarterly_earnings)
        elif earnings_growth is not None and revenue_growth is not None:
            if earnings_growth > 0 and revenue_growth > 0:
                multi_q_score, multi_q_ok = 70.0, True
                reasons.append("Revenue and earnings growth both positive (multi-quarter proxy)")
            elif earnings_growth > 0 or revenue_growth > 0:
                multi_q_score = 55.0
        if multi_q_ok:
            score += 4
    except Exception as _mq:
        logger.warning("multi-quarter check failed: %s", _mq)

    # ── Peer-relative metrics (from second version) ──
    sector_norm = normalize_sector(sector, symbol) if 'normalize_sector' in globals() else sector
    peer_list = peers_for(symbol, sector_norm) if 'peers_for' in globals() else []
    peer_rel = {"score": 50.0, "components": {}, "note": "peers_not_fetched"}
    try:
        peer_rows = []
        for ps in peer_list[:4]:
            try:
                pr = httpx.get(f"{MARKET_DATA_URL}/fundamentals/{ps}", timeout=8)
                if pr.status_code == 200:
                    peer_rows.append(pr.json())
            except Exception:
                continue
        if peer_rows:
            peer_avg = average_metrics(peer_rows) if 'average_metrics' in globals() else {}
            peer_rel = peer_relative_score(metrics, peer_avg) if 'peer_relative_score' in globals() else {"score": 50.0}
            adj = (float(peer_rel.get("score", 50)) - 50.0) * 0.12
            score += adj
            reasons.append(
                f"Peer-relative score {peer_rel.get('score')} vs {sector_norm or 'sector'} "
                f"({len(peer_rows)} peers)"
            )
        else:
            peer_rel = {"score": 50.0, "components": {}, "note": "no_peer_data"}
    except Exception as _pe:
        logger.warning("peer relative failed: %s", _pe)

    # --- Final adjustments ---
    if fallback_used:
        reasons.append("Live data temporarily unavailable — score is based on last known or default values")

    if not reasons:
        reasons.append("Fundamental data partially available; score is based on available metrics")

    if f.get("stale"):
        reasons.append("Live data was temporarily unavailable (Yahoo Finance rate limit) — showing the last known values instead")

    # Clamp final score
    score = max(0, min(100, round(score)))
    # Quality score = average of score and multi-quarter score (if available)
    quality_score = max(0, min(100, round((score + multi_q_score) / 2)))

    return {
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
        "quality_score": quality_score,
        "industry": f.get("industry"),
        "reasons": reasons,
        "metrics": metrics,
        "raw": f,
        "fallback_used": fallback_used,
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8003))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)