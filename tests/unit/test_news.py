"""资讯聚合适配器与本地 HTTP 接口的离线验证。"""

import json
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request
from urllib.request import urlopen

import pytest

from invest.reporting.news import NewsError
from invest.reporting.news import due_date
from invest.reporting.news import list_dates
from invest.reporting.news import load_report
from invest.reporting.news import load_wechat_articles
from invest.reporting.news import run_refresh
from invest.reporting.web import ReportServer


def sample_report(date="2026-09-21", state="partial"):
    # 构造与 web_read 产物一致的最小结构化报告。
    item = {
        "title": "示例资讯",
        "summary": "示例摘要",
        "why": "示例重要性",
        "category": "产业商业",
        "status": "已发布",
        "sourceName": "示例来源",
        "url": "https://example.com/news",
        "time": {"iso": "2026-09-21T10:00:00Z"},
    }
    return {
        "date": date,
        "state": state,
        "generatedAt": "2026-09-21T14:00:00Z",
        "modelIncomplete": state == "partial",
        "window": {"start": 1789912800000, "end": 1789999200000},
        "overview": ["示例概览"],
        "selected": [item],
        "extras": [{**item, "title": "补充阅读"}],
        "sources": [{"name": "示例来源", "state": "ok", "collected": 1}],
        "errors": [],
        "metrics": {"articles": 1},
    }


def write_report(root, date="2026-09-21", state="partial"):
    # 在临时 web_read 目录写入一份报告。
    folder = root / "reports" / "ai-news" / date
    folder.mkdir(parents=True)
    (folder / "report.json").write_text(
        json.dumps(sample_report(date, state), ensure_ascii=False),
        encoding="utf-8",
    )


def fake_settings(root):
    # 提供只含外部集成路径的测试配置。
    return SimpleNamespace(
        config={"integrations": {"web_read_root": str(root)}},
        path=lambda value: Path(value).resolve(),
    )


def test_news_reports_list_latest_and_validate_dates(tmp_path):
    # 日期列表倒序、默认最新和部分完成报告均可读取。
    write_report(tmp_path, "2026-09-20", "complete")
    write_report(tmp_path, "2026-09-21", "partial")
    dates = list_dates(tmp_path)
    assert [item["date"] for item in dates] == ["2026-09-21", "2026-09-20"]
    report = load_report(tmp_path)
    assert report["date"] == "2026-09-21"
    assert report["state"] == "partial"
    assert report["selected"][0]["title"] == "示例资讯"
    for value in ("../config.json", "2026-02-30", "20260921"):
        with pytest.raises(NewsError):
            load_report(tmp_path, value)


def test_news_reports_reject_missing_and_corrupt_data(tmp_path):
    # 缺失目录、损坏 JSON 和错误契约返回可理解错误。
    with pytest.raises(NewsError, match="不存在"):
        list_dates(tmp_path)
    folder = tmp_path / "reports" / "ai-news" / "2026-09-21"
    folder.mkdir(parents=True)
    (folder / "report.json").write_text("{", encoding="utf-8")
    with pytest.raises(NewsError, match="损坏"):
        load_report(tmp_path, "2026-09-21")
    (folder / "report.json").write_text(json.dumps({"date": "2026-09-21"}),
                                        encoding="utf-8")
    with pytest.raises(NewsError, match="格式"):
        load_report(tmp_path, "2026-09-21")


def test_wechat_daily_articles_are_safely_exposed(tmp_path):
    # 微信归档只返回摘要和安全原文链接，不泄露本地文件路径或正文。
    folder = tmp_path / "articles" / "2026-09-21"
    folder.mkdir(parents=True)
    (folder / "index.md").write_text(
        "# 索引\n\n消息 7 条；非链接 3 条；文章候选 4 个。\n状态：待处理\n"
        "- 不支持 — failed：正文不足\n",
        encoding="utf-8",
    )
    (folder / "example.md").write_text(
        '---\ntitle: "微信示例"\nsource_url: "https://mp.weixin.qq.com/s/abc"\n'
        'author: "示例作者"\npublish_time: "2026年9月21日 08:00"\n'
        'keywords: ["AI","产业"]\n---\n\n# 微信示例\n\n## 一句话结论\n\n'
        "一句话摘要。\n\n## 核心内容\n\n核心内容摘要。\n\n## 原文正文\n\n不应返回。",
        encoding="utf-8",
    )
    result = load_wechat_articles(tmp_path, "2026-09-21")
    assert result["metrics"] == {
        "messages": 7,
        "non_links": 3,
        "candidates": 4,
        "failed": 1,
    }
    assert result["items"][0]["title"] == "微信示例"
    assert result["items"][0]["conclusion"] == "一句话摘要。"
    assert result["items"][0]["summary"] == "核心内容摘要。"
    assert result["items"][0]["source_url"].startswith("https://mp.weixin.qq.com/")
    assert "原文正文" not in json.dumps(result, ensure_ascii=False)


def test_due_date_and_refresh_command_are_fixed(tmp_path):
    # 截止时刻前取上一日，刷新只调用固定脚本与日期参数。
    china = timezone.utc
    assert due_date(datetime(2026, 9, 21, 13, 59, tzinfo=china)) == "2026-09-20"
    assert due_date(datetime(2026, 9, 21, 14, 0, tzinfo=china)) == "2026-09-21"
    script = tmp_path / "scripts" / "run-ai-daily.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    write_report(tmp_path)
    called = {}

    def runner(command, **options):
        # 记录外部调用但不启动真实 Node 或模型服务。
        called["command"] = command
        called["options"] = options
        return SimpleNamespace(returncode=2)

    result = run_refresh(tmp_path, "2026-09-21", runner=runner)
    assert called["command"][-2:] == ["--date", "2026-09-21"]
    assert called["command"][0] == "powershell.exe"
    assert called["options"]["cwd"] == tmp_path.resolve()
    assert result["partial"] is True


def test_news_http_routes_refresh_and_lock(tmp_path):
    # 页面、历史接口、同源刷新和任务互斥均通过本地 HTTP 验证。
    write_report(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def runner(root, date):
        # 暂停模拟更新，以便断言第二次请求返回冲突。
        started.set()
        release.wait(5)
        return {"date": date, "state": "partial", "selected_count": 1, "partial": True}

    server = ReportServer(
        ("127.0.0.1", 0),
        report_db=tmp_path / "reports.db",
        research_db=tmp_path / "research.db",
        settings=fake_settings(tmp_path),
        news_runner=runner,
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/news") as response:
            assert "资讯聚合" in response.read().decode("utf-8")
        with urlopen(base + "/api/news/dates") as response:
            assert json.load(response)["items"][0]["date"] == "2026-09-21"
        with urlopen(base + "/api/news?date=2026-09-21") as response:
            assert json.load(response)["selected"][0]["title"] == "示例资讯"
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/news?date=../config.json")
        assert error.value.code == 400
        request = Request(base + "/api/news/refresh", method="POST",
                          headers={"Origin": base})
        with urlopen(request) as response:
            assert response.status == 202
        assert started.wait(2)
        with pytest.raises(HTTPError) as error:
            urlopen(request)
        assert error.value.code == 409
        release.set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()


def test_news_navigation_order_and_compact_header():
    # 资讯入口位于研究中心之后，摘要区不再继承通用白色卡片样式。
    static = Path(__file__).resolve().parents[2] / "src" / "invest" / "reporting" / "static"
    ui = (static / "ui.js").read_text(encoding="utf-8")
    css = (static / "news.css").read_text(encoding="utf-8")
    assert ui.index('["/research", "研究中心"]') < ui.index('["/news", "资讯聚合"]')
    assert ".news-page section.news-hero" in css
    assert "background:transparent" in css
    assert ".job:empty{display:none}" in css
