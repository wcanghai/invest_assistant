"""日报关键数据边界、版本持久化和本地 HTTP 行为的离线验证。"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

import pytest

from invest.reporting.local import percentile
from invest.reporting.local import summarize
from invest.providers.web import parse_crypto
from invest.providers.web import parse_future
from invest.providers.web import parse_index
from invest.providers.web import parse_us
from invest.providers.web import collection_lock
from invest.providers.web import parse_sina_index
from invest.reporting.common import CATEGORIES
from invest.reporting.common import ROOT
from invest.reporting.service import generate
from invest.storage.reports import connect
from invest.storage.reports import get_report
from invest.storage.reports import latest_observations
from invest.storage.reports import list_reports
from invest.storage.reports import save_observation
from invest.storage.reports import save_report
from invest.reporting.web import ReportServer


def test_index_uses_precision_and_filters_other_symbols():
    # 防止把顶栏指数、百分比缩放或其他交易所报价用于目标标的。
    data = {"data": {"f57": "000001", "f43": 388811, "f59": 2, "f152": 2,
                     "f170": -118, "f86": 1789114292}}
    text = "callback(" + json.dumps(data) + ");"
    url = "https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fltt=1"
    row = parse_index(text, "000001.SH", url)
    assert row["close"] == 3888.11
    assert row["pct_change"] == -1.18
    assert row["source_date"] == "2026-09-11"
    assert parse_index(text, "000001.SZ", url) is None
    assert parse_index(text, "000300.SH", url) is None
    data["data"].pop("f86")
    assert parse_index(json.dumps(data), "000001.SH", url) is None


def test_future_uses_previous_settlement_and_exact_contract():
    # 期货对昨结算计算涨跌，不能误取买价、热门合约或成交量字段。
    text = ('var hq_str_nf_AU2610="黄金2610,023000,950.360,952.880,940.460,0.000,'
            '941.840,941.920,941.880,0.000,942.220,2,1,137511,179708,沪,黄金,2026-09-12";')
    row = parse_future(text, "AU2610.SHF")
    assert row["close"] == 941.88
    assert row["pct_change"] == pytest.approx((941.88 / 942.22 - 1) * 100)
    assert row["volume"] == 179708
    assert row["quote_time"] == "2026-09-12T02:30:00+08:00"
    assert parse_future(text, "AU2612.SHF") is None


def test_us_uses_regular_session_date_not_aftermarket_or_refresh():
    # 美股时间取常规时段字段，避免北京时间跨日与盘后串价。
    values = ["苹果", "332.2700", "1.75", "2026-09-12 09:30:09", "5.7"] + ["0"] * 25
    values[21], values[24] = "332.55", "Sep 11 07:59PM EDT"
    values[25], values[29] = "Sep 11 04:00PM EDT", "2026"
    row = parse_us('var hq_str_gb_aapl="' + ",".join(values) + '";', "AAPL")
    assert row["close"] == 332.27
    assert row["source_date"] == "2026-09-11"
    assert row["quote_time"] == "2026-09-11T16:00:00-04:00"


def test_crypto_identity_timestamp_and_signed_change():
    # 币价只取目标资产的美元统计，来源 UTC 按上海日期归档。
    detail = {"slug": "bitcoin", "latestUpdateTime": "2026-09-11T20:00:00Z",
              "statistics": {"price": 77000, "priceChangePercentage24h": -1.25}}
    data = json.dumps({"props": {"pageProps": {"detailRes": {"detail": detail}}}})
    row = parse_crypto(data, "XBTUSD")
    assert row["pct_change"] == -1.25
    assert row["source_date"] == "2026-09-12"
    with pytest.raises(ValueError):
        parse_crypto(data, "ETHUSD")


def test_percentile_bounds_and_adjustment():
    # 后续行情不改变历史分位，缺失复权因子不使用默认因子。
    start = date(2023, 9, 11)
    rows = [{"trade_date": (start + timedelta(days=i)).isoformat(),
             "close": i + 1, "forward_factor": 1} for i in range(1097)]
    target = rows[-1]["trade_date"]
    assert percentile(rows, target, True) == 100
    rows.append({"trade_date": "2027-01-01", "close": 1, "forward_factor": 1})
    assert percentile(rows, target, True) == 100
    rows[-2]["forward_factor"] = None
    assert percentile(rows, target, True) is None
    assert percentile(rows[:30], target) is None


def test_breadth_excludes_suspensions_unlisted_and_reports_missing():
    # 缺行情、缺涨跌和缺成交额分别反映，停牌不能当平盘。
    rows = [
        {"stock_code": "A", "close": 10, "pct_change": 1, "amount_10k": 10000},
        {"stock_code": "B", "close": 5, "pct_change": 0, "is_suspended": 1},
        {"stock_code": "C", "list_date": "2026-09-15"},
        {"stock_code": "D", "close": None},
        {"stock_code": "E", "close": 10, "pre_close": 20},
    ]
    summary, industries, missing = summarize(rows, "2026-09-11")
    assert summary["expected"] == 3
    assert summary["covered"] == 2
    assert summary["suspended"] == summary["unlisted"] == 1
    assert summary["flat"] == 0
    assert summary["amount_missing"] == 1
    assert summary["amount_100m"] == 1
    assert missing == ["D"]
    assert industries[0]["pct_change"] == -24.5


def test_versions_idempotence_and_prefer_complete(tmp_path):
    # 重复输入幂等；同日部分数据不会覆盖完整版本。
    conn = connect(tmp_path / "reports.db")
    payload = {"report_date": "2026-09-11", "generated_at": "a", "status": "complete"}
    first = save_report(conn, payload, "first")
    payload["generated_at"] = "b"
    assert save_report(conn, payload, "second") == first
    payload["status"] = "partial"
    second = save_report(conn, payload, "partial")
    assert second != first
    assert get_report(conn)["id"] == first
    assert get_report(conn, second)["version"] == 2
    conn.close()


def test_concurrent_generation_unique(tmp_path):
    # 多连接竞争相同报告时只持久化一个版本。
    path = tmp_path / "reports.db"
    connect(path).close()

    def write(index):
        # 各线程独立连接结果库，模拟 CLI 与网页同时生成。
        conn = connect(path)
        try:
            return save_report(conn, {"report_date": "2026-09-11", "generated_at": str(index),
                                      "status": "complete"}, "content")
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(write, range(8)))
    assert len(set(ids)) == 1


def test_observation_cutoff_failure_fallback(tmp_path):
    # 历史补报不可读取未来采集，失败时只能标注旧值而不是伪装成功。
    conn = connect(tmp_path / "reports.db")
    base = {"category": "us_stocks", "code": "AAPL", "source_date": "2026-09-10"}
    save_observation(conn, {**base, "observed_at": "2026-09-11T08:00:00+08:00",
                            "status": "ok", "close": 100})
    save_observation(conn, {**base, "observed_at": "2026-09-12T08:00:00+08:00",
                            "status": "error", "error": "timeout"})
    config = {key: {} for key in ("a_share_indices", "commodity_futures", "crypto_pairs")}
    config["us_stocks"] = {"AAPL": "苹果"}
    row = latest_observations(conn, config, "2026-09-11T23:59:59+08:00")["us_stocks"][0]
    assert row["status"] == "ok"
    row = latest_observations(conn, config, "2026-09-12T23:59:59+08:00")["us_stocks"][0]
    assert row["status"] == "stale" and row["close"] == 100
    assert row["error"] == "timeout"
    conn.close()


def test_http_routes_and_same_origin_protection(tmp_path):
    # 网站空状态、非法版本、路径穿越和跨站刷新不会触发采集或泄露文件。
    server = ReportServer(("127.0.0.1", 0), report_db=tmp_path / "reports.db")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base) as response:
            assert response.status == 200
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        with urlopen(base + "/api/reports") as response:
            assert json.load(response) == []
        with urlopen(base + "/recommendations") as response:
            assert "股票推荐研究" in response.read().decode("utf-8")
        with urlopen(base + "/api/recommendations/job") as response:
            assert json.load(response)["running"] is False
        with urlopen(base + "/etf-recommendations") as response:
            assert "ETF 推荐研究" in response.read().decode("utf-8")
        with urlopen(base + "/api/etf-recommendations/job") as response:
            assert json.load(response)["running"] is False
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/recommendations")
        assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/etf-recommendations")
        assert error.value.code == 404
        for path, expected in (("/api/report", 404), ("/api/report?id=no", 400),
                               ("/../common.py", 404)):
            with pytest.raises(HTTPError) as error:
                urlopen(base + path)
            assert error.value.code == expected
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/refresh", method="POST"))
        assert error.value.code == 403
        server.refresh_lock.acquire()
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + "/api/refresh", method="POST", headers={"Origin": base}))
        assert error.value.code == 409
        server.refresh_lock.release()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_optimized_static_pages_and_shared_ui_are_served(tmp_path):
    # 五个页面加载共享导航，并公开新增的纯前端交互资源。
    server = ReportServer(("127.0.0.1", 0), report_db=tmp_path / "reports.db")
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for path in ("/", "/recommendations", "/etf-recommendations",
                     "/rankings", "/securities"):
            with urlopen(base + path) as response:
                html = response.read().decode("utf-8")
            assert '/ui.js' in html
            assert '/nav-layout.css' in html
            assert 'id="site-navigation"' in html
            assert 'name="description"' in html
        with urlopen(base + "/ui.js") as response:
            shared = response.read().decode("utf-8")
        assert "市场日报" in shared
        assert "request(url" in shared
        with urlopen(base + "/securities-extra.css") as response:
            assert ".recent-item" in response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_collect_lock_excludes_second_process_handle(tmp_path):
    # 同一结果库的两次采集不能同时持锁，退出后可重新获取。
    path = tmp_path / "reports.db"
    with collection_lock(path):
        with pytest.raises(RuntimeError):
            with collection_lock(path):
                pass
    with collection_lock(path):
        pass


def test_watchlist_migrates_and_supports_idempotent_changes(tmp_path):
    # 首次合并旧名单，之后所有项目都可加入或删除且能够重启恢复。
    from invest.reporting.watchlist import add
    from invest.reporting.watchlist import load
    from invest.reporting.watchlist import remove

    config = tmp_path / "config.json"
    tracking = tmp_path / "tracking.json"
    watchlist = tmp_path / "watchlist.json"
    config.write_text(json.dumps({
        "a_share_stocks": {"000001.SZ": "平安银行"},
        "industry_etfs": {"510300.SH": "沪深300ETF"},
    }, ensure_ascii=False), encoding="utf-8")
    tracking.write_text(json.dumps({
        "a_share_stocks": {"600000.SH": "浦发银行"},
    }, ensure_ascii=False), encoding="utf-8")
    initial = load(watchlist, config, tracking)
    assert list(initial["a_share_stocks"]) == ["000001.SZ", "600000.SH"]
    add("a_share_stocks", "600000.SH", "浦发银行", watchlist)
    add("industry_etfs", "512880.SH", "证券ETF", watchlist)
    remove("a_share_stocks", "000001.SZ", watchlist)
    remove("a_share_stocks", "000001.SZ", watchlist)
    restored = load(watchlist, config, tracking)
    assert restored["a_share_stocks"] == {"600000.SH": "浦发银行"}
    assert restored["industry_etfs"]["512880.SH"] == "证券ETF"


def test_sina_index_full_quote_and_null_reference():
    # 新浪指数取完整长串而非不含日期的简版顶栏报价。
    values = ["上证指数", "3910.92", "3934.40", "3888.11"] + ["0"] * 28
    values[30], values[31] = "2026-09-11", "15:43:32"
    text = 'var hq_str_sh000001="' + ",".join(values) + '";'
    row = parse_sina_index(text, "000001.SH")
    assert row["close"] == 3888.11
    assert row["source_date"] == "2026-09-11"
    assert parse_sina_index(text, "399001.SZ") is None


def test_generate_weekend_with_readonly_main_db_and_no_future_data(tmp_path):
    # 周末日报使用最近交易日，历史补报不读未来日线或当天之后网页记录。
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    schemas = ROOT / "src/invest/storage"
    for path in (schemas / "security_pool.sql", schemas / "stock.sql"):
        conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO stock_master(stock_code,exchange,first_seen_date,created_at,updated_at) "
        "VALUES ('000001.SZ','SZ','2026-09-10','2026-09-10','2026-09-10')"
    )
    for day, price in (("2026-09-10", 10), ("2026-09-11", 11), ("2026-09-14", 15)):
        conn.execute(
            "INSERT INTO stock_snapshot(snapshot_date,stock_code,stock_name,is_active,"
            "is_all_a,archived_at) VALUES (?,'000001.SZ','示例',1,1,?)", (day, day)
        )
        conn.execute(
            "INSERT INTO stock_daily_bar(stock_code,trade_date,close,pct_change,amount_10k,"
            "forward_factor,updated_at) VALUES ('000001.SZ',?,?,1,10000,1,?)", (day, price, day)
        )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM stock_daily_bar").fetchone()[0]
    config = {key: {} for key in CATEGORIES}
    config["a_share_stocks"] = {"000001.SZ": "示例"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    result = generate("2026-09-12", source, tmp_path / "reports.db", path)
    assert result["data"]["a_share_date"] == "2026-09-11"
    assert result["data"]["sections"]["a_share_stocks"][0]["close"] == 11
    assert result["status"] == "complete"
    historical = generate("2026-09-10", source, tmp_path / "reports.db", path)
    assert historical["data"]["sections"]["a_share_stocks"][0]["close"] == 10
    assert conn.execute("SELECT COUNT(*) FROM stock_daily_bar").fetchone()[0] == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='reports'").fetchone() is None
    conn.close()
