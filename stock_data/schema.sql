PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS stock_daily_bar (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    amount_10k REAL,
    forward_factor REAL,
    pre_close REAL,
    pct_change REAL,
    is_trading INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_bar_date
    ON stock_daily_bar(trade_date, stock_code);

CREATE TABLE IF NOT EXISTS stock_capital_daily (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    total_shares REAL,
    float_shares REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_capital_daily_date
    ON stock_capital_daily(trade_date, stock_code);

CREATE TABLE IF NOT EXISTS stock_corporate_action (
    stock_code TEXT NOT NULL,
    event_date TEXT NOT NULL,
    action_type INTEGER NOT NULL,
    cash_bonus REAL,
    allot_price REAL,
    share_bonus REAL,
    allotment REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, event_date, action_type),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_action_date
    ON stock_corporate_action(event_date, stock_code);

CREATE TABLE IF NOT EXISTS stock_trade_daily (
    stock_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    shareholder_count REAL,
    financing_balance_10k REAL,
    securities_lending_balance_shares REAL,
    northbound_holding_shares REAL,
    financing_buy_10k REAL,
    financing_repay_10k REAL,
    financing_net_buy_10k REAL,
    total_market_cap_10k REAL,
    dividend_yield_pct REAL,
    limit_status INTEGER,
    limit_order_amount_10k REAL,
    pledge_ratio_pct REAL,
    market_popularity_rank REAL,
    industry_popularity_rank REAL,
    raw_metrics_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_trade_daily_date
    ON stock_trade_daily(trade_date, stock_code);

CREATE TABLE IF NOT EXISTS stock_financial_report (
    stock_code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    announce_date TEXT NOT NULL,
    basic_eps REAL,
    deduct_eps REAL,
    book_value_per_share REAL,
    roe_pct REAL,
    total_assets REAL,
    total_liabilities REAL,
    total_equity REAL,
    operating_cash_flow REAL,
    net_profit REAL,
    revenue REAL,
    operating_profit REAL,
    parent_net_profit REAL,
    deduct_net_profit REAL,
    revenue_growth_pct REAL,
    net_profit_growth_pct REAL,
    gross_margin_pct REAL,
    debt_ratio_pct REAL,
    total_shares REAL,
    float_a_shares REAL,
    shareholder_count REAL,
    raw_values_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, report_date, announce_date),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);

CREATE INDEX IF NOT EXISTS idx_stock_financial_announce
    ON stock_financial_report(announce_date, stock_code);

CREATE INDEX IF NOT EXISTS idx_stock_financial_report_date
    ON stock_financial_report(report_date, stock_code);

CREATE TABLE IF NOT EXISTS stock_data_sync_state (
    stock_code TEXT NOT NULL,
    data_domain TEXT NOT NULL CHECK (
        data_domain IN ('bar', 'capital', 'action', 'trade', 'financial')
    ),
    last_success_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (stock_code, data_domain),
    FOREIGN KEY (stock_code) REFERENCES stock_master(stock_code)
);
