"""Tests for two-stage incremental financial synchronization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from invest.storage import security_pool as pool_db
from invest.storage import stock as db
from invest.providers.stock import CORE_FN_FIELDS
from invest.market.stock.financial_delta import PROBE_FIELDS
from invest.market.stock.financial_delta import sync_financial_delta


class FakeFinancialClient:
    def __init__(self) -> None:
        # 初始化模拟客户端的调用记录。
        self.requests: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def get_financial_data(
        self,
        stock_list,
        field_list,
        start_time,
        end_time,
        report_type,
    ):
        # 记录字段请求，仅给指定测试股票返回财报。
        self.requests.append((tuple(stock_list), tuple(field_list)))
        if "600001.SH" not in stock_list:
            return {}
        values = {field: [1.0] for field in field_list}
        values["announce_time"] = ["20260905"]
        values["tag_time"] = ["20260630"]
        return {"600001.SH": pd.DataFrame(values)}


def _seed(db_path: Path) -> None:
    # 创建独立的双股票测试数据库。
    connection = pool_db.connect(db_path)
    try:
        db.init_db(connection)
        for code in ("600001.SH", "600002.SH"):
            connection.execute(
                "INSERT INTO stock_master("
                "stock_code,exchange,first_seen_date,created_at,updated_at"
                ") VALUES (?,?,?,?,?)",
                (code, "SH", "2026-01-01", "2026-01-01", "2026-01-01"),
            )
            connection.execute(
                "INSERT INTO stock_current("
                "stock_code,data_date,stock_name,is_active,is_all_a,updated_at"
                ") VALUES (?,?,?,?,?,?)",
                (code, "2026-09-10", code, 1, 1, "2026-09-10"),
            )
        connection.commit()
    finally:
        connection.close()


def test_financial_delta_probes_then_fetches_only_hit_stocks(tmp_path: Path) -> None:
    # 验证仅对探测命中的股票获取详细财报。
    db_path = tmp_path / "security_pool.db"
    _seed(db_path)
    client = FakeFinancialClient()

    result = sync_financial_delta(
        "2026-09-02",
        "2026-09-10",
        db_path,
        probe_batch_size=100,
        detail_batch_size=5,
        client=client,
    )

    assert result["status"] == "SUCCESS"
    assert result["scanned_stocks"] == 2
    assert result["report_stock_count"] == 1
    assert result["written_rows"] == 1
    assert client.requests[0][1] == PROBE_FIELDS
    assert client.requests[1] == (("600001.SH",), CORE_FN_FIELDS)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM stock_financial_report"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM stock_data_sync_state "
            "WHERE data_domain='financial' AND last_success_date='2026-09-10'"
        ).fetchone()[0] == 2
