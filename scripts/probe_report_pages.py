"""开发验证：查看公开行情页面的主报价和页面自身的行情响应。"""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

URLS = {
    "index": "https://quote.eastmoney.com/zs000001.html",
    "futures": "https://finance.sina.com.cn/futures/quotes/AU2610.shtml",
    "us": "https://finance.yahoo.com/quote/AAPL/",
    "crypto": "https://www.kraken.com/prices/bitcoin",
}
DEST = Path(__file__).resolve().parents[1] / ".tmp/report_probe"


async def inspect(context, key, url):
    # 保存可见页面文本和页面主动请求的有限行情证据。
    page = await context.new_page()
    captured = []
    tasks = []

    async def response(res):
        # 只检查行情响应，不保存登录态或其他浏览器数据。
        if any(token in res.url for token in ("hq.sinajs", "stock/get", "/chart/")):
            try:
                captured.append({"url": res.url, "text": (await res.text())[:12000]})
            except Exception:
                pass

    def schedule(res):
        # 保持异步响应读取任务的引用直到页面检查完成。
        tasks.append(asyncio.create_task(response(res)))

    page.on("response", schedule)
    try:
        res = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(12000)
        text = await page.locator("body").inner_text(timeout=10000)
        DEST.mkdir(parents=True, exist_ok=True)
        (DEST / f"{key}.txt").write_text(text, encoding="utf-8")
        await page.screenshot(path=str(DEST / f"{key}.png"))
        await asyncio.gather(*tasks, return_exceptions=True)
        (DEST / f"{key}.json").write_text(json.dumps(captured), encoding="utf-8")
        print(key, res.status if res else None, page.url, text[:400], flush=True)
    except Exception as exc:
        print(key, str(exc)[:500], flush=True)
    finally:
        await page.close()


async def main():
    # 使用独立 Edge 无头浏览器并行检查四类公开来源。
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 950})
        await asyncio.gather(*(inspect(context, key, url) for key, url in URLS.items()))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
