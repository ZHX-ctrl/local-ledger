PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income')),
    currency TEXT NOT NULL CHECK (currency IN ('CNY', 'SGD')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    name TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    note TEXT NOT NULL DEFAULT '',
    photo_id TEXT,
    fx_status TEXT NOT NULL CHECK (fx_status IN ('resolved', 'pending')),
    rate TEXT,
    quote_date TEXT,
    fx_source TEXT,
    fx_reason TEXT,
    cny_cents INTEGER,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (fx_status = 'resolved' AND cny_cents IS NOT NULL)
        OR (fx_status = 'pending' AND cny_cents IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS entries_date_idx ON entries(date, id);
CREATE INDEX IF NOT EXISTS entries_active_date_idx ON entries(deleted, date, id);
CREATE INDEX IF NOT EXISTS entries_category_idx ON entries(category_id, deleted);

CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    from_currency TEXT NOT NULL CHECK (from_currency IN ('CNY', 'SGD')),
    from_amount_cents INTEGER NOT NULL CHECK (from_amount_cents > 0),
    to_currency TEXT NOT NULL CHECK (to_currency IN ('CNY', 'SGD')),
    to_amount_cents INTEGER NOT NULL CHECK (to_amount_cents > 0),
    note TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (from_currency <> to_currency)
);

CREATE INDEX IF NOT EXISTS exchanges_date_idx ON exchanges(date, id);

CREATE TABLE IF NOT EXISTS internal_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('CNY', 'SGD')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    from_account TEXT NOT NULL,
    to_account TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS internal_transfers_date_idx ON internal_transfers(date, id);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency TEXT NOT NULL CHECK (currency IN ('CNY', 'SGD')),
    balance_cents INTEGER NOT NULL,
    cutoff TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS snapshots_wallet_idx ON snapshots(currency, cutoff, id);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_cache (
    consumption_date TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'pending')),
    rate TEXT,
    quote_date TEXT,
    source TEXT,
    reason TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'resolved' AND rate IS NOT NULL AND quote_date IS NOT NULL)
        OR (status = 'pending')
    )
);
