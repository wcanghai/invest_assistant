"""读取并触发外部 web_read AI 资讯日报。"""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Callable


SHANGHAI = timezone(timedelta(hours=8))
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_STATES = {"complete", "partial", "failed", "scrape_only"}


class NewsError(ValueError):
    # 表示可安全返回给页面的资讯集成错误。
    pass


def web_read_root(settings) -> Path:
    # 从应用配置解析 web_read 根目录。
    value = settings.config.get("integrations", {}).get("web_read_root")
    if not isinstance(value, str) or not value.strip():
        raise NewsError("尚未配置资讯源项目路径")
    return settings.path(value)


def report_root(root: Path) -> Path:
    # 返回固定的 AI 资讯报告目录，不接受外部路径片段。
    return root.resolve() / "reports" / "ai-news"


def validate_date(value: str) -> str:
    # 严格校验业务日期，阻止路径穿越和无效日历日期。
    if not DATE_PATTERN.fullmatch(value):
        raise NewsError("日期格式应为 YYYY-MM-DD")
    try:
        if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
            raise ValueError
    except ValueError as exc:
        raise NewsError("日期不是有效日历日期") from exc
    return value


def _read_report_file(path: Path) -> dict:
    # 读取并校验页面使用的最小报告契约。
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise NewsError("该日期尚无资讯报告") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise NewsError("资讯报告无法读取或内容损坏") from exc
    required_lists = ("overview", "selected", "extras", "sources", "errors")
    if not isinstance(value, dict) or any(
            not isinstance(value.get(key), list) for key in required_lists):
        raise NewsError("资讯报告格式不受支持")
    if value.get("state") not in VALID_STATES or not isinstance(value.get("date"), str):
        raise NewsError("资讯报告状态或日期无效")
    validate_date(value["date"])
    if path.parent.name != value["date"]:
        raise NewsError("资讯报告日期与目录不一致")
    for item in value["selected"] + value["extras"]:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            raise NewsError("资讯报告条目格式无效")
    for source in value["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("name"), str):
            raise NewsError("资讯来源状态格式无效")
    return value


def list_dates(root: Path) -> list[dict]:
    # 按日期倒序枚举经过校验的报告摘要，损坏目录不会拖垮列表。
    folder = report_root(root)
    if not folder.is_dir():
        raise NewsError("资讯源项目或报告目录不存在")
    result = []
    for child in folder.iterdir():
        if not child.is_dir() or not DATE_PATTERN.fullmatch(child.name):
            continue
        try:
            report = _read_report_file(child / "report.json")
        except NewsError:
            continue
        result.append({
            "date": report["date"],
            "state": report["state"],
            "generated_at": report.get("generatedAt"),
            "selected_count": len(report["selected"]),
        })
    return sorted(result, key=lambda item: item["date"], reverse=True)


def load_report(root: Path, date: str | None = None) -> dict:
    # 读取指定日期或最新日期的结构化资讯报告。
    if date is None:
        dates = list_dates(root)
        if not dates:
            raise NewsError("尚无可用的资讯报告")
        date = dates[0]["date"]
    date = validate_date(date)
    path = report_root(root) / date / "report.json"
    report = _read_report_file(path)
    return {
        "date": report["date"],
        "window": report.get("window", {}),
        "generated_at": report.get("generatedAt"),
        "state": report["state"],
        "model_incomplete": bool(report.get("modelIncomplete")),
        "overview": report["overview"],
        "selected": report["selected"][:10],
        "extras": report["extras"][:5],
        "sources": report["sources"],
        "errors": [str(item) for item in report["errors"]],
        "metrics": report.get("metrics", {}),
        "wechat": load_wechat_articles(root, date),
    }


def _section(markdown: str, heading: str) -> str:
    # 提取文章 Markdown 中指定二级标题的正文。
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\s*(.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _frontmatter_value(markdown: str, key: str) -> str:
    # 读取简单字符串型 YAML 头字段，不引入额外解析依赖。
    match = re.search(rf'^\s*{re.escape(key)}:\s*"(.*)"\s*$', markdown, re.MULTILINE)
    return match.group(1).replace(r'\"', '"').strip() if match else ""


def _safe_web_url(value: str) -> str:
    # 仅向页面返回 HTTP 与 HTTPS 原文链接。
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    return value if parsed.scheme in ("http", "https") and parsed.netloc else ""


def load_wechat_articles(root: Path, date: str) -> dict:
    # 读取指定业务日的微信归档摘要，不暴露原始数据库与本地文件路径。
    date = validate_date(date)
    folder = root.resolve() / "articles" / date
    if not folder.is_dir():
        return {"date": date, "status": "尚无归档", "metrics": {}, "items": []}
    index_file = folder / "index.md"
    index = index_file.read_text(encoding="utf-8-sig") if index_file.is_file() else ""
    count_match = re.search(
        r"消息\s*(\d+)\s*条；非链接\s*(\d+)\s*条；文章候选\s*(\d+)\s*个",
        index,
    )
    status_match = re.search(r"^状态：(.+)$", index, re.MULTILINE)
    metrics = {}
    if count_match:
        metrics = {
            "messages": int(count_match.group(1)),
            "non_links": int(count_match.group(2)),
            "candidates": int(count_match.group(3)),
            "failed": len(re.findall(r"—\s*failed", index)),
        }
    items = []
    for path in sorted(folder.glob("*.md")):
        if path.name.lower() == "index.md":
            continue
        try:
            markdown = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        title = _frontmatter_value(markdown, "title")
        if not title:
            title_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else path.stem
        keywords_match = re.search(r"^keywords:\s*\[(.*)\]\s*$", markdown, re.MULTILINE)
        keywords = re.findall(r'"([^"]+)"', keywords_match.group(1)) if keywords_match else []
        items.append({
            "title": title,
            "author": _frontmatter_value(markdown, "author"),
            "publish_time": _frontmatter_value(markdown, "publish_time"),
            "source_url": _safe_web_url(_frontmatter_value(markdown, "source_url")),
            "conclusion": _section(markdown, "一句话结论"),
            "summary": _section(markdown, "核心内容"),
            "keywords": keywords[:8],
        })
    return {
        "date": date,
        "status": status_match.group(1).strip() if status_match else "已归档",
        "metrics": metrics,
        "items": items[:30],
    }


def due_date(now: datetime | None = None, cutoff_hour: int = 22) -> str:
    # 返回最近已经结束的北京时间日报窗口日期。
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    if current.hour < cutoff_hour:
        current -= timedelta(days=1)
    return current.date().isoformat()


def run_refresh(
        root: Path,
        date: str,
        timeout: int = 3000,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    # 用固定脚本和参数重跑指定到期批次，不透传网页输入为命令。
    date = validate_date(date)
    root = root.resolve()
    script = root / "scripts" / "run-ai-daily.ps1"
    if not script.is_file():
        raise NewsError("资讯更新脚本不存在")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "--date",
        date,
    ]
    try:
        result = runner(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise NewsError("资讯更新超时，稍后可查看原项目日志") from exc
    except OSError as exc:
        raise NewsError("无法启动资讯更新程序") from exc
    if result.returncode not in (0, 2):
        raise NewsError("资讯更新失败，请查看原项目日志")
    report = load_report(root, date)
    return {
        "date": date,
        "state": report["state"],
        "selected_count": len(report["selected"]),
        "partial": result.returncode == 2 or report["state"] == "partial",
    }
