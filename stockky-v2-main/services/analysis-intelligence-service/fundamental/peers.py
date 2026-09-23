"""
Sector peer map + self-calculated peer-relative metrics (free tier).
Peers are curated liquid NSE names; metrics compared when market-data
returns fundamentals for the symbol and (optionally) cached peer averages.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Any

# Curated sector → peer symbols (NSE). Keep liquid names only.
SECTOR_PEERS: Dict[str, List[str]] = {
    "IT": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "PERSISTENT", "COFORGE", "MPHASIS"],
    "Banks": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK", "BANDHANBNK", "FEDERALBNK"],
    "Finance": ["BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "CHOLAFIN", "MUTHOOTFIN", "PFC", "RECLTD"],
    "Auto": ["MARUTI", "M&M", "TATAMOTORS", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "TVSMOTOR", "ASHOKLEY"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA", "TORNTPHARM", "ALKEM"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO", "GODREJCP", "TATACONSUM"],
    "Energy": ["RELIANCE", "ONGC", "BPCL", "IOC", "GAIL", "PETRONET", "HINDPETRO", "POWERGRID", "NTPC"],
    "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA", "NMDC", "SAIL", "JINDALSTEL"],
    "Infra": ["LT", "ADANIPORTS", "ULTRACEMCO", "SHREECEM", "AMBUJACEM", "DLF", "GODREJPROP", "OBEROIRLTY"],
    "Telecom": ["BHARTIARTL", "IDEA"],
    "Consumer Durables": ["TITAN", "HAVELLS", "VOLTAS", "CROMPTON", "WHIRLPOOL", "DIXON", "AMBER"],
    "Capital Goods": ["SIEMENS", "ABB", "CGPOWER", "BHEL", "CUMMINSIND", "HAL", "BEL"],
}

# Symbol → sector override when Yahoo sector string is messy
SYMBOL_SECTOR: Dict[str, str] = {}
for sec, syms in SECTOR_PEERS.items():
    for s in syms:
        SYMBOL_SECTOR[s] = sec


def normalize_sector(raw: Optional[str], symbol: Optional[str] = None) -> Optional[str]:
    if symbol:
        su = symbol.upper().replace(".NS", "").replace(".BO", "")
        if su in SYMBOL_SECTOR:
            return SYMBOL_SECTOR[su]
    if not raw:
        return None
    r = raw.lower()
    mapping = [
        ("software", "IT"), ("information technology", "IT"), ("technology", "IT"),
        ("bank", "Banks"),
        ("capital market", "Finance"), ("credit", "Finance"), ("insurance", "Finance"), ("nbfc", "Finance"),
        ("auto", "Auto"), ("automobile", "Auto"),
        ("drug", "Pharma"), ("pharma", "Pharma"), ("biotech", "Pharma"),
        ("food", "FMCG"), ("beverage", "FMCG"), ("household", "FMCG"), ("tobacco", "FMCG"), ("fmcg", "FMCG"),
        ("oil", "Energy"), ("gas", "Energy"), ("power", "Energy"), ("energy", "Energy"), ("refining", "Energy"),
        ("steel", "Metals"), ("metal", "Metals"), ("mining", "Metals"), ("aluminium", "Metals"),
        ("cement", "Infra"), ("construction", "Infra"), ("engineering", "Infra"), ("real estate", "Infra"),
        ("telecom", "Telecom"),
        ("consumer durable", "Consumer Durables"), ("electronics", "Consumer Durables"),
        ("electrical", "Capital Goods"), ("industrial", "Capital Goods"), ("aerospace", "Capital Goods"), ("defence", "Capital Goods"),
    ]
    for key, sec in mapping:
        if key in r:
            return sec
    return None


def peers_for(symbol: str, sector: Optional[str] = None) -> List[str]:
    su = symbol.upper().replace(".NS", "").replace(".BO", "")
    sec = normalize_sector(sector, su)
    if not sec:
        return []
    return [p for p in SECTOR_PEERS.get(sec, []) if p != su]


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def average_metrics(rows: List[dict]) -> Dict[str, Optional[float]]:
    keys = ["pe_ratio", "pe", "roe", "roce", "revenue_growth", "earnings_growth", "debt_to_equity", "profit_margins"]
    acc: Dict[str, list] = {k: [] for k in keys}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in keys:
            val = _f(row.get(k))
            if val is not None:
                acc[k].append(val)
    out = {}
    for k, vals in acc.items():
        out[k] = round(sum(vals) / len(vals), 4) if vals else None
    # aliases
    if out.get("pe_ratio") is None and out.get("pe") is not None:
        out["pe_ratio"] = out["pe"]
    return out


def peer_relative_score(symbol_metrics: dict, peer_avg: Optional[dict]) -> Dict[str, Any]:
    """
    Returns score 0–100 and component breakdown.
    Cheaper P/E than peers, higher ROE/growth, lower debt → higher score.
    """
    if not peer_avg:
        return {"score": 50.0, "components": {}, "note": "no_peer_avg"}

    score = 50.0
    components = {}

    pe = _f(symbol_metrics.get("pe_ratio") or symbol_metrics.get("pe"))
    peer_pe = _f(peer_avg.get("pe_ratio") or peer_avg.get("pe"))
    if pe is not None and peer_pe is not None and peer_pe > 0 and pe > 0:
        # relative valuation: peer/me ; >1 means cheaper than peers
        rel = peer_pe / pe
        delta = max(-15.0, min(15.0, (rel - 1.0) * 20.0))
        score += delta
        components["pe"] = round(delta, 2)

    roe = _f(symbol_metrics.get("roe"))
    peer_roe = _f(peer_avg.get("roe"))
    if roe is not None and peer_roe is not None:
        delta = max(-15.0, min(15.0, (roe - peer_roe) * 0.8))
        score += delta
        components["roe"] = round(delta, 2)

    g = _f(symbol_metrics.get("revenue_growth") or symbol_metrics.get("earnings_growth"))
    pg = _f(peer_avg.get("revenue_growth") or peer_avg.get("earnings_growth"))
    if g is not None and pg is not None:
        delta = max(-10.0, min(10.0, (g - pg) * 0.3))
        score += delta
        components["growth"] = round(delta, 2)

    de = _f(symbol_metrics.get("debt_to_equity"))
    pde = _f(peer_avg.get("debt_to_equity"))
    if de is not None and pde is not None:
        # lower debt than peers is better
        delta = max(-8.0, min(8.0, (pde - de) * 2.0))
        score += delta
        components["debt"] = round(delta, 2)

    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "components": components,
        "peer_avg": peer_avg,
        "note": "self_calculated",
    }
