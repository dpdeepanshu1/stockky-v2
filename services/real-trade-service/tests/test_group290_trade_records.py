"""group290: per-trade record + expectancy report (trade_records.py, GET /positions/{mode}/records and /report).

Pure functions first (SimpleNamespace rows), then the real loader against in-memory SQLite, then the real HTTP routes."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import trade_records as tr

D0 = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)      # 09:30 IST


def utc(h, m=0, day=9):
    return datetime(2026, 10, day, h, m, tzinfo=timezone.utc)


def pos(id=1, symbol="AAA", *, entry=100.0, pnl=50.0, opened=None, closed=None, mode="REAL", status="CLOSED", **kw):
    d = dict(id=id, mode=mode, symbol=symbol, status=status, avg_entry_price=entry, realized_pnl=pnl,
             opened_at=opened or utc(4, 0), closed_at=closed or utc(5, 0), broker_imported=False,
             watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW",
             entry_conviction_score=70.0, is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def order(id, side, symbol="AAA", *, mode="REAL", status="FILLED", reason=None, decision_id=None, watch=None,
          source="AUTO", at=None):
    return NS(id=id, side=side, symbol=symbol, mode=mode, status=status, exit_reason=reason, decision_id=decision_id,
              watchlist_entry_id=watch, execution_source=source, created_at=at or utc(4, 0), updated_at=at or utc(4, 0))


def fill(order_id, qty, price, at):
    return NS(order_id=order_id, qty=qty, price=price, filled_at=at)


def build(positions, orders=(), fills=(), decisions=(), cands=(), watch=(), ledger=()):
    return tr.build_records(positions, orders, fills, decisions, cands, watch, ledger)


class TestAssign:
    def test_orders_go_to_the_matching_position_by_time(self):
        p1 = pos(1, opened=utc(4, 0), closed=utc(5, 0))
        p2 = pos(2, opened=utc(6, 0), closed=utc(7, 0))
        o = [order(1, "BUY", at=utc(4, 0)), order(2, "SELL", at=utc(5, 0)),
             order(3, "BUY", at=utc(6, 0)), order(4, "SELL", at=utc(7, 0))]
        a = tr._assign([p1, p2], o, {})
        assert [x.id for x in a[1]["buys"]] == [1] and [x.id for x in a[1]["sells"]] == [2]
        assert [x.id for x in a[2]["buys"]] == [3] and [x.id for x in a[2]["sells"]] == [4]

    def test_fill_time_beats_order_time(self):
        p = pos(1, opened=utc(4, 0), closed=utc(5, 0))
        o = order(1, "BUY", at=utc(9, 0))                              # created late, but filled on time
        a = tr._assign([p], [o], {1: [fill(1, 5, 100, utc(4, 0))]})
        assert [x.id for x in a[1]["buys"]] == [1]

    def test_unfilled_orders_are_ignored(self):
        p = pos()
        a = tr._assign([p], [order(1, "BUY", status="REJECTED"), order(2, "BUY", status="CANCELLED")], {})
        assert a[1] == {"buys": [], "sells": []}

    def test_partial_status_counts_as_filled(self):
        assert tr._assign([pos()], [order(1, "BUY", status="PARTIAL")], {})[1]["buys"]

    def test_other_symbol_and_other_mode_never_match(self):
        a = tr._assign([pos()], [order(1, "BUY", symbol="ZZZ"), order(2, "BUY", mode="DEMO")], {})
        assert a[1] == {"buys": [], "sells": []}

    def test_order_long_after_close_is_not_taken(self):
        p = pos(opened=utc(4, 0), closed=utc(5, 0))
        a = tr._assign([p], [order(1, "SELL", at=utc(7, 0))], {})
        assert a[1]["sells"] == []

    def test_late_sell_fill_within_the_hour_is_taken(self):
        p = pos(opened=utc(4, 0), closed=utc(5, 0))
        assert tr._assign([p], [order(1, "SELL", at=utc(5, 30))], {})[1]["sells"]

    def test_order_slightly_before_opened_at_is_taken(self):
        p = pos(opened=utc(4, 0), closed=utc(5, 0))
        assert tr._assign([p], [order(1, "BUY", at=utc(4, 0) - timedelta(seconds=60))], {})[1]["buys"]

    def test_naive_datetimes_are_read_as_utc(self):
        p = pos(opened=datetime(2026, 10, 9, 4, 0), closed=datetime(2026, 10, 9, 5, 0))
        o = order(1, "BUY", at=datetime(2026, 10, 9, 4, 0))
        assert tr._assign([p], [o], {})[1]["buys"]


class TestBuildRecord:
    def test_full_record(self):
        w = NS(id=7, source_tier=1, catalyst_type="results", catalyst_price=98.0, catalyst_price_source="live")
        cand = NS(id=3, signal_price=99.0)
        dec = NS(id=5, candidate_id=3)
        p = pos(entry=100.0, pnl=48.0, watchlist_entry_id=7)
        o = [order(1, "BUY", decision_id=5), order(2, "SELL", reason="Target Hit", at=utc(5, 0))]
        f = [fill(1, 10, 100.0, utc(4, 0)), fill(2, 6, 105.0, utc(5, 0)), fill(2, 4, 106.0, utc(5, 1))]
        led = [NS(order_id=1, total_charges=10.0, estimated=False), NS(order_id=2, total_charges=12.5, estimated=False)]
        r = build([p], o, f, [dec], [cand], [w], led)[0]
        assert r["tier"] == 1 and r["tier_name"] == "tier1_full_pipeline" and r["catalyst_type"] == "results"
        assert r["signal_price"] == 99.0 and r["entry_slippage_pct"] == pytest.approx(1.010, abs=1e-3)
        assert r["entry_vs_catalyst_pct"] == pytest.approx(2.041, abs=1e-3)
        assert r["exit_price"] == pytest.approx((6 * 105 + 4 * 106) / 10, abs=1e-3)
        assert r["exit_reason"] == "target_hit" and r["exits"] == 1 and r["entries"] == 1
        assert r["entry_time_ist"] == "09:30" and r["entry_hour_ist"] == "09" and r["exit_time_ist"] == "10:30"
        assert r["exit_day_ist"] == "2026-10-09" and r["held_minutes"] == 60.0
        assert r["gross_pnl"] == 48.0 and r["charges"] == 22.5 and r["charges_source"] == "ledger" and r["net_pnl"] == 25.5

    def test_unknown_fields_are_none_never_zero(self):
        r = build([pos()])[0]
        assert r["signal_price"] is None and r["entry_slippage_pct"] is None and r["exit_price"] is None
        assert r["catalyst_price"] is None and r["tier"] is None and r["tier_name"] is None
        assert r["exit_reason"] is None and r["charges"] is None and r["net_pnl"] is None
        assert r["orders_matched"] is False

    def test_zero_or_negative_signal_and_catalyst_prices_are_unknown(self):
        w = NS(id=7, source_tier=3, catalyst_type="volume_shock", catalyst_price=0.0, catalyst_price_source=None)
        cand = NS(id=3, signal_price=0.0)
        r = build([pos(watchlist_entry_id=7)], [order(1, "BUY", decision_id=5)], [], [NS(id=5, candidate_id=3)], [cand], [w])[0]
        assert r["signal_price"] is None and r["catalyst_price"] is None and r["entry_slippage_pct"] is None
        assert r["tier_name"] == "tier3_volume_shock"

    def test_negative_slippage_when_filled_below_signal(self):
        r = build([pos(entry=99.0)], [order(1, "BUY", decision_id=5)], [], [NS(id=5, candidate_id=3)], [NS(id=3, signal_price=100.0)])[0]
        assert r["entry_slippage_pct"] == -1.0

    def test_watchlist_entry_found_through_the_order_when_position_has_none(self):
        w = NS(id=9, source_tier=2, catalyst_type="insider", catalyst_price=50.0, catalyst_price_source="close")
        r = build([pos()], [order(1, "BUY", watch=9)], [], watch=[w])[0]
        assert r["tier"] == 2 and r["catalyst_type"] == "insider"

    def test_manual_sell_without_reason_is_manual(self):
        r = build([pos()], [order(1, "BUY"), order(2, "SELL", source="MANUAL")])[0]
        assert r["exit_reason"] == "manual"

    def test_reason_is_normalised(self):
        r = build([pos()], [order(1, "SELL", reason="  Trail  Stop ")])[0]
        assert r["exit_reason"] == "trail_stop"

    def test_blank_reason_is_unknown(self):
        assert build([pos()], [order(1, "SELL", reason="  ")])[0]["exit_reason"] is None

    def test_last_sell_names_the_exit_reason(self):
        o = [order(1, "BUY"), order(2, "SELL", reason="partial_target", at=utc(4, 30)),
             order(3, "SELL", reason="eod_squareoff", at=utc(4, 50))]
        r = build([pos()], o)[0]
        assert r["exit_reason"] == "eod_squareoff" and r["exits"] == 2

    def test_broker_imported_open_and_pnl_less_positions_are_left_out(self):
        no_open = pos(4)
        no_open.opened_at = None
        ps = [pos(1, broker_imported=True), pos(2, status="OPEN"), pos(3, pnl=None), no_open, pos(5)]
        assert [r["position_id"] for r in build(ps)] == [5]

    def test_loss_and_net(self):
        r = build([pos(pnl=-30.0, realized_cost_estimate=8.0)])[0]
        assert r["charges"] == 8.0 and r["charges_source"] == "estimate" and r["net_pnl"] == -38.0

    def test_ledger_must_cover_every_matched_order_else_estimate_is_used(self):
        o = [order(1, "BUY"), order(2, "SELL")]
        led = [NS(order_id=1, total_charges=10.0, estimated=False)]            # SELL missing from the ledger
        r = build([pos(realized_cost_estimate=15.0)], o, ledger=led)[0]
        assert r["charges"] == 15.0 and r["charges_source"] == "estimate"

    def test_estimated_ledger_row_is_flagged(self):
        o = [order(1, "BUY"), order(2, "SELL")]
        led = [NS(order_id=1, total_charges=10.0, estimated=False), NS(order_id=2, total_charges=5.0, estimated=True)]
        r = build([pos()], o, ledger=led)[0]
        assert r["charges_source"] == "ledger_estimated" and r["charges"] == 15.0

    def test_net_realized_pnl_is_the_last_resort(self):
        r = build([pos(pnl=50.0, net_realized_pnl=41.0)])[0]
        assert r["net_pnl"] == 41.0 and r["charges"] is None and r["charges_source"] == "net_realized_pnl"

    def test_zero_estimate_is_not_a_charge(self):
        r = build([pos(realized_cost_estimate=0.0)])[0]
        assert r["charges"] is None and r["net_pnl"] is None

    def test_two_trades_in_one_symbol_the_same_day_do_not_mix(self):
        p1 = pos(1, pnl=10.0, opened=utc(4, 0), closed=utc(4, 30))
        p2 = pos(2, pnl=-20.0, opened=utc(5, 0), closed=utc(5, 30))
        o = [order(1, "BUY", at=utc(4, 0)), order(2, "SELL", reason="target_hit", at=utc(4, 30)),
             order(3, "BUY", at=utc(5, 0)), order(4, "SELL", reason="stop_loss", at=utc(5, 30))]
        r = {x["position_id"]: x for x in build([p1, p2], o)}
        assert r[1]["exit_reason"] == "target_hit" and r[2]["exit_reason"] == "stop_loss"

    def test_a_broken_position_is_skipped_not_fatal(self):
        bad = pos(1, entry="oops")
        good = pos(2)
        bad.realized_pnl = object()                  # float() raises inside the record
        assert [r["position_id"] for r in build([bad, good])] == [2]

    def test_records_are_ordered_by_close_time(self):
        a = pos(1, closed=utc(7, 0))
        b = pos(2, closed=utc(5, 0))
        assert [r["position_id"] for r in build([a, b])] == [2, 1]


def rec(pnl, *, net=None, reason="target_hit", hour="09", tier="tier1_full_pipeline", cat="results", tab="watchlist",
        day="2026-10-09", slip=None, held=30.0, charges=None):
    return {"gross_pnl": pnl, "net_pnl": net, "exit_reason": reason, "entry_hour_ist": hour, "tier_name": tier,
            "catalyst_type": cat, "source_tab": tab, "exit_day_ist": day, "entry_slippage_pct": slip,
            "held_minutes": held, "charges": charges}


class TestReport:
    def test_empty(self):
        out = tr.expectancy_report([])
        assert out["trades"] == 0 and out["overall"] is None and out["by_exit_reason"] == {}

    def test_expectancy_win_rate_payoff(self):
        out = tr.expectancy_report([rec(100, net=90), rec(-40, net=-45), rec(60, net=50), rec(-10, net=-14)])
        o = out["overall"]
        assert o["trades"] == 4 and o["wins"] == 2 and o["losses"] == 2 and o["win_rate_pct"] == 50.0
        assert o["expectancy"] == round((90 - 45 + 50 - 14) / 4, 2)
        assert o["avg_win"] == 70.0 and o["avg_loss"] == -29.5 and o["payoff_ratio"] == round(70 / 29.5, 2)
        assert o["gross_pnl"] == 110.0 and o["net_pnl"] == 81.0 and o["net_known_trades"] == 4

    def test_win_is_judged_after_charges(self):
        o = tr.expectancy_report([rec(3, net=-2)])["overall"]
        assert o["wins"] == 0 and o["losses"] == 1

    def test_gross_used_where_net_unknown(self):
        o = tr.expectancy_report([rec(10, net=None), rec(20, net=5)])["overall"]
        assert o["expectancy"] == 7.5 and o["net_known_trades"] == 1 and o["net_pnl"] == 5.0

    def test_flat_trade_is_neither_win_nor_loss(self):
        o = tr.expectancy_report([rec(0.0)])["overall"]
        assert o["wins"] == 0 and o["losses"] == 0 and o["payoff_ratio"] is None

    def test_payoff_none_without_losses_or_wins(self):
        assert tr.expectancy_report([rec(10)])["overall"]["payoff_ratio"] is None
        assert tr.expectancy_report([rec(-10)])["overall"]["payoff_ratio"] is None

    def test_groups(self):
        rows = [rec(10, reason="target_hit", hour="09", tier="tier1_full_pipeline", cat="results", day="2026-10-08"),
                rec(-5, reason="stop_loss", hour="10", tier=None, cat=None, tab=None),
                rec(-5, reason=None, hour="10")]
        out = tr.expectancy_report(rows)
        assert set(out["by_exit_reason"]) == {"target_hit", "stop_loss", "unknown"}
        assert set(out["by_entry_hour_ist"]) == {"09", "10"} and out["by_entry_hour_ist"]["10"]["trades"] == 2
        assert set(out["by_tier"]) == {"tier1_full_pipeline", "no_watchlist_tier"}
        assert set(out["by_catalyst_type"]) == {"results", "none"}
        assert set(out["by_source_tab"]) == {"watchlist", "?"}
        assert set(out["by_day"]) == {"2026-10-08", "2026-10-09"}

    def test_low_sample_flag_and_threshold(self):
        out = tr.expectancy_report([rec(1)] * 4 + [rec(1, reason="x")] * 5)
        assert out["by_exit_reason"]["target_hit"]["low_sample"] is True
        assert out["by_exit_reason"]["x"]["low_sample"] is False
        assert tr.expectancy_report([rec(1)] * 2, min_trades_note=2)["overall"]["low_sample"] is False

    def test_slippage_charges_and_hold_averages(self):
        o = tr.expectancy_report([rec(1, slip=1.0, held=10, charges=4.0), rec(1, slip=0.5, held=20, charges=6.0), rec(1)])["overall"]
        assert o["avg_entry_slippage_pct"] == 0.75 and o["avg_held_minutes"] == 20.0 and o["charges"] == 10.0

    def test_unknown_averages_are_none(self):
        o = tr.expectancy_report([rec(1, held=None)])["overall"]
        assert o["avg_entry_slippage_pct"] is None and o["avg_held_minutes"] is None and o["charges"] is None


# ── real DB loader + real HTTP routes ───────────────────────────────────────────────────────────────────────────────
import models  # noqa: E402


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    models.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _now_naive(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(tzinfo=None)


def seed_trade(db, symbol="AAA", *, mode="REAL", pnl=48.0, opened_min=120, closed_min=60, reason="target_hit",
               signal=99.0, tier=1, catalyst=98.0, charges=(10.0, 12.5)):
    w = models.WatchlistEntry(mode=mode, symbol=symbol, catalyst_type="results", catalyst_price=catalyst,
                              catalyst_price_source="live", horizon_class="short", decay_half_life_days=1.0,
                              entry_band_pct=3.0, source_tier=tier, status="entered",
                              expires_at=_now_naive(-600))
    cand = models.TradeCandidate(mode=mode, symbol=symbol, signal_price=signal, consumed=True)
    db.add_all([w, cand]); db.commit()
    dec = models.TradeDecision(mode=mode, candidate_id=cand.id, symbol=symbol, decision_type="ENTRY", action="ENTER")
    db.add(dec); db.commit()
    buy = models.TradeOrder(mode=mode, symbol=symbol, side="BUY", qty=10, status="FILLED", decision_id=dec.id,
                            watchlist_entry_id=w.id, created_at=_now_naive(opened_min), updated_at=_now_naive(opened_min))
    sell = models.TradeOrder(mode=mode, symbol=symbol, side="SELL", qty=10, status="FILLED", exit_reason=reason,
                             created_at=_now_naive(closed_min), updated_at=_now_naive(closed_min))
    db.add_all([buy, sell]); db.commit()
    db.add_all([models.TradeFill(order_id=buy.id, qty=10, price=100.0, filled_at=_now_naive(opened_min)),
                models.TradeFill(order_id=sell.id, qty=10, price=104.8, filled_at=_now_naive(closed_min))])
    p = models.TradePosition(mode=mode, symbol=symbol, status="CLOSED", qty_open=0, avg_entry_price=100.0,
                             realized_pnl=pnl, opened_at=_now_naive(opened_min), closed_at=_now_naive(closed_min),
                             watchlist_entry_id=w.id, source_tab="watchlist")
    db.add(p); db.commit()
    if charges:
        for oid, c in zip((buy.id, sell.id), charges):
            db.add(models.TradeChargesLedger(order_id=oid, mode=mode, symbol=symbol, side="BUY", day="2026-10-10",
                                             total_charges=c))
        db.commit()
    return p


class TestLoader:
    def test_loads_a_full_record_from_real_tables(self, db):
        seed_trade(db)
        (r,) = tr.load_records(db, "REAL", 7)
        assert r["symbol"] == "AAA" and r["tier"] == 1 and r["catalyst_type"] == "results"
        assert r["signal_price"] == 99.0 and r["entry_slippage_pct"] == pytest.approx(1.010, abs=1e-3)
        assert r["exit_reason"] == "target_hit" and r["exit_price"] == pytest.approx(104.8)
        assert r["charges"] == 22.5 and r["net_pnl"] == 25.5 and r["charges_source"] == "ledger"

    def test_other_mode_symbol_filter_and_window(self, db):
        seed_trade(db, "AAA", mode="REAL")
        seed_trade(db, "BBB", mode="DEMO")
        seed_trade(db, "CCC", mode="REAL", opened_min=60 * 24 * 20, closed_min=60 * 24 * 19)
        assert [r["symbol"] for r in tr.load_records(db, "REAL", 7)] == ["AAA"]
        assert [r["symbol"] for r in tr.load_records(db, "DEMO", 7)] == ["BBB"]
        assert sorted(r["symbol"] for r in tr.load_records(db, "REAL", 30)) == ["AAA", "CCC"]
        assert [r["symbol"] for r in tr.load_records(db, "REAL", 30, symbol="ccc")] == ["CCC"]

    def test_empty_database(self, db):
        assert tr.load_records(db, "REAL", 7) == []

    def test_open_positions_are_not_loaded(self, db):
        p = seed_trade(db)
        p.status = "OPEN"; p.closed_at = None; db.commit()
        assert tr.load_records(db, "REAL", 7) == []

    def test_same_symbol_twice_keeps_each_trades_own_reason(self, db):
        seed_trade(db, "AAA", opened_min=240, closed_min=200, reason="stop_loss", pnl=-20.0)
        seed_trade(db, "AAA", opened_min=120, closed_min=60, reason="target_hit", pnl=30.0)
        by_pnl = {r["gross_pnl"]: r["exit_reason"] for r in tr.load_records(db, "REAL", 7)}
        assert by_pnl == {-20.0: "stop_loss", 30.0: "target_hit"}


class TestRoutes:
    @pytest.fixture()
    def client(self, db):
        from fastapi.testclient import TestClient
        import main
        main.app.dependency_overrides[main.get_db] = lambda: db
        yield TestClient(main.app)
        main.app.dependency_overrides.clear()

    def test_records_route(self, client, db):
        seed_trade(db, mode="DEMO")
        body = client.get("/positions/DEMO/records?days=7").json()
        assert body["mode"] == "DEMO" and body["total"] == 1 and body["returned"] == 1
        assert body["records"][0]["exit_reason"] == "target_hit" and body["records"][0]["net_pnl"] == 25.5

    def test_report_route(self, client, db):
        seed_trade(db, "AAA", mode="DEMO", reason="target_hit", pnl=48.0)
        seed_trade(db, "BBB", mode="DEMO", reason="stop_loss", pnl=-30.0, charges=(5.0, 5.0))
        body = client.get("/positions/DEMO/report").json()
        assert body["trades"] == 2 and set(body["by_exit_reason"]) == {"target_hit", "stop_loss"}
        assert body["by_exit_reason"]["target_hit"]["net_pnl"] == 25.5
        assert body["by_exit_reason"]["stop_loss"]["net_pnl"] == -40.0

    def test_limit_symbol_and_clamps(self, client, db):
        for s in ("AAA", "BBB", "CCC"):
            seed_trade(db, s, mode="DEMO")
        assert client.get("/positions/DEMO/records?limit=2").json()["returned"] == 2
        assert client.get("/positions/DEMO/records?limit=0").json()["returned"] == 1       # clamped to 1
        assert client.get("/positions/DEMO/records?symbol=bbb").json()["total"] == 1
        assert client.get("/positions/DEMO/records?days=9999").json()["days"] == 60

    def test_bad_mode_is_400_and_real_needs_admin(self, client):
        assert client.get("/positions/NOPE/records").status_code == 400
        assert client.get("/positions/NOPE/report").status_code == 400
        assert client.get("/positions/REAL/records").status_code in (401, 403)
        assert client.get("/positions/REAL/report").status_code in (401, 403)

    def test_routes_do_not_shadow_the_close_route(self, client):
        paths = [(r.path, tuple(sorted(r.methods))) for r in __import__("main").app.routes if hasattr(r, "methods")]
        assert ("/positions/{mode}/{position_id}/close", ("POST",)) in paths
