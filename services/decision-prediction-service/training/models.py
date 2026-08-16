# services/training-service/models.py
"""
SQLAlchemy models for the training service.

Tables:
- PredictionSnapshot: stores the feature snapshot at prediction time (immutable).
- PredictionOutcome: stores T+1 and T+5 evaluation results.
- TrainingRun: tracks each training pipeline run (for auditing and metrics).
"""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, JSON, Text, LargeBinary,
    create_engine, inspect, text, desc
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import numpy as np

# ---------- IST timezone helper ----------
IST = ZoneInfo("Asia/Kolkata")

def ist_now() -> datetime:
    """Return current time as a naive datetime in IST (UTC+5:30)."""
    return datetime.now(IST).replace(tzinfo=None)

# ---------- Base ----------
Base = declarative_base()

# ---------- Numpy conversion helper ----------
def convert_numpy(obj):
    """Recursively convert numpy types to Python native types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj

# ---------- Models ----------
class PredictionSnapshot(Base):
    """Immutable snapshot of a prediction at the time it was made."""
    __tablename__ = "prediction_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id = Column(String(50), unique=True, nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    price = Column(Float, nullable=False)
    decision = Column(String(20), nullable=False)
    confidence = Column(String(20))
    combined_score = Column(Float)
    technical_score = Column(Float)
    fundamental_score = Column(Float)
    news_score = Column(Float, nullable=True)
    prediction_score = Column(Float, nullable=True)
    market_score = Column(Float)
    market_sentiment_adjustment = Column(Float)
    training_score = Column(Float)
    event_risk = Column(Boolean, default=False)
    entry_range_low = Column(Float, nullable=True)
    entry_range_high = Column(Float, nullable=True)
    target = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    holding_period = Column(String(50), nullable=True)
    support = Column(Float, nullable=True)
    resistance = Column(Float, nullable=True)
    sector = Column(String(50), nullable=True)
    valuation = Column(Text, nullable=True)

    # Market sentiment at prediction time
    market_mood = Column(String(20), nullable=True)
    market_score_extra = Column(Float, nullable=True)
    nifty_change_pct = Column(Float, nullable=True)
    sensex_change_pct = Column(Float, nullable=True)

    # Technical features (snapshot of key indicators)
    rsi = Column(Float, nullable=True)
    macd = Column(String(20), nullable=True)
    ema = Column(String(20), nullable=True)
    volume_ratio = Column(Float, nullable=True)

    # Fundamental features (snapshot)
    debt_to_equity = Column(Float, nullable=True)
    roe = Column(Float, nullable=True)
    roce = Column(Float, nullable=True)

    # Additional feature snapshot as JSON (for flexibility)
    feature_snapshot = Column(JSON, nullable=True)

    # Metadata
    model_version = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=ist_now)

    # Outcome flags (updated after evaluation)
    t1_success = Column(Integer, default=0)   # 0 = pending, 1 = success, 2 = failed
    t5_success = Column(Integer, default=0)
    overall_success = Column(Integer, default=0)


class PredictionOutcome(Base):
    """Evaluation outcomes for a prediction (T+1, T+5)."""
    __tablename__ = "prediction_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    prediction_id = Column(String(50), nullable=False, index=True)
    evaluation_period = Column(String(10), nullable=False, index=True)
    evaluation_date = Column(DateTime, nullable=False)

    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)

    max_favorable_excursion = Column(Float, nullable=True)
    max_adverse_excursion = Column(Float, nullable=True)
    return_pct = Column(Float, nullable=True)

    entry_reached = Column(Integer, default=0)
    target_reached = Column(Integer, default=0)
    stop_loss_reached = Column(Integer, default=0)
    direction_correct = Column(Integer, default=0)
    success = Column(Integer, default=0)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=ist_now)


class PortfolioAccount(Base):
    """
    Singleton (always id=1): the shared dummy-money pool every paper trade
    draws capital from and returns proceeds to. Replaces the earlier
    design where every trade got its own fresh Rs 1,00,000 regardless of
    what else was open — that didn't model a real account, where opening
    five positions actually uses up the same pool of money. Top up via
    deposit_funds(); balance moves automatically as trades open/close.
    """
    __tablename__ = "portfolio_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cash_balance = Column(Float, nullable=False, default=100000.0)
    total_deposited = Column(Float, nullable=False, default=100000.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, default=ist_now)


class PortfolioTransaction(Base):
    """Audit trail for every balance movement — deposits, capital locked
    into a trade on open, proceeds returned on close. Lets the daily/
    weekly trade reports reconstruct what happened without recomputing
    from PaperTrade rows alone, and makes the running balance auditable."""
    __tablename__ = "portfolio_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    transaction_type = Column(String(20), nullable=False, index=True)  # deposit | trade_open | trade_close
    amount = Column(Float, nullable=False)  # positive = credit, negative = debit
    trade_id = Column(String(50), nullable=True, index=True)
    balance_after = Column(Float, nullable=False)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=ist_now, index=True)


class PaperTrade(Base):
    """
    A simulated position sized against a dummy capital allocation (e.g.
    Rs 1,00,000), opened against a real recorded prediction. Closed trade
    P&L is a more honest training signal than t1_success/t5_success alone:
    those are a binary same/next-day heuristic, this tracks what actually
    holding the position would have returned, checked daily against real
    price data, with the same target/stop-loss decision-engine set.
    """
    __tablename__ = "paper_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(50), unique=True, nullable=False, index=True)
    prediction_id = Column(String(50), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)

    capital_allocated = Column(Float, nullable=False)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_date = Column(DateTime, nullable=False, index=True)

    target = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    max_holding_days = Column(Integer, default=21)  # hard cap: see weekly review logic in trades.py
    weeks_held = Column(Integer, nullable=False, default=0)  # incremented at each weekly review checkpoint
    last_weekly_review_at = Column(DateTime, nullable=True)

    status = Column(String(20), nullable=False, default="OPEN", index=True)  # OPEN | CLOSED
    exit_price = Column(Float, nullable=True)
    exit_date = Column(DateTime, nullable=True)
    exit_reason = Column(String(30), nullable=True)  # target_hit | stop_loss_hit | max_holding_period | manual

    current_price = Column(Float, nullable=True)
    last_marked_at = Column(DateTime, nullable=True)

    pnl_amount = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)

    created_at = Column(DateTime, default=ist_now)


class TrainingRun(Base):
    """Tracks each training pipeline run with configuration and performance."""
    __tablename__ = "training_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_timestamp = Column(DateTime, nullable=False, index=True)
    config = Column(JSON, nullable=False)
    dataset_size = Column(Integer)
    num_symbols = Column(Integer)
    model_version = Column(String(50), nullable=True)
    walk_forward_metrics = Column(JSON, nullable=True)
    fold_details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=ist_now)


class ModelArtifact(Base):
    """
    The actual trained model, stored IN the database.
    """
    __tablename__ = "model_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(String(20), unique=True, nullable=False, index=True)
    status = Column(String(20), nullable=False, default="candidate", index=True)
    model_blob = Column(LargeBinary, nullable=False)
    scaler_blob = Column(LargeBinary, nullable=True)
    feature_columns = Column(JSON, nullable=True)
    config = Column(JSON, nullable=True)
    metrics = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=ist_now, index=True)
    promoted_at = Column(DateTime, nullable=True)


class ModelRegistry:
    """Postgres-backed model store."""
    def __init__(self, session_factory=None):
        if session_factory is None:
            import os
            db_url = os.environ.get("DATABASE_URL", "sqlite:///./training.db")
            engine = create_engine(db_url, echo=False)
            Base.metadata.create_all(engine)
            session_factory = sessionmaker(bind=engine)
        self._session_factory = session_factory

    def _next_version(self, session) -> str:
        latest = session.query(ModelArtifact).order_by(desc(ModelArtifact.id)).first()
        n = 1
        if latest and latest.version.startswith("v"):
            try:
                n = int(latest.version[1:]) + 1
            except ValueError:
                n = (latest.id or 0) + 1
        return f"v{n}"

    def save_production_model(self, model, scaler, config: dict, metrics: dict, feature_columns=None) -> str:
        return self._save(model, scaler, config, metrics, feature_columns, status="production")

    def save_candidate_model(self, model, scaler, config: dict, metrics: dict, feature_columns=None) -> str:
        return self._save(model, scaler, config, metrics, feature_columns, status="candidate")

    def _save(self, model, scaler, config, metrics, feature_columns, status) -> str:
        import io
        import joblib

        model_buf = io.BytesIO()
        joblib.dump(model, model_buf)
        model_bytes = model_buf.getvalue()

        scaler_bytes = None
        if scaler is not None:
            scaler_buf = io.BytesIO()
            joblib.dump(scaler, scaler_buf)
            scaler_bytes = scaler_buf.getvalue()

        config_sanitized = convert_numpy(config)
        metrics_sanitized = convert_numpy(metrics)

        session = self._session_factory()
        try:
            version = self._next_version(session)
            artifact = ModelArtifact(
                version=version,
                status=status,
                model_blob=model_bytes,
                scaler_blob=scaler_bytes,
                feature_columns=feature_columns,
                config=config_sanitized,
                metrics=metrics_sanitized,
                created_at=ist_now(),
                promoted_at=ist_now() if status == "production" else None,
            )
            if status == "production":
                session.query(ModelArtifact).filter(
                    ModelArtifact.status == "production"
                ).update({"status": "archived"})
            session.add(artifact)
            session.commit()
            return version
        finally:
            session.close()

    def promote_model(self, version: str) -> bool:
        session = self._session_factory()
        try:
            target = session.query(ModelArtifact).filter(ModelArtifact.version == version).first()
            if not target:
                return False
            session.query(ModelArtifact).filter(
                ModelArtifact.status == "production"
            ).update({"status": "archived"})
            target.status = "production"
            target.promoted_at = ist_now()
            session.commit()
            return True
        finally:
            session.close()

    def get_production_model(self):
        import io
        import joblib

        session = self._session_factory()
        try:
            artifact = session.query(ModelArtifact).filter(
                ModelArtifact.status == "production"
            ).order_by(desc(ModelArtifact.promoted_at)).first()
            if not artifact:
                return None
            model = joblib.load(io.BytesIO(artifact.model_blob))
            scaler = joblib.load(io.BytesIO(artifact.scaler_blob)) if artifact.scaler_blob else None
            meta = {
                "version": artifact.version,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                "promoted_at": artifact.promoted_at.isoformat() if artifact.promoted_at else None,
                "feature_columns": artifact.feature_columns,
                "config": artifact.config,
                "metrics": artifact.metrics,
            }
            return model, scaler, meta
        finally:
            session.close()

    def list_models(self):
        session = self._session_factory()
        try:
            rows = session.query(ModelArtifact).order_by(desc(ModelArtifact.created_at)).all()
            return [
                {
                    "version": r.version,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "promoted_at": r.promoted_at.isoformat() if r.promoted_at else None,
                    "metrics": r.metrics,
                    "config": r.config,
                }
                for r in rows
            ]
        finally:
            session.close()


# ---------- Migration helper (fixes missing columns and types) ----------
def ensure_schema(engine):
    """Add missing columns and fix column types if needed."""
    inspector = inspect(engine)

    # ---- prediction_snapshots ----
    table_name = "prediction_snapshots"
    if inspector.has_table(table_name):
        existing_columns = {col['name']: col['type'] for col in inspector.get_columns(table_name)}
        # Complete list of all columns defined in PredictionSnapshot (except 'id')
        required_columns = {
            # Basic fields
            'prediction_id': 'VARCHAR(50) UNIQUE NOT NULL',
            'symbol': 'VARCHAR(20) NOT NULL',
            'timestamp': 'TIMESTAMP NOT NULL',
            'price': 'FLOAT NOT NULL',
            'decision': 'VARCHAR(20) NOT NULL',
            'confidence': 'VARCHAR(20)',
            'combined_score': 'FLOAT',
            'technical_score': 'FLOAT',
            'fundamental_score': 'FLOAT',
            'news_score': 'FLOAT',
            'prediction_score': 'FLOAT',
            'market_score': 'FLOAT',
            'market_sentiment_adjustment': 'FLOAT',
            'training_score': 'FLOAT',
            'event_risk': 'BOOLEAN',
            'entry_range_low': 'FLOAT',
            'entry_range_high': 'FLOAT',
            'target': 'FLOAT',
            'stop_loss': 'FLOAT',
            'holding_period': 'VARCHAR(50)',
            'support': 'FLOAT',
            'resistance': 'FLOAT',
            'sector': 'VARCHAR(50)',
            'valuation': 'TEXT',
            # Market sentiment fields
            'market_mood': 'VARCHAR(20)',
            'market_score_extra': 'FLOAT',
            'nifty_change_pct': 'FLOAT',
            'sensex_change_pct': 'FLOAT',
            # Technical fields
            'rsi': 'FLOAT',
            'macd': 'VARCHAR(20)',
            'ema': 'VARCHAR(20)',
            'volume_ratio': 'FLOAT',
            # Fundamental fields
            'debt_to_equity': 'FLOAT',
            'roe': 'FLOAT',
            'roce': 'FLOAT',
            # JSON and metadata
            'feature_snapshot': 'JSON',
            'model_version': 'VARCHAR(50)',
            'created_at': 'TIMESTAMP',
            # Outcome flags
            't1_success': 'INTEGER',
            't5_success': 'INTEGER',
            'overall_success': 'INTEGER',
        }

        with engine.connect() as conn:
            # 1. Add missing columns
            for col_name, col_type in required_columns.items():
                if col_name not in existing_columns:
                    alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"Added column {col_name} to {table_name}")

            # 2. Fix column types for known mismatches
            # Confidence should be VARCHAR, not NUMERIC/DOUBLE
            if 'confidence' in existing_columns:
                col_type = existing_columns['confidence']
                if 'double' in str(col_type).lower() or 'numeric' in str(col_type).lower() or 'float' in str(col_type).lower():
                    alter_sql = f'ALTER TABLE {table_name} ALTER COLUMN confidence TYPE VARCHAR(20) USING confidence::text'
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print("Fixed confidence column type to VARCHAR(20)")

    # ---- prediction_outcomes ----
    table_name = "prediction_outcomes"
    if inspector.has_table(table_name):
        existing_columns = {col['name']: col['type'] for col in inspector.get_columns(table_name)}
        required_columns = {
            'prediction_id': 'VARCHAR(50) NOT NULL',
            'evaluation_period': 'VARCHAR(10) NOT NULL',
            'evaluation_date': 'TIMESTAMP NOT NULL',
            'open_price': 'FLOAT',
            'high_price': 'FLOAT',
            'low_price': 'FLOAT',
            'close_price': 'FLOAT',
            'max_favorable_excursion': 'FLOAT',
            'max_adverse_excursion': 'FLOAT',
            'return_pct': 'FLOAT',
            'entry_reached': 'INTEGER',
            'target_reached': 'INTEGER',
            'stop_loss_reached': 'INTEGER',
            'direction_correct': 'INTEGER',
            'success': 'INTEGER',
            'notes': 'TEXT',
            'created_at': 'TIMESTAMP',
        }
        with engine.connect() as conn:
            for col_name, col_type in required_columns.items():
                if col_name not in existing_columns:
                    alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"Added column {col_name} to {table_name}")

    # ---- training_runs ----
    table_name = "training_runs"
    if inspector.has_table(table_name):
        existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
        required_columns = {
            'run_timestamp': 'TIMESTAMP',
            'config': 'JSON',
            'dataset_size': 'INTEGER',
            'num_symbols': 'INTEGER',
            'model_version': 'VARCHAR(50)',
            'walk_forward_metrics': 'JSON',
            'fold_details': 'JSON',
            'created_at': 'TIMESTAMP',
        }
        with engine.connect() as conn:
            for col_name, col_type in required_columns.items():
                if col_name not in existing_columns:
                    alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"Added column {col_name} to {table_name}")

    # ---- paper_trades ----
    # weeks_held / last_weekly_review_at are new this round (weekly-cycle
    # review logic in trades.py). On a fresh DB, create_tables() already
    # creates paper_trades with every column via SQLAlchemy — this only
    # matters for a database where paper_trades already existed from an
    # earlier deploy and needs these two columns added in place.
    table_name = "paper_trades"
    if inspector.has_table(table_name):
        existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
        required_columns = {
            'weeks_held': 'INTEGER',
            'last_weekly_review_at': 'TIMESTAMP',
        }
        with engine.connect() as conn:
            for col_name, col_type in required_columns.items():
                if col_name not in existing_columns:
                    alter_sql = f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}'
                    conn.execute(text(alter_sql))
                    conn.commit()
                    print(f"Added column {col_name} to {table_name}")

# ---------- Database setup helpers ----------
def get_engine(database_url="sqlite:///./training.db"):
    return create_engine(database_url, echo=False)

def create_tables(engine):
    Base.metadata.create_all(engine)

def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()