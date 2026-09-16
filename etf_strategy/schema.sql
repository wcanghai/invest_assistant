CREATE TABLE IF NOT EXISTS etf_classification (
 etf_code TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
 known_at TEXT NOT NULL, asset_class TEXT NOT NULL, market TEXT NOT NULL,
 exposure_group TEXT NOT NULL, factor_style TEXT, pool TEXT NOT NULL,
 source TEXT NOT NULL, verified INTEGER NOT NULL, historical_verified INTEGER NOT NULL,
 PRIMARY KEY(etf_code,valid_from,known_at)
);
CREATE TABLE IF NOT EXISTS etf_adjustment (
 etf_code TEXT NOT NULL, trade_date TEXT NOT NULL, factor REAL NOT NULL CHECK(factor>0),
 source TEXT NOT NULL, fetched_at TEXT NOT NULL,
 PRIMARY KEY(etf_code,trade_date)
);
CREATE TABLE IF NOT EXISTS etf_action (
 event_id TEXT PRIMARY KEY, etf_code TEXT NOT NULL, record_date TEXT NOT NULL,
 ex_date TEXT NOT NULL, pay_date TEXT NOT NULL, cash_per_share REAL NOT NULL,
 share_multiplier REAL NOT NULL CHECK(share_multiplier>0), known_at TEXT NOT NULL,
 source TEXT NOT NULL, verified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS etf_validation (
 etf_code TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
 validated_at TEXT NOT NULL, source TEXT NOT NULL, factor_direction TEXT NOT NULL,
 actions_complete INTEGER NOT NULL, identity_complete INTEGER NOT NULL,
 trading_rules_verified INTEGER NOT NULL,
 PRIMARY KEY(etf_code,start_date,end_date)
);
CREATE TABLE IF NOT EXISTS etf_calendar (
 trade_date TEXT PRIMARY KEY, source TEXT NOT NULL, verified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS etf_identity (
 etf_code TEXT PRIMARY KEY, list_date TEXT NOT NULL, end_date TEXT,
 known_at TEXT NOT NULL, source TEXT NOT NULL, verified INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS etf_rule (
 etf_code TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
 known_at TEXT NOT NULL, lot_size INTEGER NOT NULL CHECK(lot_size>0),
 price_tick REAL NOT NULL CHECK(price_tick>0), is_t0 INTEGER NOT NULL,
 source TEXT NOT NULL, verified INTEGER NOT NULL,
 PRIMARY KEY(etf_code,valid_from)
);
CREATE TABLE IF NOT EXISTS etf_daily_provenance (
 etf_code TEXT NOT NULL, trade_date TEXT NOT NULL, domain TEXT NOT NULL,
 published_at TEXT, fetched_at TEXT NOT NULL, source TEXT NOT NULL,
 PRIMARY KEY(etf_code,trade_date,domain)
);
CREATE TABLE IF NOT EXISTS etf_universe_validation (
 start_date TEXT NOT NULL, end_date TEXT NOT NULL, source TEXT NOT NULL,
 validated_at TEXT NOT NULL, terminated_products_complete INTEGER NOT NULL,
 historical_mappings_complete INTEGER NOT NULL, PRIMARY KEY(start_date,end_date)
);
