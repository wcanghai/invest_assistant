"""证券池全量和每日更新的集成测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from security_pool.main import daily_update, full_load
from security_pool.processor import parse_date, parse_flag, parse_float
from tdx_client import TdxClient


class FakeTdxApi:
    """为集成测试提供可切换交易日的通达信假接口。"""

    def __init__(self) -> None:
        # 初始化第一天证券列表和接口测试数据。
        self.day = 1
        self.initialized = False
        self.empty_all_a = False

    def initialize(self, script_file: str):
        # 模拟通达信初始化接口。
        self.initialized = bool(script_file)
        return 1

    def _all_a(self) -> list[dict[str, str]]:
        # 返回当前测试日的全部 A 股列表。
        if self.empty_all_a:
            return []
        if self.day == 1:
            return [
                {"Code": "600001.SH", "Name": "上证测试"},
                {"Code": "000001.SZ", "Name": "深证测试"},
                {"Code": "300001.SZ", "Name": "创业测试"},
            ]
        return [
            {"Code": "600001.SH", "Name": "上证测试"},
            {"Code": "300001.SZ", "Name": "创业测试"},
            {"Code": "688001.SH", "Name": "科创测试"},
        ]

    def get_stock_list(self, market=None, list_type: int = 0):
        # 按测试分类返回证券代码或代码名称列表。
        categories = {
            "5": self._all_a(),
            "7": [{"Code": "600001.SH", "Name": "上证测试"}],
            "8": [{"Code": "000001.SZ", "Name": "深证测试"}],
            "23": [{"Code": "600001.SH", "Name": "上证测试"}],
            "24": [],
            "25": [{"Code": "300001.SZ", "Name": "创业测试"}],
            "28": [],
            "50": self._all_a(),
            "51": [{"Code": "300001.SZ", "Name": "创业测试"}],
            "52": (
                [{"Code": "688001.SH", "Name": "科创测试"}]
                if self.day == 2
                else []
            ),
            "53": [],
            "56": [{"Code": "600001.SH", "Name": "上证测试"}],
            "57": [{"Code": "600001.SH", "Name": "上证测试"}],
            "32": [{"Code": "123001.SZ", "Name": "测试转债"}],
            "91": [{"Code": "000300.SH", "Name": "沪深300"}],
        }
        records = categories.get(str(market), [])
        if list_type == 1:
            return records
        return [record["Code"] for record in records]

    def get_stock_info(self, stock_code: str, field_list=None):
        # 返回测试股票的基础信息。
        names = {
            "600001.SH": "上证测试",
            "000001.SZ": "深证测试",
            "300001.SZ": "创业测试",
            "688001.SH": "科创测试",
        }
        kinds = {"600001.SH": "1", "000001.SZ": "1", "300001.SZ": "3"}
        return {
            "Name": names[stock_code],
            "Unit": "100",
            "MinPrice": "0.01",
            "XsFlag": "2",
            "HSStockKind": kinds.get(stock_code, "4"),
            "J_start": "20200101",
            "IsSTGP": "0",
            "IsQuitGP": "0",
            "BelongHasKQZ": "1" if stock_code == "300001.SZ" else "0",
            "ActiveCapital": "8000",
            "J_zgb": "10000",
            "tdx_dycode": "01",
            "tdx_dyname": "测试地区",
            "rs_hycode_sim": "H01",
            "rs_hyname": "测试行业",
            "ErrorId": "0",
        }

    def get_more_info(self, stock_code: str, field_list=None):
        # 返回测试股票的每日动态信息。
        return {
            "HqDate": "20260830",
            "TPFlag": "0",
            "Zsz": "100.5",
            "Ltsz": "80.5",
            "fHSL": "1.2",
            "CJJEPre1": "5000",
            "FreeLtgb": "7000",
            "StaticPE_TTM": "15.2",
            "PB_MRQ": "1.8",
            "DYRatio": "2.1",
            "ErrorId": "0",
        }

    def get_relation(self, stock_code: str = ""):
        # 返回测试股票所属行业和概念板块。
        return [
            {
                "BlockCode": "881001.SH",
                "BlockName": "测试行业",
                "BlockType": "行业",
                "GPNume": "3",
            },
            {
                "BlockCode": "880001.SH",
                "BlockName": "测试概念",
                "BlockType": "概念",
                "GPNume": "3",
            },
        ]

    def get_ipo_info(self, ipo_type: int = 0, ipo_date: int = 0):
        # 返回一条新股或新债申购测试记录。
        if ipo_type == 0:
            return [{
                "Code": "001001.SZ",
                "Name": "测试新股",
                "SGDate": "20260901",
                "SGPrice": "10.00",
                "SGCode": "001001",
                "MaxSG": "1.00",
                "PE_Issue": "20.00",
            }]
        return [{
            "Code": "123999.SZ",
            "Name": "测试新债",
            "SGDate": "20260902",
            "SGPrice": "100.00",
            "SGCode": "123999",
            "MaxSG": "10.00",
            "PE_Issue": "0.00",
        }]

    def get_kzz_info(self, stock_code: str = "", field_list=None):
        # 返回可转债测试数据。
        return {
            "KZZCode": "123001",
            "HSCode": "300001",
            "ZGPrice": "20.0",
            "RestScope": "50000",
            "EndDate": "20300101",
            "KZZPrice": "120.0",
            "AGPrice": "25.0",
            "KZZYj": "5.0",
            "ZGValue": "125.0",
            "ErrorId": "0",
        }

    def get_trackzs_etf_info(self, zs_code: str = ""):
        # 返回指数对应 ETF 测试数据。
        return [{
            "Code": "510300.SH",
            "Name": "沪深300ETF",
            "NowPrice": "4.0",
            "PreClose": "3.9",
            "IOPV": "4.01",
            "Zgb": "100000",
            "Sz": "400",
        }]


def _client(api: FakeTdxApi) -> TdxClient:
    # 创建注入假 API 的已初始化客户端。
    client = TdxClient(script_file=__file__, api=api)
    client.initialize()
    return client


def _scalar(db_path: Path, sql: str) -> int:
    # 执行 SQLite 标量查询并返回整数结果。
    with sqlite3.connect(db_path) as connection:
        return int(connection.execute(sql).fetchone()[0])


def test_normalizers() -> None:
    # 验证常用日期、数值和标识转换。
    assert parse_date("20260830") == "2026-08-30"
    assert parse_date("0") is None
    assert parse_float("1,234.50") == 1234.5
    assert parse_float("") is None
    assert parse_flag("1") == 1
    assert parse_flag("9") is None


def test_full_load_and_daily_update(tmp_path: Path) -> None:
    # 验证首次全量、次日新增退出和每日全历史股票快照。
    db_path = tmp_path / "security_pool.db"
    api = FakeTdxApi()
    client = _client(api)

    first_counts = full_load("2026-08-30", db_path, client=client)
    assert first_counts["stock_master"] == 3
    assert first_counts["stock_current"] == 3
    assert first_counts["stock_snapshot"] == 3
    assert first_counts["stock_sector_snapshot"] == 6
    assert first_counts["ipo_info"] == 2
    assert first_counts["convertible_bond_info"] == 1
    assert first_counts["etf_info"] == 1

    api.day = 2
    second_counts = daily_update("2026-08-31", db_path, client=client)
    assert second_counts["stock_master"] == 4
    assert second_counts["stock_current"] == 4
    assert second_counts["stock_snapshot"] == 7
    assert _scalar(
        db_path,
        "SELECT is_active FROM stock_current WHERE stock_code='000001.SZ'",
    ) == 0
    assert _scalar(
        db_path,
        "SELECT COUNT(*) FROM stock_snapshot WHERE snapshot_date='2026-08-31'",
    ) == 4


def test_empty_core_classification_does_not_replace_data(tmp_path: Path) -> None:
    # 验证核心分类为空时任务失败且旧证券池保持不变。
    db_path = tmp_path / "security_pool.db"
    api = FakeTdxApi()
    client = _client(api)
    full_load("2026-08-30", db_path, client=client)
    api.empty_all_a = True

    with pytest.raises(RuntimeError, match="所有 A 股分类返回为空"):
        daily_update("2026-08-31", db_path, client=client)

    assert _scalar(db_path, "SELECT COUNT(*) FROM stock_current") == 3
    assert _scalar(db_path, "SELECT COUNT(*) FROM stock_snapshot") == 3
    assert _scalar(
        db_path,
        "SELECT COUNT(*) FROM fetch_log WHERE status='FAILED'",
    ) == 1
