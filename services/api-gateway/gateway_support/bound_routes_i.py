"""Route handler moved out of main.py in E84 (MainRef batch I, after E55 and E76-E83): only `GET /quote/{symbol}` (proxy_quote). Body unchanged except that every main.py name is
read as `ref.<name>` at call time (see gateway_support/__init__.py). It has its own factory file on purpose: the last-resort fallback calls `yf.Ticker`, and
tests/test_yfinance_callers_guard.py lists THIS one file, so no other moved handler can start using yfinance unnoticed. Market-data (Dhan -> AngelOne -> yfinance,
QUOTE_PROVIDER_ORDER) is asked first; direct yfinance runs only when that gives no price AND GATEWAY_DIRECT_YFINANCE_FALLBACK is on."""
from types import SimpleNamespace

from fastapi import APIRouter


def build_bound_routes_i(ref):
    proxy_quote_router = APIRouter()

    @proxy_quote_router.get("/quote/{symbol}")
    def proxy_quote(symbol: str):
        """Near-realtime quote proxy (market-data). Used by stock detail 30s refresh."""
        sym = symbol.upper().replace(".NS", "").replace(".BO", "")
        try:
            ref.metrics.inc("stockky_quote_proxy_total")
        except Exception:
            pass
        try:
            resp = ref.httpx.get(f"{ref.MARKET_DATA_URL}/quote/{sym}", timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if not isinstance(data, dict):      # E495: a list / null body raised on `.get` (caught below, but logged as a bare AttributeError)
                    data = {}
                # E495: each key must be a finite positive number. NaN / inf made the JSON response itself fail (500) and a text price was passed
                # through; an unusable `price` now falls to the next key, then to the fallback below.
                price = None
                for _k in ("price", "regularMarketPrice", "close", "last"):
                    _v = ref._hot_float(data.get(_k))
                    if _v is not None and _v > 0:
                        price = _v
                        break
                if price:
                    return {
                        "symbol": sym,
                        "price": price,
                        "close": (lambda _c: _c if _c is not None and _c > 0 else price)(ref._hot_float(data.get("close"))),
                        "as_of": data.get("as_of") or ref.datetime.now(ref.IST).isoformat(),
                        "source": data.get("source") or "market-data",
                        "raw": {k: data.get(k) for k in ("volume", "delivery_pct", "change_pct") if k in data},
                    }
                # A 200 with no usable price used to be returned as price=None; fall through to yfinance instead.
                ref.logger.warning("quote proxy %s: market-data returned no price, trying yfinance", sym)
        except Exception as e:
            ref.logger.warning("quote proxy %s: %s", sym, e)
        # fallback yfinance light
        try:
            if not ref._direct_yf_ok():
                raise ValueError("direct yfinance fallback is off (GATEWAY_DIRECT_YFINANCE_FALLBACK=0)")
            yf_ticker = ref.resolve_ns_ticker(sym)
            if not yf_ticker:
                raise ValueError(f"{sym} not resolvable on NSE")
            t = ref.yf.Ticker(yf_ticker)
            info = t.fast_info if hasattr(t, "fast_info") else {}
            price = None
            try:
                price = float(info.get("last_price") or info.get("lastPrice") or 0) or None
                if price is not None and not (ref.math.isfinite(price) and price > 0):   # E495: NaN is truthy, so it was returned as the price
                    price = None
            except Exception:
                price = None
            return {"symbol": sym, "price": price, "close": price, "as_of": ref.datetime.now(ref.IST).isoformat(), "source": "yfinance_fast"}
        except Exception as e:
            raise ref.HTTPException(status_code=502, detail=f"quote unavailable: {e}")

    return SimpleNamespace(
        proxy_quote=proxy_quote,
        proxy_quote_router=proxy_quote_router,
    )
