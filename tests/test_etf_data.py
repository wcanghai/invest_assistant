"""ETF 扩展数据采集测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from security_pool.etf_data import load_etf_data


class FakeEtfClient:
    def __init__(self) -> None:
        self.market_starts: list[str] = []

    def get_stock_list(self, market=None, list_type: int = 0):
        rows = {
            "31": [
                {"Code": "510300.SH", "Name": "沪深300ETF"},
                {"Code": "513100.SH", "Name": "纳指ETF"},
            ],
            "36": [{"Code": "513100.SH", "Name": "纳指ETF"}],
        }.get(str(market), [])
        return rows if list_type == 1 else [row["Code"] for row in rows]

    def get_stock_info(self, stock_code: str, field_list=None):
        return {
            "ErrorId": "0",
            "Name": "沪深300ETF" if stock_code == "510300.SH" else "纳指ETF",
            "Unit": "100",
            "XsFlag": "3",
            "J_start": "20120101",
            "BelongRZRQ": "1",
            "ActiveCapital": "10000",
            "underly_setcode": "1" if stock_code == "510300.SH" else "12",
            "underly_code": "000300" if stock_code == "510300.SH" else "A_NDX",
        }

    def get_market_data(self, fields, codes, period, start, end, count, fq, fill):
        self.market_starts.append(start)
        index = pd.to_datetime(["2026-09-03", "2026-09-04"])
        values = {
            "Open": [1.0, 1.1], "High": [1.2, 1.3], "Low": [0.9, 1.0],
            "Close": [1.1, 1.2], "Volume": [100.0, 200.0],
            "Amount": [10.0, 20.0], "ForwardFactor": [1.0, 1.0],
        }
        return {
            field: pd.DataFrame({code: values[field] for code in codes}, index=index)
            for field in fields
        }

    def get_gpjy_value(self, codes, fields, start, end):
        return {
            code: {
                "GP47": [{"Date": "20260904", "Value": ["12.5"]}],
                "GP51": [{"Date": "20260904", "Value": ["1.2", "1.5"]}],
                "GP52": [{"Date": "20260904", "Value": ["10000", "12000"]}],
            }
            for code in codes
        }

    def get_scjy_value(self, fields, start, end):
        return {
            "SC08": [{"Date": "20260904", "Value": ["3000", "20"]}],
            "SC38": [{"Date": "20260904", "Value": ["5000", "30"]}],
        }


def test_load_etf_data(tmp_path: Path) -> None:
    db_path = tmp_path / "etf.db"
    client = FakeEtfClient()
    result = load_etf_data(
        "2026-09-04",
        "2026-09-03",
        db_path,
        batch_size=2,
        incremental=False,
        client=client,
    )
    assert result["status"] == "SUCCESS"
    assert result["etf_count"] == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM etf_master").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0] == 4
        assert connection.execute(
            "SELECT is_t0 FROM etf_master WHERE etf_code='513100.SH'"
        ).fetchone()[0] == 1
        row = connection.execute(
            "SELECT unit_nav,cumulative_nav,shares_10k,size_10k,net_subscription_10k "
            "FROM etf_daily WHERE etf_code='510300.SH' AND trade_date='2026-09-04'"
        ).fetchone()
        assert row == (1.2, 1.5, 10000.0, 12000.0, 12.5)
        assert connection.execute("SELECT COUNT(*) FROM etf_market_daily").fetchone()[0] == 1

    second = load_etf_data(
        "2026-09-05",
        "2026-09-05",
        db_path,
        batch_size=2,
        refresh_master=False,
        incremental=True,
        client=client,
    )
    assert second["status"] == "SUCCESS"
    assert client.market_starts[-1] == "20260904"
