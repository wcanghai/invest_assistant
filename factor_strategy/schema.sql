PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS strategy_run (
    run_id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    factor_date TEXT,
    start_date TEXT,
    end_date TEXT,
    model_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS factor_score_daily (
    factor_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    model_version TEXT NOT NULL,
    universe_flag INTEGER NOT NULL,
    exclusion_reason TEXT,
    value_score REAL,
    quality_score REAL,
    growth_score REAL,
    momentum_score REAL,
    low_risk_score REAL,
    flow_sentiment_score REAL,
    liquidity_score REAL,
    total_score REAL,
    valid_factor_count INTEGER NOT NULL,
    factor_detail_json TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (factor_date, stock_code, model_version)
);

CREATE INDEX IF NOT EXISTS idx_factor_score_date_rank
    ON factor_score_daily(factor_date, model_version, total_score DESC);

CREATE TABLE IF NOT EXISTS portfolio_target (
    rebalance_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    model_version TEXT NOT NULL,
    rank_no INTEGER NOT NULL,
    target_weight REAL NOT NULL,
    total_score REAL NOT NULL,
    industry_code TEXT,
    constraint_notes TEXT,
    run_id TEXT NOT NULL,
    PRIMARY KEY (rebalance_date, stock_code, model_version)
);

CREATE TABLE IF NOT EXISTS backtest_nav (
    backtest_id TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    nav REAL NOT NULL,
    cash REAL NOT NULL,
    holding_value REAL NOT NULL,
    daily_return REAL,
    benchmark_return REAL,
    drawdown REAL,
    PRIMARY KEY (backtest_id, trade_date)
);

CREATE TABLE IF NOT EXISTS backtest_trade (
    backtest_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    trade_date TEXT,
    stock_code TEXT NOT NULL,
    side TEXT NOT NULL,
    target_quantity INTEGER NOT NULL,
    filled_quantity INTEGER NOT NULL,
    trade_price REAL,
    fee REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (backtest_id, order_id)
);
