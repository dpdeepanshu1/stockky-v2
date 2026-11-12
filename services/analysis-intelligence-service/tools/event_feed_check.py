#!/usr/bin/env python3
"""Round 181 - one tool for checking and refining the typed event feed. Stdlib only; run from services/analysis-intelligence-service on the VM.

  python3 tools/event_feed_check.py live                 # command 5: agree / fills / contested / multi-company headlines on the live feed
  python3 tools/event_feed_check.py dryrun               # what the market-hours thread WOULD trigger / veto right now (read-only, no DB, no Telegram)
  python3 tools/event_feed_check.py classify FILE|-      # classify headlines (one per line, optional 'SYMBOL | headline') with both classifiers
  python3 tools/event_feed_check.py poll                 # force the feed to poll its RSS sources now, then print a short summary
Env: EVENT_BASE (default http://127.0.0.1:8002/event)
"""
import collections, importlib.util, json, os, re, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "event"))
from env_util import env_float_safe  # E505 (event/ is on sys.path above)
BASE = os.getenv("EVENT_BASE", "http://127.0.0.1:8002/event").rstrip("/")


def _get(path, timeout=30):
    return json.load(urllib.request.urlopen(BASE + path, timeout=timeout))


def _post(path, timeout=120):
    return json.load(urllib.request.urlopen(urllib.request.Request(BASE + path, data=b"", method="POST"), timeout=timeout))


def _load():
    import event_lite as el
    os.environ["EVENT_FEED_CLASSIFIER"] = "legacy"
    spec = importlib.util.spec_from_file_location("ef", os.path.join(ROOT, "event", "event_feed.py"))
    ef = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ef)
    return el, ef


def cmd_live():
    el, ef = _load()
    d = _get("/events/active")
    ev = d.get("events", [])
    print("ok", d.get("ok"), "events", len(ev), "healthy", d.get("healthy_sources"))
    by = collections.defaultdict(set)
    for e in ev:
        by[e["headline"]].add(e["symbol"])
    print("distinct headlines", len(by), "symbols", len({e["symbol"] for e in ev}))
    multi = [(len(s), h) for h, s in by.items() if len(s) >= 2]
    print("headlines attached to >=2 symbols:", len(multi))
    for n, h in sorted(multi, reverse=True)[:8]:
        print("  ", n, "|", h[:90])
    print("flagged multi-company (sign forced to 0):", sum(1 for e in ev if e.get("multi")))
    agree = fill = cont = 0
    rows = []
    for h in by:
        r = el.classify(h)
        l = ef.classify(h)
        lp, lc = r["pol"], r["confidence"]
        if r["type"] == "none":
            lp, lc = 0, 0
        bp = l["polarity"]
        if lp == bp:
            agree += 1
        elif bp == 0 and lc >= 0.75:
            fill += 1
            rows.append(("FILL", bp, lp, r["type"], lc, h))
        elif bp != 0 and lp == -bp and lc >= 0.8:
            cont += 1
            rows.append(("CONTESTED", bp, lp, r["type"], lc, h))
        else:
            rows.append(("DIFF-below-thr", bp, lp, r["type"], lc, h))
    n = len(by)
    print(f"agree {agree}/{n} fills {fill} contested {cont}")
    for t, b, p, ty, c, h in rows[:60]:
        print(f"{t:14} keyword:{b:+d} lite:{p:+d} {ty[:10]:10} c{c:.2f} | {h[:95]}")
    tc = collections.Counter((e["type"], e["polarity"]) for e in ev)
    print(sorted(tc.items(), key=lambda x: -x[1])[:12])


_TRIGGER_TYPES = tuple(t.strip() for t in os.getenv("INTRADAY_EVENT_TRIGGER_TYPES", "results,order,deal").split(",") if t.strip())
_W = {"regulatory": 0.9, "results": 0.85, "deal": 0.7, "order": 0.65, "rating": 0.45, "management": 0.4, "other": 0.2}


def cmd_dryrun():
    d = _get("/events/active")
    ev = d.get("events", [])
    min_strength = env_float_safe("INTRADAY_EVENT_TRIGGER_MIN_STRENGTH", 0.60, lo=0, hi=1)
    max_age = env_float_safe("INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES", 30, lo=0) * 60.0
    veto_min = env_float_safe("INTRADAY_EVENT_VETO_MIN_STRENGTH", 0.40, lo=0, hi=1)
    print("feed ok", d.get("ok"), "events", len(ev), "| trigger types", _TRIGGER_TYPES, f"min strength {min_strength} max age {int(max_age/60)}m | veto min {veto_min}")
    best = {}
    for e in ev:
        pol = int(e.get("polarity") or 0)
        if pol == 0:
            continue
        base = _W.get(e.get("type"), 0.2)
        s = base if pol < 0 else min(base, 0.70)
        cur = best.get(e["symbol"])
        if cur is None or (pol < 0 and cur[0] > 0) or (pol == cur[0] and s > cur[1]):
            best[e["symbol"]] = (pol, s, e)
    if not best:
        print("no signed events: nothing would trigger or veto")
    for sym, (pol, s, e) in sorted(best.items(), key=lambda kv: -kv[1][1]):
        age = float(e.get("age_s") or 0)
        if pol > 0:
            ok = e["type"] in _TRIGGER_TYPES and s >= min_strength and age <= max_age
            verdict = "WOULD TRIGGER (queue BUY NOW candidate)" if ok else "positive, no trigger (" + ("type" if e["type"] not in _TRIGGER_TYPES else "age" if age > max_age else "strength") + ")"
        else:
            verdict = "VETO entry" if s >= veto_min else "negative below veto strength"
        print(f"{sym:12} {pol:+d} {e['type']:10} s={s:.2f} age={int(age/60)}m | {verdict} | {str(e['headline'])[:90]}")
    print("Read-only: nothing was written, no Telegram was sent. Real trigger also needs the news-scan toggle ON, time >= 09:20 IST, a preview price and <= 3 per mode per day.")


def cmd_classify(src):
    el, ef = _load()
    lines = sys.stdin.read().splitlines() if src == "-" else open(src, encoding="utf-8").read().splitlines()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        sym, h = ("", ln)
        if "|" in ln and re.match(r"^[A-Z0-9&\-]{2,20}\s*\|", ln):
            sym, h = [x.strip() for x in ln.split("|", 1)]
        r = el.classify(h)
        l = ef.classify(h)
        print(f"kw:{l['polarity']:+d} {str(l.get('type'))[:10]:10} | lite:{r['pol']:+d} {r['type'][:10]:10} c{r['confidence']:.2f} | {sym:10} {h[:100]}")


def cmd_poll():
    r = _post("/events/poll-now")
    print(json.dumps(r, indent=1)[:1500])


if __name__ == "__main__":
    a = sys.argv[1:] or ["live"]
    if a[0] == "live":
        cmd_live()
    elif a[0] == "dryrun":
        cmd_dryrun()
    elif a[0] == "classify" and len(a) > 1:
        cmd_classify(a[1])
    elif a[0] == "poll":
        cmd_poll()
    else:
        print(__doc__)
