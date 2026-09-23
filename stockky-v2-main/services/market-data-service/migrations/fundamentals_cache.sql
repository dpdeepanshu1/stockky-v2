-- §7 fundamentals_cache — nightly batch worker writes here, live scans read here
CREATE TABLE IF NOT EXISTS fundamentals_cache (
    symbol      TEXT        PRIMARY KEY,
    data_json   JSONB,
    updated_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_fc_updated ON fundamentals_cache (updated_at);
