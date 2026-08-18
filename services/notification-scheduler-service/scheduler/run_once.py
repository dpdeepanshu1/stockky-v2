"""
Scheduler — single-shot mode, driven by GitHub Actions cron.

Now uses batch scanning via /scan/batch to avoid monolithic timeouts.
"""
import os
import json
import logging
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler-once")

API_GATEWAY_URL = os.environ["API_GATEWAY_URL"]
EVENT_TRACKER_URL = os.environ.get("EVENT_TRACKER_URL", "")
NOTIFICATION_URL = os.environ.get("NOTIFICATION_URL", "")
IST = ZoneInfo("Asia/Kolkata")
USE_REDIS = os.environ.get("USE_REDIS", "0").lower() in ("1", "true", "yes")

# Market hours (IST)
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SCAN_START = dtime(8, 15)
SCAN_END = dtime(15, 30)
OPEN_ANNOUNCE_TIME = dtime(8, 15)
CLOSE_SUMMARY_TIME = dtime(15, 30)
SLEEP_TIME = dtime(16, 30)

# Redis keys
STATE_KEY = "stockky:scheduler:last_decisions"
OPEN_MSG_KEY = "stockky:scheduler:open_msg:"
OPEN_NOW_KEY = "stockky:scheduler:open_now:"
CLOSE_MSG_KEY = "stockky:scheduler:close_msg:"
SLEEP_MSG_KEY = "stockky:scheduler:sleep_msg:"
DAILY_PICKS_KEY = "stockky:scheduler:picks:"
LAST_SCAN_KEY = "stockky:scheduler:last_scan_timestamp"
START_MSG_KEY = "stockky:scheduler:start_msg:"

HOLIDAYS_2026 = [
    "2026-01-26", "2026-03-02", "2026-03-31", "2026-04-02",
    "2026-04-10", "2026-04-14", "2026-05-01", "2026-08-15",
    "2026-10-02", "2026-10-22", "2026-11-14", "2026-11-15", "2026-12-25",
]

_redis = None
if USE_REDIS:
    try:
        from upstash_redis import Redis as _UR
        _redis = _UR(
            url=os.environ.get("UPSTASH_REDIS_REST_URL", ""),
            token=os.environ.get("UPSTASH_REDIS_REST_TOKEN", ""),
        )
        _redis.ping()
        logger.info("Scheduler Redis ON (USE_REDIS=1)")
    except Exception as e:
        logger.warning("Scheduler Redis unavailable (%s) — file state only", e)
        _redis = None
else:
    logger.info("Scheduler Redis OFF — local file state + optional QStash")

_STATE_DIR = os.environ.get("SCHEDULER_STATE_DIR", "/tmp/stockky_scheduler")
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except Exception:
    pass

def _file_key_path(key: str) -> str:
    safe = key.replace(":", "_").replace("/", "_")
    return os.path.join(_STATE_DIR, safe + ".json")

def _state_get(key: str):
    if _redis is not None:
        try:
            return _state_get(key)
        except Exception:
            pass
    path = _file_key_path(key)
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    except Exception:
        pass
    return None

def _state_set(key: str, value, ex=None):
    raw = value if isinstance(value, str) else json.dumps(value, default=str)
    if _redis is not None:
        try:
            if ex:
                _state_set(key, raw, ex=ex)
            else:
                _state_set(key, raw)
            return
        except Exception as e:
            logger.debug("redis set fail: %s", e)
    path = _file_key_path(key)
    try:
        with open(path, "w") as f:
            f.write(raw)
    except Exception as e:
        logger.debug("file state set fail: %s", e)

BUY_FAMILY = {"BUY NOW", "PREPARE TO BUY", "HOLD"}

# Mirrors api-gateway's _value_adjusted_score/_select_top_picks exactly —
# duplicated here because this is a standalone script with no shared
# import path to api-gateway's process. Keep these two in sync if the
# constants change.
VALUE_PRICE_CAP = 2000.0
VALUE_BONUS_MAX = 8.0
VALUE_MIN_FUNDAMENTAL_FOR_BONUS = 50.0

def _value_adjusted_score(r: dict):
    price = r.get("close")
    combined = r.get("combined_score", 0) or 0
    if price is None or price <= 0:
        return combined, True
    eligible = price <= VALUE_PRICE_CAP
    if not eligible:
        return combined, False
    fundamental = r.get("fundamental_score", 0) or 0
    bonus = (
        (1 - price / VALUE_PRICE_CAP) * VALUE_BONUS_MAX
        if fundamental >= VALUE_MIN_FUNDAMENTAL_FOR_BONUS
        else 0.0
    )
    return combined + bonus, True

def _select_top_picks(actionable: list, limit: int = 5) -> list:
    eligible = [r for r in actionable if _value_adjusted_score(r)[1]]
    eligible.sort(key=lambda r: _value_adjusted_score(r)[0], reverse=True)
    return eligible[:limit]

SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "60"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "12"))          # max symbols per batch (gateway limit 15)
MAX_CONCURRENT_BATCHES = int(os.getenv("MAX_CONCURRENT_BATCHES", "2"))
BATCH_TIMEOUT = int(os.getenv("BATCH_TIMEOUT", "300"))   # seconds per batch (free-tier cold)
SCAN_TIMEOUT_TOTAL = int(os.getenv("SCAN_TIMEOUT_TOTAL", "5400"))  # 90 min hard cap
USE_FULL_UNIVERSE = os.getenv("USE_FULL_UNIVERSE", "true").lower() in ("1", "true", "yes")
USE_GATEWAY_ASYNC_SCAN = os.getenv("USE_GATEWAY_ASYNC_SCAN", "true").lower() in ("1", "true", "yes")
FORCE_SCAN = os.getenv("FORCE_SCAN", "false").lower() == "true"


def is_holiday(today: datetime) -> bool:
    date_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        return True
    return date_str in HOLIDAYS_2026


def _wake_up_services():
    services = [API_GATEWAY_URL, NOTIFICATION_URL, EVENT_TRACKER_URL]
    for url in services:
        try:
            httpx.get(f"{url}/health", timeout=10)
        except Exception:
            pass


def _notify(title: str, message: str, channel: str = "all", retries: int = 3):
    _wake_up_services()
    time.sleep(5)
    payload = {"title": title, "message": message, "channel": channel}
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(f"{NOTIFICATION_URL}/notify", json=payload, timeout=60)
            if resp.status_code == 200:
                logger.info("Notification sent: %s", title)
                return
        except Exception as e:
            logger.warning(f"Notify attempt {attempt+1} failed: {e}")
        if attempt < retries:
            time.sleep(10)
    logger.error("Notification failed after all retries: %s", title)


def _load_last_decisions() -> dict:
    try:
        val = _state_get(STATE_KEY)
        return json.loads(val) if val else {}
    except Exception:
        return {}


def _save_last_decisions(decisions: dict):
    try:
        _state_set(STATE_KEY, json.dumps(decisions))
    except Exception:
        pass


def check_decision_changes(all_results: list):
    previous = _load_last_decisions()
    current = {r["symbol"]: r["decision"] for r in all_results}
    for symbol, decision in current.items():
        prev = previous.get(symbol)
        if decision == "BUY NOW" and prev != "BUY NOW":
            _notify(f"🟢 New BUY NOW: {symbol}",
                    f"{symbol} just became a BUY NOW opportunity.")
        elif decision == "SELL" and prev in BUY_FAMILY:
            _notify(f"🔴 {symbol} flipped to SELL",
                    f"{symbol} moved from {prev} to SELL.")
    _save_last_decisions(current)


def sync_event_subscriptions():
    try:
        wl = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=15).json()
        httpx.post(f"{EVENT_TRACKER_URL}/subscribe", json={"symbols": wl["symbols"]}, timeout=15)
        logger.info("Event Tracker subscriptions synced")
    except Exception as e:
        logger.warning(f"Event sync failed: {e}")


def run_event_check():
    """Notifies on changes returned by event-tracker-service's /check.
    NOTE: filtering these to "recent or upcoming only" (not stale/past)
    needs to happen in event-tracker-service itself, where the actual
    date fields for each change live — this script only receives
    change["symbol"]/change["changes"] (list of description strings, no
    structured date), so there's nothing here to filter on without
    guessing a field name that might not exist. See the event-tracker
    file for where this actually needs fixing."""
    try:
        resp = httpx.get(f"{EVENT_TRACKER_URL}/check", timeout=60)
        resp.raise_for_status()
        result = resp.json()
        for change in result.get("changes", []):
            _notify(f"📅 Event update: {change['symbol']}", "\n".join(change["changes"]))
    except Exception as e:
        logger.warning(f"Event check failed: {e}")


def scan_batch(symbols: List[str], timeout: int = BATCH_TIMEOUT) -> List[Dict]:
    """Call /scan/batch for a list of symbols."""
    try:
        # Pre-wake helps free-tier avoid first-request 502s
        try:
            httpx.get(f"{API_GATEWAY_URL}/health", timeout=15)
        except Exception:
            pass
        resp = httpx.post(
            f"{API_GATEWAY_URL}/scan/batch",
            json={"symbols": symbols},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        logger.error(f"Batch scan failed for {symbols}: {e}")
        # Return error entries for each symbol
        return [{"symbol": sym, "decision": "ERROR", "error": str(e)} for sym in symbols]


def _fetch_scan_symbols() -> List[str]:
    """Prefer full gateway scan universe; fall back to watchlist."""
    symbols: List[str] = []
    if USE_FULL_UNIVERSE:
        try:
            # Wake gateway first
            try:
                httpx.post(f"{API_GATEWAY_URL}/wake/all", timeout=30)
            except Exception:
                pass
            resp = httpx.get(f"{API_GATEWAY_URL}/scan/universe", timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                symbols = data.get("symbols") or data.get("universe") or []
                if isinstance(symbols, list) and symbols:
                    logger.info("Loaded %s symbols from /scan/universe", len(symbols))
                    return [str(s).upper().replace(".NS", "").replace(".BO", "") for s in symbols]
        except Exception as e:
            logger.warning("Full universe fetch failed (%s) — falling back to watchlist", e)
    try:
        wl_resp = httpx.get(f"{API_GATEWAY_URL}/watchlist", timeout=30)
        wl_resp.raise_for_status()
        symbols = wl_resp.json().get("symbols", [])
        logger.info("Loaded %s symbols from watchlist", len(symbols))
    except Exception as e:
        logger.error("Failed to fetch watchlist: %s", e)
        return []
    return [str(s).upper().replace(".NS", "").replace(".BO", "") for s in symbols]


def _run_gateway_async_scan() -> Dict[str, Any]:
    """Prefer gateway's own full async scan (handles universe + notify)."""
    try:
        try:
            httpx.post(f"{API_GATEWAY_URL}/wake/all", timeout=45)
        except Exception:
            pass
        # force_refresh so market-hour ticks get a real scan
        resp = httpx.post(
            f"{API_GATEWAY_URL}/scan/start",
            params={"force_refresh": "true", "lite": "true"},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            logger.warning("scan/start returned no task_id: %s", data)
            return {}
        logger.info("Started gateway async scan task_id=%s", task_id)
        # Poll until done or overall timeout
        start = datetime.now()
        last_status = None
        while (datetime.now() - start).total_seconds() < SCAN_TIMEOUT_TOTAL:
            try:
                st = httpx.get(f"{API_GATEWAY_URL}/scan/status/{task_id}", timeout=30)
                st.raise_for_status()
                body = st.json()
                last_status = body.get("status")
                processed = body.get("processed")
                total = body.get("total")
                logger.info("Scan progress: status=%s processed=%s/%s", last_status, processed, total)
                if last_status in ("done", "error", "cancelled"):
                    result = body.get("result") or body
                    return result if isinstance(result, dict) else {"raw": body}
            except Exception as e:
                logger.warning("poll status failed: %s", e)
            time.sleep(15)
        logger.warning("Gateway async scan timed out after %ss", SCAN_TIMEOUT_TOTAL)
        return {"status": "timeout", "task_id": task_id, "last": last_status}
    except Exception as e:
        logger.error("Gateway async scan failed: %s", e)
        return {}


def run_scan() -> Dict[str, Any]:
    """Run full-universe scan (prefer gateway async), else batched /scan/batch."""
    if USE_GATEWAY_ASYNC_SCAN:
        result = _run_gateway_async_scan()
        ok = bool(result) and (
            result.get("recommendations") is not None
            or result.get("status") in ("done", "timeout")
            or result.get("scanned") is not None
            or result.get("results") is not None
        )
        if ok:
            # Gateway already notifies on completion; still track decision changes
            recs = result.get("recommendations") or result.get("results") or []
            if isinstance(recs, list) and recs:
                try:
                    check_decision_changes(recs)
                except Exception as e:
                    logger.warning("check_decision_changes failed: %s", e)
            if result.get("status") != "timeout":
                return result
        logger.warning("Gateway async scan empty/failed/timeout — falling back to batch mode")

    symbols = _fetch_scan_symbols()
    if not symbols:
        logger.warning("No symbols to scan")
        return {}

    logger.info(f"Scanning {len(symbols)} symbols in batches of {BATCH_SIZE}")

    # Split into batches
    batches = [symbols[i:i+BATCH_SIZE] for i in range(0, len(symbols), BATCH_SIZE)]
    all_results = []
    start_time = datetime.now()
    timed_out = False

    # Use ThreadPoolExecutor to run batches in parallel
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_BATCHES) as executor:
        future_to_batch = {executor.submit(scan_batch, batch): batch for batch in batches}
        for future in as_completed(future_to_batch):
            # Check overall timeout
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed > SCAN_TIMEOUT_TOTAL:
                logger.warning(f"Overall timeout ({SCAN_TIMEOUT_TOTAL}s) reached, stopping.")
                timed_out = True
                break
            try:
                results = future.result(timeout=min(BATCH_TIMEOUT, SCAN_TIMEOUT_TOTAL - elapsed))
                all_results.extend(results)
            except Exception as e:
                batch = future_to_batch[future]
                logger.error(f"Batch {batch} failed: {e}")
                # Add error entries
                all_results.extend([{"symbol": sym, "decision": "ERROR", "error": str(e)} for sym in batch])

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info(f"Batch scan completed in {elapsed:.2f}s, got {len(all_results)} results")

    # Filter errors
    valid_results = [r for r in all_results if r.get("decision") not in ("ERROR", None)]
    if valid_results:
        check_decision_changes(valid_results)

    # Top 5 picks — previously just took the first 5 actionable results in
    # whatever order the parallel batches happened to complete (not sorted
    # by score at all), so Telegram notifications and store_daily_picks
    # were showing arbitrary picks, not the best ones. Now ranked the same
    # way api-gateway's own recommendations are: value-adjusted score
    # (Rs 2000 cap + low-price/good-fundamentals bonus), not raw
    # combined_score alone.
    actionable = [
        r for r in valid_results
        if r.get("decision") in ("BUY NOW", "PREPARE TO BUY")
    ]
    picks = _select_top_picks(actionable, limit=5)

    return {
        "verdict": "Batch scan completed" + (" (partial)" if timed_out else ""),
        "all_results": all_results,
        "recommendations": picks,
        "scanned": len(symbols),
        "successful": len(valid_results),
        "elapsed": elapsed,
        "timed_out": timed_out,
    }


def format_stock_picks(picks: List[Dict]) -> str:
    if not picks:
        return "No actionable BUY NOW / PREPARE TO BUY stocks at the moment."
    lines = ["🏆 *Top Picks:*"]
    for i, p in enumerate(picks[:5], 1):
        decision = p.get("decision", "UNKNOWN")
        sym = p.get("symbol", "?")
        score = p.get("combined_score", 0)
        entry = p.get("entry_range", {})
        target = p.get("target", 0)
        stop = p.get("stop_loss", 0)
        lines.append(f"{i}. *{sym}* – {decision} (Score: {score})")
        lines.append(f"   Entry: {entry.get('low')}–{entry.get('high')} | Target: {target} | Stop: {stop}")
    return "\n".join(lines)


def store_daily_picks(date_str: str, picks: List[Dict]):
    key = DAILY_PICKS_KEY + date_str
    existing = _state_get(key)
    if existing:
        try:
            existing_picks = json.loads(existing)
        except:
            existing_picks = []
    else:
        existing_picks = []
    symbols = {p["symbol"]: p for p in existing_picks}
    for p in picks:
        symbols[p["symbol"]] = p
    new_list = list(symbols.values())
    new_list.sort(key=lambda x: x.get("combined_score", 0), reverse=True)
    if len(new_list) > 20:
        new_list = new_list[:20]
    _state_set(key, json.dumps(new_list, default=str))


def get_daily_picks(date_str: str) -> List[Dict]:
    key = DAILY_PICKS_KEY + date_str
    data = _state_get(key)
    if data:
        try:
            return json.loads(data)
        except:
            return []
    return []


def send_start_message():
    title = "🟢 Scheduler Tick Started"
    msg = f"Stockky scheduler tick at {datetime.now(IST).strftime('%H:%M')} IST"
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    picks = get_daily_picks(yesterday)
    if picks:
        msg += "\n\n" + format_stock_picks(picks)
    _notify(title, msg)


def send_market_open_announcement():
    yesterday = (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    picks = get_daily_picks(yesterday)
    if picks:
        msg = f"The market will open at 09:15 IST. Get ready!\n\n{format_stock_picks(picks)}"
    else:
        msg = "The market will open at 09:15 IST. Get ready!"
    _notify("🕐 Market opens in 1 hour", msg)


def send_market_open_now():
    _notify("🟢 Market is now open", "Trading has started. Let's find opportunities!")


def send_scan_picks(picks: List[Dict], timed_out: bool = False):
    timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")
    if timed_out:
        title = "⏱️ Scan Timed Out (Partial Results)"
        if not picks:
            msg = f"Scan timed out – no actionable stocks found yet.\nTime: {timestamp}"
        else:
            msg = f"Scan timed out – partial picks:\n\n{format_stock_picks(picks)}\n\n⏱️ {timestamp}"
        _notify(title, msg)
    else:
        if not picks:
            _notify("🚫 No buy signals", f"No actionable stocks at this hour.\nTime: {timestamp}")
        else:
            msg = format_stock_picks(picks) + f"\n\n⏱️ {timestamp}"
            _notify("📈 Market Scan Update", msg)


def send_close_summary(date_str: str):
    picks = get_daily_picks(date_str)
    if not picks:
        _notify("📊 End of Day – No picks today", "No strong buy opportunities were found today.")
        return
    best = picks[:3]
    lines = ["📊 *End-of-Day Summary – Best picks*"]
    for i, p in enumerate(best, 1):
        sym = p.get("symbol", "?")
        decision = p.get("decision", "UNKNOWN")
        score = p.get("combined_score", 0)
        entry = p.get("entry_range", {})
        target = p.get("target", 0)
        stop = p.get("stop_loss", 0)
        lines.append(f"{i}. *{sym}* – {decision} (Score: {score})")
        lines.append(f"   Entry: {entry.get('low')}–{entry.get('high')} | Target: {target} | Stop: {stop}")
    _notify("📊 End-of-Day Summary", "\n".join(lines))


def send_sleep_message():
    _notify("🌙 Going to sleep", "Good night! I'll be back tomorrow before market open.")


def should_skip_scan() -> bool:
    try:
        last_scan_str = _state_get(LAST_SCAN_KEY)
        if not last_scan_str:
            return False
        last_scan_time = datetime.fromisoformat(last_scan_str)
        now = datetime.now(IST)
        return (now - last_scan_time) < timedelta(minutes=SCAN_INTERVAL_MINUTES)
    except Exception:
        return False


def main():
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    time_now = now.time()

    logger.info("Current IST time: %s", now.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Scan window: %s – %s", SCAN_START.strftime("%H:%M"), SCAN_END.strftime("%H:%M"))
    if FORCE_SCAN:
        logger.info("FORCE_SCAN enabled – bypassing window.")

    if is_holiday(now):
        logger.info("Market holiday – skipping.")
        return

    sync_event_subscriptions()

    # Start message
    if FORCE_SCAN or (SCAN_START <= time_now <= SCAN_END):
        send_start_message()

    # Timed notifications (once per day)
    if time_now == OPEN_ANNOUNCE_TIME and not _state_get(OPEN_MSG_KEY + today_str):
        send_market_open_announcement()
        _state_set(OPEN_MSG_KEY + today_str, "1", ex=86400)

    if time_now == MARKET_OPEN and not _state_get(OPEN_NOW_KEY + today_str):
        send_market_open_now()
        _state_set(OPEN_NOW_KEY + today_str, "1", ex=86400)

    if time_now == CLOSE_SUMMARY_TIME and not _state_get(CLOSE_MSG_KEY + today_str):
        send_close_summary(today_str)
        _state_set(CLOSE_MSG_KEY + today_str, "1", ex=86400)

    if time_now == SLEEP_TIME and not _state_get(SLEEP_MSG_KEY + today_str):
        send_sleep_message()
        _state_set(SLEEP_MSG_KEY + today_str, "1", ex=86400)

    # Run scan?
    should_run = FORCE_SCAN or (SCAN_START <= time_now <= SCAN_END)
    if should_run:
        if should_skip_scan():
            logger.info("Skipping scan – scheduler service already scanned recently.")
        else:
            # Health check
            try:
                health = httpx.get(f"{API_GATEWAY_URL}/health", timeout=30)
                if health.status_code != 200:
                    logger.error("Gateway not healthy.")
                    _notify("⚠️ Scan Aborted", "API Gateway is not healthy.")
                    return
            except Exception:
                logger.error("Gateway unreachable.")
                _notify("⚠️ Scan Aborted", "API Gateway unreachable.")
                return

            logger.info("Running batch scan at %s", time_now.strftime("%H:%M"))
            scan_result = run_scan()

            if scan_result:
                picks = scan_result.get("recommendations", [])
                timed_out = scan_result.get("timed_out", False)
                if picks:
                    store_daily_picks(today_str, picks)
                send_scan_picks(picks, timed_out)
                run_event_check()
            else:
                logger.error("Scan failed – no results.")
                _notify("⚠️ Scan Failed", f"Scan at {time_now.strftime('%H:%M')} IST could not complete.")
    else:
        logger.info("Skipping scan – outside window.")

    logger.info("Scheduler tick completed.")


def _prefer_data_feed_then_scan():
    """If data-feed has fresh data, skip heavy upstream fan-out where possible."""
    if os.environ.get("PREFER_DATA_FEED", "1").lower() not in ("1", "true", "yes"):
        return
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.get(f"{API_GATEWAY_URL.rstrip('/')}/data-feed/status")
            if r.status_code == 200:
                st = r.json()
                logger.info("data-feed status before scan: %s", str(st)[:200])
    except Exception as e:
        logger.debug("data-feed status: %s", e)


if __name__ == "__main__":
    _prefer_data_feed_then_scan()

    main()