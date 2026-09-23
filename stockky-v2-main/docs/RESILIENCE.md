# Stockky resilience: metrics, alerting, testing

## Metrics (in-process, free-tier)

API Gateway exposes:

| Endpoint | Format |
|----------|--------|
| `GET /metrics` | JSON snapshot (counters, gauges, timings) |
| `GET /metrics?format=prom` | Prometheus text (no extra deps) |
| `GET /circuits` | Circuit breaker states |
| `POST /ops/check-alert` | Threshold check → notification if unhealthy |

### Example metrics keys
- `stockky_dependency_ok_total{dependency="decision"}`
- `stockky_dependency_errors_total{dependency="news"}`
- `stockky_circuit_open_total{dependency="decision"}`
- `stockky_scan_complete_total`
- `stockky_ops_alerts_total`

### Code snippet — increment on dependency call

```python
from metrics import metrics
from circuit_breaker import get_breaker, CircuitOpenError

async def _cb_get(client, name, url, timeout=30.0):
    br = get_breaker(name)
    if not br.allow():
        metrics.inc("stockky_circuit_open_total", dependency=name)
        raise CircuitOpenError(name, br.retry_after())
    t0 = time.time()
    try:
        resp = await client.get(url, timeout=timeout)
        metrics.observe_ms("stockky_dependency_latency", (time.time() - t0) * 1000, dependency=name)
        if resp.status_code >= 500:
            br.record_failure(f"HTTP {resp.status_code}")
            metrics.inc("stockky_dependency_errors_total", dependency=name)
        else:
            br.record_success()
            metrics.inc("stockky_dependency_ok_total", dependency=name)
        return resp
    except Exception as e:
        metrics.inc("stockky_dependency_errors_total", dependency=name)
        br.record_failure(str(e))
        raise
```

### Alert thresholds (`POST /ops/check-alert`)
- Any circuit **open** → high urgency notify
- After ≥20 dependency samples, **error rate ≥ 40%** → high urgency notify

Cron (market hours full warm) already calls this endpoint.

---

## Keep-warm: market hours only for full process

`.github/workflows/wake-services.yml`:

| When (Asia/Kolkata) | Behaviour |
|---------------------|-----------|
| Mon–Fri **08:30–16:00** | **FULL**: `/wake-all`, `health?warm=true` all services, outbox process, ops alert |
| Nights / weekends | **LIGHT**: gateway `/health` only (+ notification health if set) |

Manual override: workflow_dispatch `mode=force_full` or `force_light`.

---

## Resilience testing tools (explored)

| Tool | Fit for free-tier Stockky | Notes |
|------|---------------------------|--------|
| **scripts/resilience_check.py** | ✅ Primary | Health, circuits, metrics, ops alert — zero deps |
| **curl / httpx** | ✅ | Smoke + latency |
| **pytest + respx/httpx mock** | ✅ Local unit | Mock downstream 500s to open circuit |
| **k6 / locust** | Optional | Load; easy to hit Yahoo rate limits |
| **toxiproxy / chaos-mesh** | Heavy | Overkill for Render free |
| **Gremlin / AWS FIS** | Paid | Not needed |

### Run built-in check

```bash
export API_GATEWAY_URL=https://your-api-gateway.onrender.com
python scripts/resilience_check.py

# Optional decide smoke:
RESILIENCE_DECIDE=1 python scripts/resilience_check.py
```

### Unit-test style snippet (pytest + httpx mock)

```python
# tests/test_circuit_breaker.py
from circuit_breaker import CircuitBreaker, CircuitOpenError

def test_opens_after_threshold():
    br = CircuitBreaker("demo", failure_threshold=3, recovery_timeout=60)
    for _ in range(3):
        br.record_failure("boom")
    assert br.state() == "open"
    assert br.allow() is False

def test_half_open_recovers():
    br = CircuitBreaker("demo", failure_threshold=2, recovery_timeout=0.01)
    br.record_failure("a")
    br.record_failure("b")
    assert br.state() == "open"
    import time; time.sleep(0.02)
    assert br.allow() is True  # half_open
    br.record_success()
    assert br.state() == "closed"
```

### Simulate open circuit with curl

```bash
# Inspect
curl -s "$API_GATEWAY_URL/circuits" | jq

# After many forced failures downstream, scan symbols should return DO NOT BUY with circuit_open
curl -s "$API_GATEWAY_URL/metrics" | jq '.counters'
```

---

## Recommended free-tier loop
1. Market-hours **full warm** every 5 min  
2. Off-hours **light health** only  
3. Cron/`ops/check-alert` → Telegram/CallMeBot on circuit open  
4. Weekly `python scripts/resilience_check.py` from Actions or laptop  
