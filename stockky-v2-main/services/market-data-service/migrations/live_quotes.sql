-- §1 live_quotes — AngelOne WS tick feed + REAL-mode pricing source
CREATE TABLE IF NOT EXISTS live_quotes (
    symbol      TEXT        PRIMARY KEY,
    ltp         NUMERIC,
    ohlc_json   JSONB,
    volume      BIGINT,
    source      TEXT,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_live_quotes_updated ON live_quotes (updated_at);
