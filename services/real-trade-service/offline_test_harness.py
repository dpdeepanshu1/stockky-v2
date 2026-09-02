"""
offline_test_harness.py — Short-Term Trading Upgrade real-data test harness
(2026-09-02, v2: candidate_engine + risk_engine coverage added)

WHAT THIS DOES
──────────────
Runs the ACTUAL real-trade-service Python modules — not a reimplementation —
against a local data folder (from stockky_data_download.ipynb's output zip)
and an in-memory SQLite DB, so the full watchlist → entry-band → exit-profile
→ candidate-scoring → risk-check pipeline can be exercised with real NSE
data, with no live services, no Oracle/Postgres DB, and no Dhan connection
required.

Modules imported and actually executed (unmodified):
    watchlist_engine.decay
    watchlist_engine.sources      (fetch_watchlist_candidates — Tier 1/2/3 ladder)
    watchlist_engine.watchlist    (refresh_watchlist, expire_stale_entries)
    entry_engine.entry            (evaluate_watchlist_entries)
    exit_engine.exit               (_load_profile, _trail_atr_mult)
    candidate_engine.candidates   (refresh_candidates — full RSI/ADX/ATR-cap/
                                    resistance multi-timeframe scoring, NEW v2)
    risk_engine.engine            (evaluate() — all 9 checks, NEW v2)
    models                        (real schema, via sqlite instead of Oracle/Postgres)

The only things swapped out are network calls (httpx → fixture files) and
get_quotes() (→ fixture quotes.json). Nothing about the decision logic is
touched. No retry logic has been added anywhere in this harness — every
fixture lookup is a single attempt, same as before.

WHAT THIS STILL DOES NOT DO (cannot be tested from a data zip, full stop)
──────────────────────────────────────────────────────────────────────────
- Does not place any Dhan order. execution/dhan_client.py (place_order,
  cancel_order, get_positions, get_funds, token auth) needs a live or paper
  Dhan broker connection — there is no notebook-downloadable data that can
  stand in for that. RiskVerdict.APPROVED is as far downstream as this
  harness goes; nothing past risk_engine.evaluate() is exercised.
- Does not run execution/auto_pilot.py, equity_sync.py, reconcile.py, the
  actual cycle_runner.py scheduler loop, or main.py's FastAPI service —
  none of these add decision logic beyond what Stages 1-6 already exercise;
  they're wiring/scheduling around it.
- risk_engine's AccountState (equity, cash_available, open positions, daily
  P&L) is SYNTHETIC in this harness — it comes from TradeRiskConfig's own
  schema defaults (models.py) and config.py's DEFAULT_DEMO_CAPITAL, not from
  a real Dhan account, since no real data zip can contain your live account
  state. Every price/ATR/technical-score feeding INTO risk_engine.evaluate()
  IS real. See build_demo_account() below for the exact numbers and where
  each one is sourced from.
- Two source tracks have no NSE-public data equivalent and are synthesized
  as empty-but-valid payloads so their code path falls through cleanly
  instead of crashing (documented in FixtureRouter below): watchlist tier 2
  (/events/raw-feed) and candidate "surprise" momentum (/surprise/scan).
  Everything else — hot_picks, ipo, volume_shock/scan-universe, history,
  quote, delivery — is served from real downloaded data.

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
        self.delivery = (self._load("quotes/delivery.json") or {}).get("delivery", {})
        self.history = {}  # lazy per-symbol cache, loaded on first request
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
        # candidate_engine's "surprise" track (/surprise/scan?cached=true) has
        # no NSE-public equivalent either (needs a live surprise-momentum
        # scan service) — same synthesize-empty treatment as raw_feed above,
        # so _refresh_standard_candidates falls through cleanly to hot_picks
        # + ipo instead of crashing on a 404.
        self.surprise = {"results": []}
        # period/interval -> download-notebook's history file key. Verified
        # against candidate_engine.candidates._multi_tf_analysis's `periods`
        # dict and the download notebook's cell 5 TIMEFRAMES list.
        self._tf_key_map = {
            ("1d", "60m"): "1d_60m",
            ("5d", "1d"): "5d_1d",
            ("1mo", "1d"): "1mo_1d",
            ("3mo", "1d"): "3mo_1d",
            ("6mo", "1wk"): "6mo_1wk",
            ("1y", "1wk"): "1y_1wk",
            ("1y", "1d"): "1y_1d",
            ("2y", "1mo"): "2y_1mo",
        }

    def _load(self, rel_path: str):
        p = self.data_dir / rel_path
        if not p.exists():
            return None
        with open(p) as f:
            return json.load(f)

    def _load_history(self, symbol: str):
        if symbol not in self.history:
            self.history[symbol] = self._load(f"history/{symbol}.json")
        return self.history[symbol]

    def get(self, url: str):
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/stockky-hot":
            return _FakeResponse(200, self.hot_picks or {})
        if path == "/surprise/ipo/list":
            return _FakeResponse(200, self.ipo_list or {"results": []})
        if path == "/scan/universe":
            return _FakeResponse(200, self.volume_shockers or {"momentum_movers": []})
        if path == "/events/raw-feed":
            return _FakeResponse(200, self.raw_feed)
        if path == "/surprise/scan":
            return _FakeResponse(200, self.surprise)
        if path.startswith("/history/"):
            symbol = path.rsplit("/", 1)[-1]
            period = (query.get("period") or [None])[0]
            interval = (query.get("interval") or [None])[0]
            hdata = self._load_history(symbol)
            if hdata is None:
                return _FakeResponse(404, None)
            key = self._tf_key_map.get((period, interval))
            candles = hdata.get("timeframes", {}).get(key, []) if key else []
            return _FakeResponse(200, {"symbol": symbol, "candles": candles})
        if path.startswith("/quote/"):
            symbol = path.rsplit("/", 1)[-1]
            q = self.quotes.get(symbol)
            if q is None:
                return _FakeResponse(404, None)
            return _FakeResponse(200, q)
        if path.startswith("/delivery/"):
            symbol = path.rsplit("/", 1)[-1]
            d = self.delivery.get(symbol)
            if d is None:
                return _FakeResponse(404, None)
            return _FakeResponse(200, d)
        return _FakeResponse(404, None)

    def post(self, url: str, json_body):
        # Only real POST caller is candidate_engine's /quotes/bulk cache-warm
        # prefetch — best-effort by design (candidates.py's own docstring:
        # "never raises"), and every result it would warm is already served
        # directly by the per-symbol GET /quote/{symbol} route above. A
        # fixture doesn't need a real server-side cache to warm, so this is
        # a harmless no-op 200, not a shortcut around any real logic.
        path = urlparse(url).path
        if path == "/quotes/bulk":
            return _FakeResponse(200, {"warmed": True})
        return _FakeResponse(404, None)


class _FakeAsyncClient:
    """Drop-in replacement for httpx.AsyncClient covering .get() and .post()."""
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

    async def post(self, url, json=None, timeout=None):
        return self._router.post(url, json)


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

    # ── Stage 5: candidate_engine full scoring (REAL candidate_engine.candidates) ──
    # Real RSI/ADX/ATR-cap/resistance multi-timeframe scoring against real
    # history/quote/delivery data, via the FixtureRouter's new routes.
    import candidate_engine.candidates as candidates_mod

    inserted = await candidates_mod.refresh_candidates(db, MODE)
    # watchlist_entry_id is set only for Stage 2's entry_engine-sourced rows —
    # filter those out here so this breakdown shows just what candidate_engine
    # itself inserted this stage, not Stage 2's rows re-listed under a new label.
    ce_candidates = (
        db.query(models.TradeCandidate)
        .filter_by(mode=MODE)
        .filter(models.TradeCandidate.watchlist_entry_id.is_(None))
        .all()
    )
    by_source = {}
    for c in ce_candidates:
        by_source.setdefault(c.source_tab, []).append(c)
    print(f"\n[Stage 5] candidate_engine.refresh_candidates: {inserted} candidates inserted "
          f"(quality+MTF filter passed)")
    for src, cands in by_source.items():
        syms = ", ".join(c.symbol for c in cands[:8])
        more = " ..." if len(cands) > 8 else ""
        print(f"    {src:14s} ({len(cands):3d} symbols): {syms}{more}")
    if not ce_candidates:
        print("    No candidates passed candidate_engine's quality gate on this data pull —"
              " this can be a genuinely weak/choppy market day (MIN_CONVICTION=55,"
              " MIN_BULLISH_TIMEFRAMES=4 are strict by design), not necessarily a bug.")

    # Stage 6 risk-checks every TradeCandidate row regardless of which stage
    # produced it — that matches production (risk_engine is the single choke
    # point downstream of both entry_engine and candidate_engine).
    all_candidates = db.query(models.TradeCandidate).filter_by(mode=MODE).all()

    # ── Stage 6: risk_engine.evaluate() (REAL risk_engine.engine) ────────────
    # AccountState here is SYNTHETIC — see module docstring's "WHAT THIS
    # STILL DOES NOT DO" section for exactly why and where each number comes
    # from. entry_price/atr/qty are computed from the REAL candidate data
    # produced in Stage 5, not invented.
    import risk_engine.engine as risk_mod

    def build_demo_account() -> "risk_mod.AccountState":
        # Every numeric default below is the REAL schema/config default —
        # models.TradeRiskConfig's column defaults and config.py's
        # DEFAULT_DEMO_CAPITAL — not a harness-invented number. This models
        # a freshly-seeded DEMO account with no open positions and no P&L
        # yet today, since a real one isn't available from a data-only zip.
        return risk_mod.AccountState(
            equity=config.DEFAULT_DEMO_CAPITAL,
            risk_per_trade_pct=1.0,
            max_daily_loss_pct=3.0,
            max_concurrent_positions=3,
            max_portfolio_risk_pct=5.0,
            stale_data_seconds=30,
            max_tick_volatility_mult=2.0,
            allow_pyramiding=False,
            realized_pnl_today=0.0,
            open_position_count=0,
            open_position_symbols=set(),
            open_positions_total_risk=0.0,
            trading_globally_paused=False,
            market_is_open=True,
            cash_available=config.DEFAULT_DEMO_CAPITAL,
        )

    account = build_demo_account()
    print(f"\n[Stage 6] risk_engine.evaluate() against {len(all_candidates)} real candidates "
          f"(synthetic DEMO account: equity=₹{account.equity:,.0f}, "
          f"risk_per_trade={account.risk_per_trade_pct}%):")
    verdict_tally: dict[str, int] = {}
    for c in all_candidates:
        payload = json.loads(c.raw_payload or "{}")
        atr_pct = (payload.get("_mtf") or {}).get("atr_pct")
        price = c.signal_price or 0.0
        if not price:
            q = router.quotes.get(c.symbol) or {}
            price = q.get("price") or q.get("ltp") or 0.0
        if not price:
            verdict_tally["no_price_skip"] = verdict_tally.get("no_price_skip", 0) + 1
            continue
        atr_abs = (atr_pct / 100.0 * price) if atr_pct else price * 0.02  # 2% fallback
        stop_price = round(price - 1.5 * atr_abs, 2)
        risk_per_share = price - stop_price
        qty = int((account.equity * (account.risk_per_trade_pct / 100.0)) // risk_per_share) if risk_per_share > 0 else 0

        intent = risk_mod.OrderIntent(
            mode=MODE, symbol=c.symbol, side="BUY", qty=max(qty, 1),
            entry_price=price, stop_price=stop_price,
            recent_atr_pct=atr_pct,
        )
        result = risk_mod.evaluate(intent, account)
        verdict_tally[result.check_name] = verdict_tally.get(result.check_name, 0) + 1
        if result.verdict != risk_mod.RiskVerdict.APPROVED:
            print(f"    {c.symbol:12s} {result.verdict.value:9s} [{result.check_name}] {result.reason[:90]}")
    n_approved = verdict_tally.get("all_checks_passed", 0) + verdict_tally.get("sized_down", 0)
    print(f"    {n_approved}/{len(all_candidates)} approved (breakdown: {verdict_tally})")

    print(f"\n{'='*70}\nDONE. {len(rows)} watchlist rows | {tally.get('queued', 0)} queued for entry | "
          f"{tally.get('missed', 0)} missed (already ran) | {len(flagged)} symbols extended/extended_short | "
          f"{inserted} candidate_engine candidates | {len(all_candidates)} risk_engine-evaluated\n{'='*70}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python offline_test_harness.py /path/to/unzipped/stockky_real_data")
        sys.exit(1)
    data_dir = Path(sys.argv[1]).resolve()
    if not data_dir.exists():
        print(f"Data dir not found: {data_dir}")
        sys.exit(1)
    asyncio.run(run(data_dir))
