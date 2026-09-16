"""股票详细数据获取、处理和存储集成测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

from security_pool import db as pool_db
from stock_data.fetcher import (
    CORE_FN_FIELDS,
    FN_FIELDS,
    GP_FIELDS,
    fetch_trade_metrics,
    yearly_date_ranges,
)
from stock_data.main import bulk_full_load, check_data, daily_update, full_load
from stock_data.processor import build_action_rows, flatten_daily_bars
from tdx_client import TdxClient


class FakeStockDataApi:
    """提供一只股票所有详细数据域的假通达信接口。"""

    def __init__(self) -> None:
        # 初始化 GP 请求窗口记录。
        self.gp_requests: list[tuple[str, str]] = []
        self.bar_requests: list[tuple[str, str]] = []
        self.capital_requests: list[tuple[str, str]] = []
        self.action_requests: list[tuple[str, str]] = []
        self.financial_requests: list[tuple[str, str]] = []
        self.financial_field_requests: list[tuple[str, ...]] = []

    def initialize(self, script_file: str) -> int:
        # 模拟接口初始化成功。
        return int(bool(script_file))

    def get_market_data(
        self,
        field_list,
        stock_list,
        period,
        start_time,
        end_time,
        count,
        dividend_type,
        fill_data,
    ):
        # 返回三天不复权日线字段字典。
        self.bar_requests.append((start_time, end_time))
        dates = ["2026-08-26", "2026-08-27", "2026-08-28"]
        values = {
            "Open": [10.0, 10.5, 10.8],
            "High": [10.8, 11.0, 11.2],
            "Low": [9.9, 10.4, 10.7],
            "Close": [10.5, 10.8, 11.0],
            "Volume": [1000, 1100, 1200],
            "Amount": [10.2, 11.8, 13.1],
            "ForwardFactor": [1.0, 1.0, 1.0],
        }
        return {
            field: pd.DataFrame({stock_list[0]: values[field]}, index=dates)
            for field in field_list
        }

    def get_gb_info_by_date(self, stock_code, start_date, end_date):
        # 返回两天每日股本。
        self.capital_requests.append((start_date, end_date))
        return [
            {"Date": "20260827", "Zgb": "10000", "Ltgb": "8000"},
            {"Date": "20260828", "Zgb": "10000", "Ltgb": "8000"},
        ]

    def get_divid_factors(self, stock_code, start_time, end_time):
        # 返回一条使用实际字段名 AllotPrice 的分红记录。
        self.action_requests.append((start_time, end_time))
        return pd.DataFrame(
            {
                "Type": [1],
                "Bonus": [2.0],
                "AllotPrice": [0.0],
                "ShareBonus": [0.0],
                "Allotment": [0.0],
            },
            index=["2026-06-01"],
        )

    def get_gpjy_value(
        self,
        stock_list,
        field_list,
        start_time,
        end_time,
    ):
        # 按请求字段返回两天 GP 嵌套记录。
        self.gp_requests.append((start_time, end_time))
        stock_data = {}
        for field in field_list:
            number = int(field[2:])
            stock_data[field] = [
                {"Date": "20260827", "Value": [str(number), str(number + 1)]},
                {"Date": "20260828", "Value": [str(number + 2), str(number + 3)]},
            ]
        return {stock_list[0]: stock_data}

    def get_financial_data(
        self,
        stock_list,
        field_list,
        start_time,
        end_time,
        report_type,
    ):
        # 按请求字段返回同一份专业财务报告。
        self.financial_requests.append((start_time, end_time))
        self.financial_field_requests.append(tuple(field_list))
        values = {field: [float(int(field[2:]))] for field in field_list}
        values["announce_time"] = ["20260815"]
        values["tag_time"] = ["20260630"]
        return {stock_list[0]: pd.DataFrame(values)}


def _client() -> TdxClient:
    # 创建注入假接口的通达信客户端。
    client = TdxClient(script_file=__file__, api=FakeStockDataApi())
    client.initialize()
    return client


def _seed_master(db_path: Path) -> None:
    # 创建证券池基础表并写入一只测试股票。
    connection = pool_db.connect(db_path)
    try:
        pool_db.init_db(connection)
        connection.execute(
            "INSERT INTO stock_master("
            "stock_code,exchange,list_date,first_seen_date,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?)",
            (
                "600001.SH",
                "SH",
                "2026-01-01",
                "2026-01-01",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_processors_handle_real_return_shapes() -> None:
    # 验证行情字段字典和分红字段别名能够正确转换。
    api = FakeStockDataApi()
    bars = flatten_daily_bars(
        api.get_market_data(
            ["Open", "High", "Low", "Close", "Volume", "Amount"],
            ["600001.SH"],
            "1d",
            "20260826",
            "20260828",
            -1,
            "none",
            False,
        )
    )
    actions = build_action_rows(
        "600001.SH",
        api.get_divid_factors("600001.SH", "20260101", "20260828"),
    )
    assert len(bars) == 3
    assert round(bars[1]["pct_change"], 6) == round((10.8 / 10.5 - 1) * 100, 6)
    assert actions[0]["allot_price"] == 0.0


def test_trade_metrics_split_years_and_fields() -> None:
    # 验证 GP 历史数据同时按自然年和字段批次拆分。
    api = FakeStockDataApi()
    client = TdxClient(script_file=__file__, api=api)
    client.initialize()
    results = fetch_trade_metrics(
        client,
        ["600001.SH"],
        "2024-06-01",
        "2026-08-28",
        fields=GP_FIELDS[:14],
        field_batch_size=7,
        split_by_year=True,
    )
    assert yearly_date_ranges("2024-06-01", "2026-08-28") == [
        ("2024-06-01", "2024-12-31"),
        ("2025-01-01", "2025-12-31"),
        ("2026-01-01", "2026-08-28"),
    ]
    assert len(results) == 6
    assert api.gp_requests == [
        ("20240601", "20241231"),
        ("20240601", "20241231"),
        ("20250101", "20251231"),
        ("20250101", "20251231"),
        ("20260101", "20260828"),
        ("20260101", "20260828"),
    ]


def test_full_load_all_required_domains(tmp_path: Path) -> None:
    # 验证第一批和第二批数据可完整写入且重复执行幂等。
    db_path = tmp_path / "security_pool.db"
    _seed_master(db_path)
    first = full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
        financial_fields=FN_FIELDS,
    )
    second = full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
        financial_fields=FN_FIELDS,
    )
    bulk = bulk_full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
        financial_fields=FN_FIELDS,
    )
    assert first["status"] == "SUCCESS"
    assert second["status"] == "SUCCESS"
    assert bulk["status"] == "SUCCESS"
    assert second["table_counts"] == {
        "stock_daily_bar": 3,
        "stock_capital_daily": 2,
        "stock_corporate_action": 1,
        "stock_trade_daily": 2,
        "stock_financial_report": 1,
    }
    assert len(GP_FIELDS) == 52
    assert len(FN_FIELDS) == 438

    with sqlite3.connect(db_path) as connection:
        trade_json = connection.execute(
            "SELECT raw_metrics_json FROM stock_trade_daily LIMIT 1"
        ).fetchone()[0]
        finance_json = connection.execute(
            "SELECT raw_values_json FROM stock_financial_report LIMIT 1"
        ).fetchone()[0]
    assert len(json.loads(trade_json)) == 52
    assert len(json.loads(finance_json)) == 438
    checks = check_data(db_path, ["600001.SH"])["checks"]
    assert checks == {
        "integrity_check": "ok",
        "foreign_key_errors": 0,
        "bad_bars": 0,
        "bad_capital": 0,
        "invalid_json": 0,
    }


def test_daily_check_only_checks_target_date(tmp_path: Path) -> None:
    # 验证每日检查使用日期范围和同步状态，不执行全库完整性检查。
    db_path = tmp_path / "security_pool.db"
    _seed_master(db_path)
    full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO stock_current("
            "stock_code,data_date,stock_name,is_active,is_all_a,updated_at"
            ") VALUES (?,?,?,?,?,?)",
            (
                "600001.SH",
                "2026-08-28",
                "测试股票",
                1,
                1,
                "2026-08-28T16:00:00",
            ),
        )
        connection.commit()

    result = check_data(
        db_path,
        check_profile="daily",
        data_date="2026-08-28",
    )

    assert result["check_profile"] == "daily"
    assert result["table_counts"]["stock_daily_bar"] == 1
    assert result["checks"]["expected_stocks"] == 1
    assert result["checks"]["incomplete_sync_rows"] == 0
    assert "integrity_check" not in result["checks"]
    assert "foreign_key_errors" not in result["checks"]


def test_core_financial_profile_uses_one_request_and_preserves_full_json(
    tmp_path: Path,
) -> None:
    # 验证核心字段不超过单次上限，且不会覆盖库中已有的完整财务 JSON。
    db_path = tmp_path / "security_pool.db"
    _seed_master(db_path)
    full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
        financial_fields=FN_FIELDS,
    )
    api = FakeStockDataApi()
    client = TdxClient(script_file=__file__, api=api)
    client.initialize()
    result = bulk_full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        start_date="2004-01-01",
        domains=("financial",),
        financial_fields=CORE_FN_FIELDS,
        client=client,
    )

    assert len(CORE_FN_FIELDS) == 92
    assert result["status"] == "SUCCESS"
    assert result["financial_field_count"] == 92
    assert api.financial_field_requests == [CORE_FN_FIELDS]
    with sqlite3.connect(db_path) as connection:
        finance_json = connection.execute(
            "SELECT raw_values_json FROM stock_financial_report LIMIT 1"
        ).fetchone()[0]
        task_type = connection.execute(
            "SELECT task_type FROM fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert len(json.loads(finance_json)) == 438
    assert task_type == "STOCK_DATA_BULK_FINANCIAL_CORE"


def test_bulk_stage1_excludes_financial_data(tmp_path: Path) -> None:
    # 验证第一阶段支持起始日期和数据域，并且不会写入财务报告。
    db_path = tmp_path / "security_pool.db"
    _seed_master(db_path)
    result = bulk_full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        start_date="2004-01-01",
        domains=("bar", "capital", "action", "trade"),
        client=_client(),
    )
    assert result["status"] == "SUCCESS"
    assert result["start_date"] == "2004-01-01"
    assert result["domains"] == ("bar", "capital", "action", "trade")
    assert result["table_counts"]["stock_trade_daily"] == 2
    assert result["table_counts"]["stock_financial_report"] == 0

    with sqlite3.connect(db_path) as connection:
        task_type = connection.execute(
            "SELECT task_type FROM fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert task_type == "STOCK_DATA_BULK_STAGE1"


def test_daily_update_starts_from_last_successful_fetch_date(
    tmp_path: Path,
) -> None:
    # 验证每日任务按各数据域上次成功截止日回取，不再使用固定窗口。
    db_path = tmp_path / "security_pool.db"
    _seed_master(db_path)
    full_load(
        "2026-08-28",
        db_path,
        ["600001.SH"],
        client=_client(),
    )

    api = FakeStockDataApi()
    client = TdxClient(script_file=__file__, api=api)
    client.initialize()
    result = daily_update(
        "2026-09-02",
        db_path,
        ["600001.SH"],
        client=client,
    )

    assert result["status"] == "SUCCESS"
    assert api.bar_requests == [("20260828", "20260902")]
    assert api.capital_requests == [("20260828", "20260902")]
    assert api.action_requests == [("20260828", "20260902")]
    assert set(api.gp_requests) == {("20260828", "20260902")}
    assert set(api.financial_requests) == {("20260828", "20260902")}
    with sqlite3.connect(db_path) as connection:
        states = dict(connection.execute(
            "SELECT data_domain,last_success_date "
            "FROM stock_data_sync_state WHERE stock_code='600001.SH'"
        ))
    assert states == {
        "action": "2026-09-02",
        "bar": "2026-09-02",
        "capital": "2026-09-02",
        "financial": "2026-09-02",
        "trade": "2026-09-02",
    }
