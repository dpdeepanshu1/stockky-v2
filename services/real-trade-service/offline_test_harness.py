"""
offline_test_harness.py — Short-Term Trading Upgrade real-data test harness (2026-09-02)

WHAT THIS DOES
──────────────
Runs the ACTUAL real-trade-service Python modules — not a reimplementation —
against a local data folder (from stockky_data_download.ipynb's output zip)
and an in-memory SQLite DB, so the full watchlist → entry-band → exit-profile
pipeline can be exercised with real NSE data, with no live services, no
Oracle/Postgres DB, and no Dhan connection required.

Modules imported and actually executed (unmodified):
    watchlist_engine.decay
    watchlist_engine.sources      (fetch_watchlist_candidates — Tier 1/2/3 ladder)
    watchlist_engine.watchlist    (refresh_watchlist, expire_stale_entries)
    entry_engine.entry            (evaluate_watchlist_entries)
    exit_engine.exit               (_load_profile, _trail_atr_mult)
    models                        (real schema, via sqlite instead of Oracle/Postgres)

The only things swapped out are network calls (httpx → fixture files) and
get_quotes() (→ fixture quotes.json). Nothing about the decision logic is
touched.

WHAT THIS DOES NOT DO
──────────────────────
- Does not place any Dhan order (dhan_client is never called: RiskVerdict /
  order placement lives past evaluate_watchlist_entries's TradeCandidate
  insert, which is as far as this harness goes deliberately).
- Does not run candidate_engine's full multi-timeframe analysis (RSI/ADX/
  ATR-cap/resistance) — that pulls in numpy/pandas-heavy technical scoring
  that belongs to analysis-intelligence-service, not real-trade-service.
  extended / extended_short ARE reproduced here (see technical_flags()) using
  the exact formula from analysis-intelligence-service/technical/main.py, since
  that's a two-line calc directly relevant to the "already popped, don't
  chase" bug this upgrade fixed.

USAGE
─────
    cd services/real-trade-service
    python offline_test_harness.py /path/to/stockky_real_data   # unzipped folder
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config          # noqa: E402
import models          # noqa: E402
from sqlalchemy import create_engine       # noqa: E402
from sqlalchemy.orm import sessionmaker    # noqa: E402

MODE = "DEMO"


# ─────────────────────────────────────────────────────────────────────────────
# 1. In-memory SQLite DB using the REAL models.py schema
# ─────────────────────────────────────────────────────────────────────────────

def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fixture-backed fake httpx client — routes GET calls to local JSON files
#    instead of api-gateway / event-service, based on the exact URLs
#    sources.py and candidate_engine.candidates hit (verified against source).
# ─────────────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = "" if payload is not None else "not found"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"fake HTTP {self.status_code}")


class FixtureRouter:
    """Loads the download-notebook's output folder and answers GET requests
    for the fixed set of URLs the real code actually calls."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.hot_picks = self._load("events/hot_picks.json")
        self.ipo_list = self._load("ipo/ipo_list.json")
        self.volume_shockers = self._load("events/volume_shockers.json")
        self.quotes = (self._load("quotes/all_quotes.json") or {}).get("quotes", {})
        self.raw_feed = self._load("events/raw_feed.json")  # optional, see note below
        if self.raw_feed is None:
            # Not produced by the download notebook (Tier 2 needs a live
            # analysis-intelligence-service /events/raw-feed call, which has
            # no NSE-public equivalent) — synthesize an empty-but-valid
            # payload so Tier 2 cleanly falls through to Tier 3, exactly the
            # same as it would in production if that service were briefly
            # unhealthy. This is the one tier the offline harness cannot
            # fully exercise; Tier 1 and Tier 3 get full coverage.
            self.raw_feed = {"items": [], "hours": 24, "checked_at": None}

    def _load(self, rel_path: str):
        p = self.data_dir / rel_path
        if not p.exists():
            return None
        with open(p) as f:
            return json.load(f)

    def get(self, url: str):
        path = urlparse(url).path
        if path == "/stockky-hot":
            return _FakeResponse(200, self.hot_picks or {})
        if path == "/surprise/ipo/list":
            return _FakeResponse(200, self.ipo_list or {"results": []})
        if path == "/scan/universe":
            return _FakeResponse(200, self.volume_shockers or {"momentum_movers": []})
        if path == "/events/raw-feed":
            return _FakeResponse(200, self.raw_feed)
        return _FakeResponse(404, None)


class _FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient covering only .get()."""
    _router: FixtureRouter = None  # set by install_fixture_router()

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None):
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + "&".join(f"{k}={v}" for k, v in params.items())
        return self._router.get(url)


def install_fixture_router(data_dir: Path):
    import httpx
    router = FixtureRouter(data_dir)
    _FakeAsyncClient._router = router
    httpx.AsyncClient = _FakeAsyncClient  # patches the shared module object —
    # every module that did `import httpx; httpx.AsyncClient(...)` (sources.py,
    # candidate_engine.candidates) picks this up automatically, no per-module
    # patch needed.
    return router


# ─────────────────────────────────────────────────────────────────────────────
# 3. get_quotes() fixture — feeds entry_engine.entry's price checks
# ─────────────────────────────────────────────────────────────────────────────

def install_fake_get_quotes(router: FixtureRouter):
    from market_feed.feed import Tick
    import entry_engine.entry as entry_mod

    async def _fake_get_quotes(symbols):
        out = {}
        now = datetime.now(timezone.utc)
        for sym in symbols:
            q = router.quotes.get(sym)
            if not q:
                continue
            price = q.get("ltp") or q.get("price") or q.get("close")
            if not price:
                continue
            out[sym] = Tick(
                symbol=sym, price=float(price), as_of=now,
                atr=q.get("atr"), source="offline_fixture", volume=q.get("volume"),
            )
        return out

    entry_mod.get_quotes = _fake_get_quotes


# ─────────────────────────────────────────────────────────────────────────────
# 4. extended / extended_short — exact formula from
#    analysis-intelligence-service/technical/main.py (verbatim, see that
#    file's "Short-Term Trading Upgrade (2026-09-02) — bonus fix" comment)
# ─────────────────────────────────────────────────────────────────────────────

def technical_flags(candles_1y_1d: list[dict]) -> dict:
    closes = [c["close"] for c in candles_1y_1d]
    n = len(closes)
    flags = {"extended": False, "extended_short": False, "ret_21d": None, "ret_3d": None}
    if n >= 22:
        ret_21d = closes[-1] / closes[-22] - 1.0
        flags["ret_21d"] = round(ret_21d, 4)
        flags["extended"] = ret_21d > 0.18
    if n >= 5:
        ret_3d = closes[-1] / closes[-4] - 1.0
        flags["ret_3d"] = round(ret_3d, 4)
        flags["extended_short"] = ret_3d > 0.05
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main run
# ─────────────────────────────────────────────────────────────────────────────

async def run(data_dir: Path):
    print(f"{'='*70}\nSTOCKKY OFFLINE TEST HARNESS — real-trade-service, real NSE data\ndata dir: {data_dir}\n{'='*70}\n")

    db = make_session()
    router = install_fixture_router(data_dir)
    install_fake_get_quotes(router)

    from watchlist_engine import watchlist as watchlist_mod
    import entry_engine.entry as entry_mod
    from exit_engine import exit as exit_mod
    from watchlist_engine.decay import CATALYST_PROFILES

    # ── Stage 1: watchlist ingestion (REAL sources.py + watchlist.py) ────────
    added = await watchlist_mod.refresh_watchlist(db, MODE)
    expired = watchlist_mod.expire_stale_entries(db, MODE)
    rows = db.query(models.WatchlistEntry).filter_by(mode=MODE).all()
    print(f"[Stage 1] refresh_watchlist: {added} new rows added, {expired} expired")
    print(f"[Stage 1] total watchlist rows: {len(rows)}")
    by_type = {}
    for r in rows:
        by_type.setdefault(r.catalyst_type, []).append(r.symbol)
    for ctype, syms in by_type.items():
        tier = {v["horizon_class"] for k, v in CATALYST_PROFILES.items() if k == ctype}
        print(f"    {ctype:14s} ({len(syms):3d} symbols): {', '.join(syms[:8])}{' ...' if len(syms) > 8 else ''}")

    if not rows:
        print("\nNo watchlist rows were produced — check that hot_picks.json / "
              "ipo_list.json / volume_shockers.json exist under the data dir "
              "and are non-empty.")

    # ── Stage 2: entry-band trigger pass (REAL entry_engine.entry) ───────────
    tally = await entry_mod.evaluate_watchlist_entries(db, MODE)
    print(f"\n[Stage 2] evaluate_watchlist_entries: {tally}")

    missed = db.query(models.WatchlistEntry).filter_by(mode=MODE, status="missed").all()
    if missed:
        print(f"    MISSED (price already ran past entry band, would NOT be bought):")
        for r in missed[:10]:
            print(f"      {r.symbol:12s} {r.missed_reason}")

    queued = (
        db.query(models.TradeCandidate)
        .filter_by(mode=MODE, consumed=False)
        .filter(models.TradeCandidate.watchlist_entry_id.isnot(None))
        .all()
    )
    if queued:
        print(f"\n    QUEUED for entry (within band, would be evaluated by risk_engine next):")
        for c in queued[:15]:
            print(f"      {c.symbol}")

    # ── Stage 3: exit-profile lookup (REAL exit_engine.exit) ─────────────────
    print(f"\n[Stage 3] exit profiles that would apply, by catalyst type:")
    seen_horizons = set()
    for row in rows:
        if row.horizon_class in seen_horizons:
            continue
        seen_horizons.add(row.horizon_class)
        fake_position = SimpleNamespace(watchlist_entry_id=row.id)
        profile = exit_mod._load_profile(db, fake_position)
        print(f"    horizon={row.horizon_class:6s} (e.g. {row.catalyst_type:12s}): "
              f"max_hold={profile['max_hold_days']}d  "
              f"trail={profile['trail_atr_schedule']}  "
              f"breakeven={profile['breakeven_atr_trigger']}xATR  "
              f"partial_exit={profile['partial_exit_fraction']:.0%}")

    # ── Stage 4: extended / extended_short chase-guard on real 1y candles ───
    print(f"\n[Stage 4] extended / extended_short flags (real 1y/1d candles):")
    hist_dir = data_dir / "history"
    flagged = []
    if hist_dir.exists():
        for f in sorted(hist_dir.glob("*.json")):
            with open(f) as fh:
                hdata = json.load(fh)
            candles = hdata.get("timeframes", {}).get("1y_1d", [])
            if not candles:
                continue
            flags = technical_flags(candles)
            if flags["extended"] or flags["extended_short"]:
                flagged.append((hdata["symbol"], flags))
    if flagged:
        for sym, flags in flagged[:20]:
            tag = []
            if flags["extended"]:
                tag.append(f"extended(21d {flags['ret_21d']:+.1%})")
            if flags["extended_short"]:
                tag.append(f"extended_short(3d {flags['ret_3d']:+.1%}) — CHASE RISK, would be penalized")
            print(f"    {sym:12s} {' | '.join(tag)}")
    else:
        print("    none flagged in this data pull")

    print(f"\n{'='*70}\nDONE. {len(rows)} watchlist rows | {tally.get('queued', 0)} queued for entry | "
          f"{tally.get('missed', 0)} missed (already ran) | {len(flagged)} symbols extended/extended_short\n{'='*70}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python offline_test_harness.py /path/to/unzipped/stockky_real_data")
        sys.exit(1)
    data_dir = Path(sys.argv[1]).resolve()
    if not data_dir.exists():
        print(f"Data dir not found: {data_dir}")
        sys.exit(1)
    asyncio.run(run(data_dir))
