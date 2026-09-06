# Event depth integration

1. Copy `event_depth.py` next to `event/main.py`.

2. At the end of your events builder (before return):

```python
from event_depth import enrich_events

events = { ... }  # existing payload
events = enrich_events(events, symbol=symbol)
return events
```

This adds `event_summary`, `recent_event_score`, `has_positive_catalyst`.
