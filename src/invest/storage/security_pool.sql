PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS stock_master (
    stock_code TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    security_kind INTEGER,
    list_date TEXT,
    initial_name TEXT,
    trade_unit REAL,
    min_price_tick REAL,
    price_precision INTEGER,
    first_seen_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_current (
    stock_code TEXT PRIMARY KEY,
    data_date TEXT NOT NULL,
    stock_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_all_a INTEGER,
    is_main_board INTEGER,
    is_gem INTEGER,
    is_star INTEGER,
    is_bj_a INTEGER,
    is_hs300 INTEGER,
    is_zz500 INTEGER,
    is_zz1000 INTEGER,
    is_a500 INTEGER,
    is_stock_connect INTEGER,
    is_marginable INTEGER,
    is_st INTEGER,
    is_delisting_board INTEGER,
    is_suspended INTEGER,
    has_convertible_bond INTEGER,
    industry_code TEXT,
    industry_name TEXT,
    region_code TEXT,
    region_name TEXT,
    total_shares_10k REAL,
    float_shares_10k REAL,
    free_float_shares_10k REAL,
    total_market_cap_100m REAL,
    float_market_cap_100m REAL,
    turnover_rate REAL,
    amount_prev_1d_10k REAL,
    pe_ttm REAL,
    pb_mrq REAL,
    dividend_yield REAL,
    is_tradable INTEGER,
    exclusion_reasons TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_current_active
    ON stock_current(is_active, is_tradable);
CREATE INDEX IF NOT EXISTS idx_stock_current_industry
    ON stock_current(industry_code);
CREATE INDEX IF NOT EXISTS idx_stock_current_indices
    ON stock_current(is_hs300, is_zz500, is_zz1000);

CREATE TABLE IF NOT EXISTS stock_snapshot (
    snapshot_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    is_active INTEGER NOT NULL,
    is_all_a INTEGER,
    is_main_board INTEGER,
    is_gem INTEGER,
    is_star INTEGER,
    is_bj_a INTEGER,
    is_hs300 INTEGER,
    is_zz500 INTEGER,
    is_zz1000 INTEGER,
    is_a500 INTEGER,
    is_stock_connect INTEGER,
    is_marginable INTEGER,
    is_st INTEGER,
    is_delisting_board INTEGER,
    is_suspended INTEGER,
    has_convertible_bond INTEGER,
    industry_code TEXT,
    industry_name TEXT,
    region_code TEXT,
    region_name TEXT,
    total_shares_10k REAL,
    float_shares_10k REAL,
    free_float_shares_10k REAL,
    total_market_cap_100m REAL,
    float_market_cap_100m REAL,
    turnover_rate REAL,
    amount_prev_1d_10k REAL,
    pe_ttm REAL,
    pb_mrq REAL,
    dividend_yield REAL,
    is_tradable INTEGER,
    exclusion_reasons TEXT,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, stock_code),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_snapshot_stock_date
    ON stock_snapshot(stock_code, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_stock_snapshot_date_active
    ON stock_snapshot(snapshot_date, is_active, is_tradable);

CREATE TABLE IF NOT EXISTS stock_sector_snapshot (
    snapshot_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    sector_code TEXT NOT NULL DEFAULT '',
    sector_name TEXT NOT NULL,
    sector_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (
        snapshot_date,
        stock_code,
        sector_code,
        sector_name,
        sector_type,
        source
    )
);

CREATE INDEX IF NOT EXISTS idx_stock_sector_stock_date
    ON stock_sector_snapshot(stock_code, snapshot_date);

CREATE TABLE IF NOT EXISTS ipo_info (
    security_code TEXT NOT NULL,
    security_name TEXT,
    issue_type TEXT NOT NULL,
    subscription_date TEXT NOT NULL,
    subscription_price REAL,
    subscription_code TEXT,
    max_subscription REAL,
    issue_pe REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (security_code, subscription_date, issue_type)
);

CREATE TABLE IF NOT EXISTS convertible_bond_info (
    bond_code TEXT PRIMARY KEY,
    underlying_code TEXT,
    conversion_price REAL,
    remaining_size REAL,
    maturity_date TEXT,
    bond_price REAL,
    stock_price REAL,
    premium_rate REAL,
    conversion_value REAL,
    data_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etf_info (
    index_code TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    etf_name TEXT,
    current_price REAL,
    previous_close REAL,
    iopv REAL,
    shares_10k REAL,
    size_100m REAL,
    data_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (index_code, etf_code)
);

-- ETF 当前映射每日归档；保留历史 IOPV、份额和规模快照。
CREATE TABLE IF NOT EXISTS etf_snapshot (
    snapshot_date TEXT NOT NULL,
    index_code TEXT NOT NULL,
    etf_code TEXT NOT NULL,
    etf_name TEXT,
    current_price REAL,
    previous_close REAL,
    iopv REAL,
    shares_10k REAL,
    size_100m REAL,
    premium_discount_pct REAL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_date, index_code, etf_code)
);

CREATE INDEX IF NOT EXISTS idx_etf_snapshot_code_date
    ON etf_snapshot(etf_code, snapshot_date);

-- ETF 主表覆盖所有 ETF 分类，不局限于存在指数映射的品种。
CREATE TABLE IF NOT EXISTS etf_master (
    etf_code TEXT PRIMARY KEY,
    etf_name TEXT,
    exchange TEXT NOT NULL,
    list_date TEXT,
    underlying_code TEXT,
    underlying_market_code TEXT,
    is_t0 INTEGER NOT NULL DEFAULT 0,
    is_marginable INTEGER,
    trade_unit REAL,
    price_precision INTEGER,
    total_shares_10k REAL,
    first_seen_date TEXT NOT NULL,
    raw_info_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_etf_master_t0
    ON etf_master(is_t0, exchange);

-- ETF 日频行情与基金指标。GP 指标发布频率可能低于交易日频率。
CREATE TABLE IF NOT EXISTS etf_daily (
    etf_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    pre_close REAL,
    forward_factor REAL,
    pct_change REAL,
    volume REAL,
    amount_10k REAL,
    is_trading INTEGER,
    net_subscription_10k REAL,
    unit_nav REAL,
    cumulative_nav REAL,
    shares_10k REAL,
    size_10k REAL,
    premium_discount_pct REAL,
    raw_metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, trade_date),
    FOREIGN KEY (etf_code) REFERENCES etf_master(etf_code)
);

CREATE INDEX IF NOT EXISTS idx_etf_daily_date
    ON etf_daily(trade_date, etf_code);

-- 全市场 ETF 份额、规模和净申赎指标（SC08、SC38）。
CREATE TABLE IF NOT EXISTS etf_market_daily (
    trade_date TEXT PRIMARY KEY,
    total_shares_100m REAL,
    net_subscription_shares_100m REAL,
    total_size_100m REAL,
    net_subscription_size_100m REAL,
    raw_metrics_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etf_data_sync_state (
    etf_code TEXT NOT NULL,
    data_domain TEXT NOT NULL,
    last_success_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (etf_code, data_domain),
    FOREIGN KEY (etf_code) REFERENCES etf_master(etf_code)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data_date TEXT NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stock_count INTEGER,
    error_message TEXT
);
