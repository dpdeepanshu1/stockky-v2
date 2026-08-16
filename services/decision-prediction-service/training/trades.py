"""
Paper trading against ONE shared dummy-money portfolio (not a fresh pot
per trade — opening five positions actually draws down the same pool, the
way a real account works). Capital locks out of cash_balance when a trade
opens and the full exit value returns to cash_balance when it closes, so
the balance always reflects reality: cash + sum(open position values) is
conserved except for realized gains/losses.

Weekly-cycle exits: target/stop-loss are checked every mark-to-market
sweep and exit immediately if hit — no reason to wait out the week once a
target's been hit. Positions that hit neither are reviewed at each 7-day
checkpoint: taken off if already showing a solid gain (locks in a week's
work rather than round-tripping into next week and giving it back), held
into the next week otherwise, up to a 21-day (3-week) hard cap where it
closes regardless.

Mirrors evaluate.py's structure and conventions (own Session() per call,
yfinance for price data, explicit commit/rollback/close).
"""
import os
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

import models as db_models

logger = logging.getLogger("training-service.trades")

IST = ZoneInfo("Asia/Kolkata")

def ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)

DEFAULT_TRADE_CAPITAL = 10000.0
DEFAULT_STARTING_BALANCE = 100000.0

WEEK_DAYS = 7
MAX_HOLDING_DAYS = 21
WEEKLY_TAKE_PROFIT_PCT = 5.0

# ---------- Database engine and session factory ----------
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


def get_or_create_account(db):
    account = db.query(db_models.PortfolioAccount).filter(db_models.PortfolioAccount.id == 1).first()
    if account is None:
        account = db_models.PortfolioAccount(
            id=1, cash_balance=DEFAULT_STARTING_BALANCE,
            total_deposited=DEFAULT_STARTING_BALANCE, realized_pnl=0.0, updated_at=ist_now(),
        )
        db.add(account)
        db.commit()
        db.refresh(account)
    return account


def _log_transaction(db, account, transaction_type, amount, trade_id=None, note=None):
    db.add(db_models.PortfolioTransaction(
        transaction_type=transaction_type, amount=amount, trade_id=trade_id,
        balance_after=account.cash_balance, note=note, created_at=ist_now(),
    ))


def deposit_funds(amount: float, note: str = None):
    if amount <= 0:
        return None, "Deposit amount must be positive"
    db = SessionLocal()
    try:
        account = get_or_create_account(db)
        account.cash_balance += amount
        account.total_deposited += amount
        account.updated_at = ist_now()
        _log_transaction(db, account, "deposit", amount, note=note or "Manual deposit")
        db.commit()
        db.refresh(account)
        return account, None
    except Exception as e:
        logger.error(f"Error depositing funds: {e}")
        db.rollback()
        return None, str(e)
    finally:
        db.close()


def get_portfolio_summary():
    db = SessionLocal()
    try:
        account = get_or_create_account(db)
        open_trades = db.query(db_models.PaperTrade).filter(db_models.PaperTrade.status == "OPEN").all()
        open_value = sum((t.current_price or t.entry_price) * t.quantity for t in open_trades)
        open_pnl = sum(t.pnl_amount or 0 for t in open_trades)
        closed_trades = db.query(db_models.PaperTrade).filter(db_models.PaperTrade.status == "CLOSED").all()
        wins = [t for t in closed_trades if (t.pnl_amount or 0) > 0]
        win_rate = round(len(wins) / len(closed_trades) * 100, 1) if closed_trades else None
        return {
            "cash_balance": round(account.cash_balance, 2),
            "total_deposited": round(account.total_deposited, 2),
            "realized_pnl": round(account.realized_pnl, 2),
            "open_positions_value": round(open_value, 2),
            "open_positions_pnl": round(open_pnl, 2),
            "total_equity": round(account.cash_balance + open_value, 2),
            "open_positions": len(open_trades),
            "closed_positions": len(closed_trades),
            "win_rate": win_rate,
        }
    finally:
        db.close()


def _dynamic_trade_capital(account) -> float:
    """AI-adjusted default trade size: scales up gradually as the account
    becomes profitable — rewards sizing up only after the system has
    actually proven an edge with real closed trades, not upfront. Never
    scales below the base amount on a losing stretch; that's what
    stop-loss and the weekly review are for, not shrinking position size
    reactively here. Hard-capped as a percent of current balance
    regardless of how the performance scaling computes, so a long
    winning streak can't compound into one oversized position."""
    base = DEFAULT_TRADE_CAPITAL
    performance_ratio = (account.realized_pnl / account.total_deposited) if account.total_deposited > 0 else 0.0
    # Scales linearly up to 2x base at +50% cumulative realized return.
    scale = 1.0 + max(0.0, min(performance_ratio, 0.5)) * 2.0
    suggested = base * scale
    max_by_balance = account.cash_balance * 0.15  # never more than 15% of current cash in one trade
    if max_by_balance <= 0:
        return base
    return round(min(suggested, max_by_balance), -2)


def open_trade(prediction_id: str, capital: float = None, max_holding_days: int = MAX_HOLDING_DAYS):
    db = SessionLocal()
    try:
        existing = db.query(db_models.PaperTrade).filter(
            db_models.PaperTrade.prediction_id == prediction_id
        ).first()
        if existing:
            return existing, False, None

        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            return None, False, f"No prediction {prediction_id}"
        if not pred.price or pred.price <= 0:
            return None, False, f"Invalid entry price {pred.price}"

        account = get_or_create_account(db)
        requested = capital if capital and capital > 0 else _dynamic_trade_capital(account)
        available_capital = min(requested, account.cash_balance)

        quantity = int(available_capital // pred.price)
        if quantity < 1:
            return None, False, (
                f"Not enough cash balance (Rs {account.cash_balance:.2f}) to buy even "
                f"1 share of {pred.symbol} at Rs {pred.price}"
            )

        capital_used = quantity * pred.price

        trade = db_models.PaperTrade(
            trade_id=f"TRD-{datetime.now(IST).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
            prediction_id=prediction_id,
            symbol=pred.symbol,
            capital_allocated=capital_used,
            entry_price=pred.price,
            quantity=quantity,
            entry_date=pred.timestamp or ist_now(),
            target=pred.target,
            stop_loss=pred.stop_loss,
            max_holding_days=max_holding_days,
            weeks_held=0,
            last_weekly_review_at=None,
            status="OPEN",
            current_price=pred.price,
            last_marked_at=ist_now(),
            pnl_amount=0.0,
            pnl_pct=0.0,
            created_at=ist_now(),
        )
        db.add(trade)

        account.cash_balance -= capital_used
        account.updated_at = ist_now()
        _log_transaction(db, account, "trade_open", -capital_used, trade_id=trade.trade_id,
                          note=f"{quantity} x {pred.symbol} @ {pred.price}")

        db.commit()
        db.refresh(trade)
        logger.info(f"Opened trade {trade.trade_id} for {pred.symbol}: {quantity} @ {pred.price} "
                    f"(Rs {capital_used:.2f}, balance now Rs {account.cash_balance:.2f})")
        return trade, True, None
    except Exception as e:
        logger.error(f"Error opening trade for {prediction_id}: {e}")
        db.rollback()
        return None, False, str(e)
    finally:
        db.close()


def open_trades_bulk(prediction_ids, capital: float = None, max_holding_days: int = MAX_HOLDING_DAYS):
    opened, skipped, failed = [], [], []
    for pid in prediction_ids:
        trade, was_new, error = open_trade(pid, capital=capital, max_holding_days=max_holding_days)
        if trade is None:
            failed.append({"prediction_id": pid, "reason": error})
        elif was_new:
            opened.append(trade.trade_id)
        else:
            skipped.append({"prediction_id": pid, "existing_trade_id": trade.trade_id})
    return opened, skipped, failed


def _fetch_latest_price(symbol: str):
    try:
        ticker = yf.Ticker(symbol + ".NS")
        hist = ticker.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"Could not fetch price for {symbol}: {e}")
        return None


def _close_trade(db, account, trade, price: float, exit_reason: str):
    exit_value = price * trade.quantity
    trade.current_price = price
    trade.pnl_amount = round((price - trade.entry_price) * trade.quantity, 2)
    trade.pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)
    trade.status = "CLOSED"
    trade.exit_price = price
    trade.exit_date = ist_now()
    trade.exit_reason = exit_reason
    trade.last_marked_at = ist_now()

    account.cash_balance += exit_value
    account.realized_pnl += trade.pnl_amount
    account.updated_at = ist_now()
    _log_transaction(db, account, "trade_close", exit_value, trade_id=trade.trade_id,
                      note=f"{exit_reason}, P&L {trade.pnl_pct}%")

    logger.info(
        f"Closed {trade.trade_id} ({trade.symbol}) — {exit_reason}, P&L {trade.pnl_pct}% "
        f"(Rs {trade.pnl_amount}), balance now Rs {account.cash_balance:.2f}"
    )


def mark_to_market(trade_id: str):
    db = SessionLocal()
    try:
        trade = db.query(db_models.PaperTrade).filter(
            db_models.PaperTrade.trade_id == trade_id,
            db_models.PaperTrade.status == "OPEN",
        ).first()
        if not trade:
            return None

        price = _fetch_latest_price(trade.symbol)
        if price is None:
            return trade

        trade.current_price = price
        trade.last_marked_at = ist_now()
        trade.pnl_amount = round((price - trade.entry_price) * trade.quantity, 2)
        trade.pnl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 2)

        days_held = (ist_now() - trade.entry_date).days
        exit_reason = None

        if trade.target and price >= trade.target:
            exit_reason = "target_hit"
        elif trade.stop_loss and price <= trade.stop_loss:
            exit_reason = "stop_loss_hit"
        elif days_held >= (trade.max_holding_days or MAX_HOLDING_DAYS):
            exit_reason = "max_holding_period"
        else:
            current_week = days_held // WEEK_DAYS
            if current_week > trade.weeks_held and days_held > 0 and days_held % WEEK_DAYS == 0:
                trade.weeks_held = current_week
                trade.last_weekly_review_at = ist_now()
                if trade.pnl_pct >= WEEKLY_TAKE_PROFIT_PCT:
                    exit_reason = "weekly_review_profit_take"

        if exit_reason:
            account = get_or_create_account(db)
            _close_trade(db, account, trade, price, exit_reason)

        db.commit()
        db.refresh(trade)
        return trade
    except Exception as e:
        logger.error(f"Error marking trade {trade_id} to market: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def close_trade_manually(trade_id: str):
    db = SessionLocal()
    try:
        trade = db.query(db_models.PaperTrade).filter(
            db_models.PaperTrade.trade_id == trade_id,
            db_models.PaperTrade.status == "OPEN",
        ).first()
        if not trade:
            return None

        price = _fetch_latest_price(trade.symbol)
        if price is None:
            price = trade.current_price or trade.entry_price

        account = get_or_create_account(db)
        _close_trade(db, account, trade, price, "manual")
        db.commit()
        db.refresh(trade)
        return trade
    except Exception as e:
        logger.error(f"Error manually closing trade {trade_id}: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def mark_all_open_trades():
    db = SessionLocal()
    try:
        open_trade_ids = [
            t.trade_id for t in db.query(db_models.PaperTrade).filter(
                db_models.PaperTrade.status == "OPEN"
            ).all()
        ]
    finally:
        db.close()

    marked, closed = 0, 0
    for tid in open_trade_ids:
        result = mark_to_market(tid)
        if result is not None:
            marked += 1
            if result.status == "CLOSED":
                closed += 1
    logger.info(f"Mark-to-market sweep: {marked} marked, {closed} closed")
    return {"marked": marked, "closed": closed, "total_open_before": len(open_trade_ids)}


def get_trade_summary():
    return get_portfolio_summary()


def _build_trade_report(period: str, lookback: int):
    db = SessionLocal()
    try:
        cutoff = ist_now() - (timedelta(days=lookback) if period == "daily" else timedelta(weeks=lookback))
        closes = db.query(db_models.PaperTrade).filter(
            db_models.PaperTrade.status == "CLOSED",
            db_models.PaperTrade.exit_date >= cutoff,
        ).all()
        opens = db.query(db_models.PaperTrade).filter(
            db_models.PaperTrade.entry_date >= cutoff,
        ).all()

        def _bucket_key(ts):
            if period == "daily":
                return ts.date().isoformat()
            iso_year, iso_week, _ = ts.isocalendar()
            return f"{iso_year}-W{iso_week:02d}"

        buckets = {}

        def _get_bucket(key):
            return buckets.setdefault(key, {
                "period": key, "trades_opened": 0, "trades_closed": 0,
                "realized_pnl": 0.0, "wins": 0, "losses": 0,
                "capital_deployed": 0.0,
            })

        for t in opens:
            b = _get_bucket(_bucket_key(t.entry_date))
            b["trades_opened"] += 1
            b["capital_deployed"] += t.capital_allocated or 0

        for t in closes:
            b = _get_bucket(_bucket_key(t.exit_date))
            b["trades_closed"] += 1
            b["realized_pnl"] += t.pnl_amount or 0
            if (t.pnl_amount or 0) > 0:
                b["wins"] += 1
            else:
                b["losses"] += 1

        results = []
        for key in sorted(buckets.keys(), reverse=True):
            b = buckets[key]
            b["realized_pnl"] = round(b["realized_pnl"], 2)
            b["capital_deployed"] = round(b["capital_deployed"], 2)
            b["win_rate"] = round(b["wins"] / b["trades_closed"] * 100, 1) if b["trades_closed"] else None
            results.append(b)
        return results
    finally:
        db.close()


def get_daily_trade_report(days: int = 30):
    return _build_trade_report("daily", days)


def get_weekly_trade_report(weeks: int = 12):
    return _build_trade_report("weekly", weeks)

# ── Clear All + Backup (paper trades / tracking) ──────────────────────────
import json, os
from datetime import datetime, timezone

BACKUP_DIR = os.getenv("TRADE_BACKUP_DIR", "/app/data/trade_backups")

def clear_all_with_backup(db_session, account_id=None):
    """Reset current paper tracking after writing a JSON backup. Returns backup meta."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(BACKUP_DIR, f"backup_{ts}.json")
    payload = {"created_at": ts, "trades": [], "note": "Stockky clear-all backup"}
    try:
        # Best-effort dump of open/closed trades if models available
        try:
            from models import PaperTrade
            rows = db_session.query(PaperTrade).all() if db_session is not None else []
            payload["trades"] = [
                {c.name: getattr(r, c.name, None) for c in r.__table__.columns}
                for r in rows
            ]
            for r in rows:
                db_session.delete(r)
            if db_session is not None:
                db_session.commit()
        except Exception as inner:
            payload["dump_error"] = str(inner)
        with open(path, "w") as f:
            json.dump(payload, f, default=str)
        return {"ok": True, "backup_path": path, "count": len(payload.get("trades") or [])}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def list_trade_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    files = sorted([p for p in os.listdir(BACKUP_DIR) if p.endswith(".json")], reverse=True)
    return {"backups": files}


def add_quantity_to_trade(db_session, trade_id: str, quantity: float, price=None):
    """Add more shares to an open paper trade (Groww-style average-up)."""
    try:
        from models import PaperTrade, PortfolioAccount
    except Exception as e:
        return {"ok": False, "error": f"models unavailable: {e}"}
    if quantity is None or float(quantity) <= 0:
        return {"ok": False, "error": "quantity must be > 0"}
    quantity = float(quantity)
    try:
        if db_session is None:
            # best-effort without session: cannot mutate DB
            return {"ok": False, "error": "database session required on server"}
        trade = db_session.query(PaperTrade).filter_by(id=trade_id).first()
        if not trade:
            return {"ok": False, "error": "trade not found"}
        if getattr(trade, "status", "open") not in ("open", "OPEN", None):
            return {"ok": False, "error": "trade is not open"}
        entry = float(getattr(trade, "entry_price", 0) or 0)
        old_qty = float(getattr(trade, "quantity", 0) or 0)
        px = float(price) if price is not None else entry
        if px <= 0:
            return {"ok": False, "error": "invalid price"}
        cost = px * quantity
        # deduct cash if portfolio account exists
        try:
            acct = db_session.query(PortfolioAccount).first()
            if acct is not None and hasattr(acct, "cash_balance"):
                if float(acct.cash_balance) < cost:
                    return {"ok": False, "error": "insufficient cash balance"}
                acct.cash_balance = float(acct.cash_balance) - cost
        except Exception:
            pass
        new_qty = old_qty + quantity
        new_entry = ((entry * old_qty) + (px * quantity)) / new_qty if new_qty else px
        trade.quantity = new_qty
        trade.entry_price = round(new_entry, 4)
        db_session.commit()
        return {
            "ok": True,
            "trade_id": trade_id,
            "quantity": new_qty,
            "entry_price": trade.entry_price,
            "added": quantity,
            "added_price": px,
            "cost": cost,
        }
    except Exception as e:
        try:
            db_session.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(e)}