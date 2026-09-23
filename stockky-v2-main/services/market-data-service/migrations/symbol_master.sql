-- §4 symbol_master — ISIN-keyed, nightly-synced sector/industry taxonomy
CREATE TABLE IF NOT EXISTS symbol_master (
    current_symbol   TEXT        PRIMARY KEY,
    isin             TEXT        UNIQUE,
    prior_symbols    TEXT[],
    sector           TEXT,
    industry         TEXT,
    status           TEXT        DEFAULT 'active',
    merged_into      TEXT,
    last_verified_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sm_sector ON symbol_master (sector);
CREATE INDEX IF NOT EXISTS ix_sm_status ON symbol_master (status);
CREATE INDEX IF NOT EXISTS ix_sm_isin   ON symbol_master (isin);
