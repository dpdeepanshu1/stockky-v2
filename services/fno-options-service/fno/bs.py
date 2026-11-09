"""fno/bs.py - Black-Scholes(-Merton) price, Greeks and implied volatility for European index options. Pure stdlib.

Used (a) when Dhan's Greeks are missing, (b) by the SIMULATED intraday backtest, which has no real option prices. Index options on NSE / BSE are
European and cash settled, so the closed form is the right model; the dividend yield q defaults to 0 (the index forward is approximated by spot * e^(rT)).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, Optional, Tuple

SQRT2 = math.sqrt(2.0)
EXPIRY_CLOSE = dtime(15, 30)
YEAR_S = 365.0 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(now: datetime, expiry: date) -> float:
    """Calendar time from `now` to 15:30 on the expiry date, in years (365-day). 0 at or after expiry. `now` is naive IST or any consistent clock."""
    end = datetime.combine(expiry, EXPIRY_CLOSE)
    if now.tzinfo is not None:
        end = end.replace(tzinfo=now.tzinfo)
    secs = (end - now).total_seconds()
    return max(0.0, secs / YEAR_S)


def _d1d2(s: float, k: float, t: float, r: float, sigma: float, q: float) -> Tuple[float, float]:
    vt = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / vt
    return d1, d1 - vt


def intrinsic(s: float, k: float, opt_type: str) -> float:
    return max(0.0, s - k) if opt_type == "CE" else max(0.0, k - s)


def price(s: float, k: float, t: float, r: float, sigma: float, opt_type: str, q: float = 0.0) -> float:
    """Theoretical premium. t in years, sigma as a fraction (0.13). At t<=0 or sigma<=0 it is the intrinsic value."""
    if s <= 0 or k <= 0:
        return 0.0
    if t <= 0 or sigma <= 0:
        return intrinsic(s, k, opt_type)
    d1, d2 = _d1d2(s, k, t, r, sigma, q)
    if opt_type == "CE":
        return s * math.exp(-q * t) * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


def greeks(s: float, k: float, t: float, r: float, sigma: float, opt_type: str, q: float = 0.0) -> Dict[str, float]:
    """delta, gamma, theta (per CALENDAR DAY, in premium rupees), vega (per 1 vol point = 0.01)."""
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        d = 1.0 if (opt_type == "CE" and s > k) else (-1.0 if (opt_type == "PE" and s < k) else 0.0)
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1d2(s, k, t, r, sigma, q)
    pdf = norm_pdf(d1)
    disc_q, disc_r = math.exp(-q * t), math.exp(-r * t)
    gamma = disc_q * pdf / (s * sigma * math.sqrt(t))
    vega = s * disc_q * pdf * math.sqrt(t) / 100.0
    if opt_type == "CE":
        delta = disc_q * norm_cdf(d1)
        theta = (-s * disc_q * pdf * sigma / (2 * math.sqrt(t)) - r * k * disc_r * norm_cdf(d2) + q * s * disc_q * norm_cdf(d1)) / 365.0
    else:
        delta = disc_q * (norm_cdf(d1) - 1.0)
        theta = (-s * disc_q * pdf * sigma / (2 * math.sqrt(t)) + r * k * disc_r * norm_cdf(-d2) - q * s * disc_q * norm_cdf(-d1)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def implied_vol(premium: float, s: float, k: float, t: float, r: float, opt_type: str, q: float = 0.0,
                lo: float = 0.01, hi: float = 3.0, tol: float = 1e-5, max_iter: int = 80) -> Optional[float]:
    """Implied volatility as a fraction by bisection (robust, no derivative blow-ups near expiry). None when the premium is below intrinsic, above the
    upper bound, or t <= 0."""
    if premium is None or premium <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    if premium < intrinsic(s, k, opt_type) * math.exp(-r * t) - 1e-9:
        return None
    if price(s, k, t, r, hi, opt_type, q) < premium:
        return None
    a, b = lo, hi
    if price(s, k, t, r, a, opt_type, q) > premium:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (a + b)
        p = price(s, k, t, r, mid, opt_type, q)
        if abs(p - premium) < tol:
            return mid
        if p > premium:
            b = mid
        else:
            a = mid
    return 0.5 * (a + b)


def days_to_expiry(today: date, expiry: date) -> int:
    return (expiry - today).days
