"""Playwright 打开公开行情页，读取页面自身的报价响应或可见报价。"""

import asyncio
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .common import CONFIG, EXTERNAL, REPORT_DB, SHANGHAI, load_config, now_iso, number
from .storage import connect, save_observation

CRYPTO = {"XBTUSD": "bitcoin", "ETHUSD": "ethereum", "SOLUSD": "solana", "XRPUSD": "xrp"}
FUTURE_UNITS = {"AU": "元/克", "AG": "元/千克", "CU": "元/吨", "SC": "元/桶"}


def source_url(category, code):
    # 将旧名单代码映射为公开页面，不自动更换标的或期货合约。
    if category == "a_share_indices":
        symbol = index_symbol(code)
        if code.endswith(".CSI"):
            return f"https://quotes.sina.cn/hs/company/quotes/view/{symbol}"
        return f"https://finance.sina.com.cn/realstock/company/{symbol}/nc.shtml"
    if category == "commodity_futures":
        return f"https://finance.sina.com.cn/futures/quotes/{code.split('.')[0]}.shtml"
    if category == "us_stocks":
        return f"https://stock.finance.sina.com.cn/usstock/quotes/{code}.html"
    return f"https://coinmarketcap.com/currencies/{CRYPTO[code]}/"


def parse_index(text, code, url):
    # 按页面实际请求的小数精度还原指数，拒绝顶栏其他指数响应。
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        return None
    data = json.loads(text[start:end + 1]).get("data") or {}
    expected = code.split(".")[0]
    if data.get("f57") != expected or not data.get("f86"):
        return None
    secid = parse_qs(urlparse(url).query).get("secid", [""])[0]
    market = "2" if code.endswith(".CSI") else "0" if code.endswith(".SZ") else "1"
    if secid != f"{market}.{expected}":
        return None
    raw = parse_qs(urlparse(url).query).get("fltt", ["1"])[0] == "1"
    digits = number(data.get("f59"))
    pct_digits = number(data.get("f152"))
    if digits is None or pct_digits is None or not 0 <= digits <= 6:
        return None
    scale = 10 ** int(digits) if raw else 1
    price = number(data.get("f43"))
    pct = number(data.get("f170"))
    timestamp = datetime.fromtimestamp(float(data["f86"]), SHANGHAI)
    return {
        "close": price / scale if price is not None else None,
        "pct_change": pct / (10 ** int(pct_digits) if raw else 1) if pct is not None else None,
        "source_date": timestamp.date().isoformat(), "quote_time": timestamp.isoformat(),
        "unit": "点", "source": "东方财富网页", "raw_evidence": data,
        "quote_kind": "latest", "timestamp_basis": "页面报价时间",
    }


def sina_fields(text, symbol):
    # 提取当前页面指定证券的原始行情串，忽略热门榜和其他证券。
    match = re.search(r'var\s+hq_str_' + re.escape(symbol) + r'="([^"]*)"', text)
    return match.group(1).split(",") if match and match.group(1) else None


def index_symbol(code):
    # CSI 指数使用新浪 SI 命名，沪深指数使用其交易所前缀。
    prefix = "si" if code.endswith(".CSI") else code.split(".")[1].lower()
    return prefix + code.split(".")[0]


def parse_sina_index(text, code):
    # 读取新浪指数完整行情串，日期取报价而非页面系统时钟。
    values = sina_fields(text, index_symbol(code))
    if not values or len(values) < 32:
        return None
    price, previous = number(values[3]), number(values[2])
    quote = datetime.strptime(values[30] + " " + values[31], "%Y-%m-%d %H:%M:%S")
    return dict(
        close=price, pct_change=(price / previous - 1) * 100
        if price is not None and previous and previous > 0 else None,
        source_date=quote.date().isoformat(), quote_time=quote.replace(tzinfo=SHANGHAI).isoformat(),
        unit="点", source="新浪指数网页", raw_evidence=values, quote_kind="latest",
        timestamp_basis="页面报价时间",
    )


def parse_future(text, code):
    # 读取合约价格与昨结算，保留夜盘页面日期而非推测交易日。
    symbol = code.split(".")[0]
    values = sina_fields(text, "nf_" + symbol)
    if not values or len(values) < 18:
        return None
    price, previous = number(values[8]), number(values[10])
    quote = datetime.strptime(values[17] + values[1].zfill(6), "%Y-%m-%d%H%M%S")
    product = re.match(r"[A-Z]+", symbol).group()
    return {
        "close": price, "pct_change": (price / previous - 1) * 100
        if price is not None and previous and previous > 0 else None,
        "source_date": quote.date().isoformat(), "quote_time": quote.replace(tzinfo=SHANGHAI)
        .isoformat(), "unit": FUTURE_UNITS.get(product, "按合约"),
        "volume": number(values[14]), "source": "新浪期货网页 / 倚天财经",
        "raw_evidence": values, "quote_kind": "latest_vs_previous_settlement",
        "timestamp_basis": "页面报价日期，夜盘交易日可能不同",
    }


def parse_us(text, code):
    # 读取常规时段价格及对应美东时间，不混入盘后价或北京时间刷新时间。
    values = sina_fields(text, "gb_" + code.lower())
    if not values or len(values) < 30:
        return None
    year = int(values[29])
    if not 1990 <= year <= datetime.now().year + 1:
        raise ValueError("美股来源年份格式变化")
    stamp = values[25]
    match = re.fullmatch(r"([A-Za-z]{3}) (\d{1,2}) (\d{2}:\d{2}[AP]M) (EDT|EST)", stamp)
    if not match:
        raise ValueError("美股页面未提供可识别的常规时段时间")
    quote = datetime.strptime(f"{year} {match[1]} {match[2]} {match[3]}", "%Y %b %d %I:%M%p")
    offset = -4 if match[4] == "EDT" else -5
    quote = quote.replace(tzinfo=timezone(timedelta(hours=offset)))
    return {
        "close": number(values[1]), "pct_change": number(values[2]),
        "source_date": quote.date().isoformat(), "quote_time": quote.isoformat(),
        "unit": "USD", "volume": number(values[10]), "source": "新浪美股网页",
        "raw_evidence": values, "quote_kind": "regular_session",
        "timestamp_basis": "美东常规时段报价时间",
    }


async def collect_page(context, category, code, name):
    # 为单标的打开独立页面，最多等待二十五秒的页面报价响应。
    page = await context.new_page()
    url = source_url(category, code)
    row = dict(category=category, code=code, name=name, source_url=url)
    pending = set()
    found = asyncio.get_running_loop().create_future()

    async def on_response(response):
        # 仅解析页面自己加载的行情响应，不直接请求隐藏接口或执行响应脚本。
        if found.done():
            return
        relevant = urlparse(response.url).hostname in ("hq.sinajs.cn", "w.sinajs.cn") or (
            category == "a_share_indices" and "/api/qt/stock/get" in response.url
        )
        if not relevant:
            return
        try:
            text = await response.text()
            if category == "a_share_indices":
                parsed = (parse_index(text, code, response.url)
                          if "eastmoney.com" in response.url else parse_sina_index(text, code))
            elif category == "commodity_futures":
                parsed = parse_future(text, code)
            else:
                parsed = parse_us(text, code)
            if parsed and parsed.get("close") and parsed["close"] > 0 and not found.done():
                found.set_result(parsed)
        except (ValueError, KeyError, TypeError, OverflowError):
            pass
        except Exception:
            pass

    def schedule(response):
        # 保留响应回调任务引用，关页前取消未完成任务。
        task = asyncio.create_task(on_response(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    try:
        if category != "crypto_pairs":
            page.on("response", schedule)
        response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        if response is not None and response.status >= 400:
            raise ValueError(f"来源页面返回 HTTP {response.status}")
        if category == "crypto_pairs":
            parsed = await read_crypto(page, code)
        else:
            parsed = await asyncio.wait_for(found, timeout=25)
        row.update(parsed)
        if not row.get("close") or row["close"] <= 0:
            raise ValueError("页面没有有效报价")
        row["status"] = "ok"
    except Exception as exc:
        message = str(exc).split("Call log:")[0].strip()
        row.update(status="error", error=message[:240] or "页面未在时限内返回该标的有效报价")
    finally:
        for task in tuple(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await page.close()
    row["observed_at"] = now_iso()
    return row


async def read_crypto(page, code):
    # 读取 CoinMarketCap 页面内嵌快照，校验资产身份和原始更新时间。
    script = page.locator("script#__NEXT_DATA__")
    await script.wait_for(state="attached", timeout=20000)
    return parse_crypto(await script.text_content(), code)


def parse_crypto(text, code):
    # 只读取当前资产的美元统计，不取推荐列表或其他资产价格。
    detail = json.loads(text)["props"]["pageProps"]["detailRes"]["detail"]
    if detail["slug"] != CRYPTO[code]:
        raise ValueError("虚拟货币页面资产与配置不一致")
    statistics = detail["statistics"]
    quote = datetime.fromisoformat(detail["latestUpdateTime"].replace("Z", "+00:00"))
    if quote.tzinfo is None:
        raise ValueError("虚拟货币报价时间缺少时区")
    return dict(
        close=number(statistics["price"]),
        pct_change=number(statistics["priceChangePercentage24h"]),
        source_date=quote.astimezone(SHANGHAI).date().isoformat(),
        quote_time=quote.isoformat(), source="CoinMarketCap 网页（美元综合价）", unit="USD",
        raw_evidence={"slug": detail["slug"], "statistics": statistics,
                      "latestUpdateTime": detail["latestUpdateTime"]},
        quote_kind="aggregate_usd_24h", timestamp_basis="页面内嵌行情更新时间（UTC）",
    )


async def collect_async(config, report_db):
    # 最多三个页面并发采集，逐条写入结果库以保留部分成功数据。
    from playwright.async_api import async_playwright

    conn = connect(report_db)
    run_id = conn.execute("INSERT INTO runs(started_at,status) VALUES (?,?)",
                          (now_iso(), "running")).lastrowid
    conn.commit()
    results = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="msedge", headless=True)
            context = await browser.new_context(locale="zh-CN")
            semaphore = asyncio.Semaphore(3)

            async def one(category, code, name):
                # 限制来源压力，每条记录独立提交，不因个别页面失败回滚整批。
                async with semaphore:
                    row = await collect_page(context, category, code, name)
                    if row["status"] == "error" and not any(
                        token in row.get("error", "") for token in ("HTTP 403", "HTTP 404")
                    ):
                        await asyncio.sleep(1)
                        row = await collect_page(context, category, code, name)
                        row["attempts"] = 2
                    save_observation(conn, row)
                    results.append(row)
                    print(f"{category} {code}: {row['status']}", flush=True)

            try:
                await asyncio.gather(*(one(category, code, name) for category in EXTERNAL
                                       for code, name in config[category].items()))
            finally:
                await browser.close()
        count = sum(row["status"] == "ok" for row in results)
        summary = {"total": len(results), "ok": count, "failed": len(results) - count}
        conn.execute("UPDATE runs SET finished_at=?,status=?,message=? WHERE id=?",
                     (now_iso(), "complete" if count == len(results) else "partial",
                      json.dumps(summary), run_id))
        conn.commit()
        return summary
    except Exception as exc:
        conn.execute("UPDATE runs SET finished_at=?,status=?,message=? WHERE id=?",
                     (now_iso(), "failed", str(exc)[:500], run_id))
        conn.commit()
        raise
    finally:
        conn.close()


def collect(config_path=CONFIG, report_db=REPORT_DB):
    # 同步 CLI 和后台线程调用的公共采集入口。
    with collection_lock(report_db):
        return asyncio.run(collect_async(load_config(config_path), report_db))


@contextmanager
def collection_lock(report_db):
    # 跨进程锁阻止网页、CLI 和每日脚本同时抓取，进程退出后系统自动释放。
    import os

    path = Path(report_db).with_suffix(".collect.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有网页采集任务运行中，请稍后查看结果") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
