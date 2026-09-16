"""仅监听本机的轻量日报网站，无需 Node 构建或 Web 框架。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .common import CONFIG, REPORT_DB, SOURCE_DB, now_iso
from .storage import connect, get_report, list_reports

STATIC = Path(__file__).parent / "static"


class ReportServer(ThreadingHTTPServer):
    # 网站服务器持有单任务刷新状态，避免重复启动浏览器。

    def __init__(self, address, source_db=SOURCE_DB, report_db=REPORT_DB, config=CONFIG):
        # 注入数据库与配置，支持离线测试。
        super().__init__(address, Handler)
        self.source_db = source_db
        self.report_db = report_db
        self.config = config
        self.refresh_lock = threading.Lock()
        self.job = {"running": False, "message": "尚未执行网页更新"}

    def refresh(self):
        # 后台运行完整采集与生成，页面轮询只读取任务状态。
        from .sources import collect
        from .service import generate

        try:
            self.job = {"running": True, "message": "正在打开网页并采集行情…"}
            counts = collect(self.config, self.report_db)
            report = generate(source_db=self.source_db, report_db=self.report_db,
                              config_path=self.config)
            self.job = {"running": False, "message":
                        f"更新完成：网页行情 {counts['ok']}/{counts['total']}，日报已保存",
                        "report_id": report["id"]}
        except Exception as exc:
            self.job = {"running": False, "message": f"更新失败：{exc}"}
        finally:
            self.job["finished_at"] = now_iso()
            self.refresh_lock.release()


class Handler(BaseHTTPRequestHandler):
    # 只开放固定静态资源、只读报告和同源刷新入口。

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        # 为所有响应设置长度与基本浏览器安全头。
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def json_reply(self, status, value):
        # 序列化中文 JSON 响应。
        self.reply(status, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def valid_host(self):
        # 阻止非本机 Host 请求访问本地投资数据。
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        # 路由静态页面、版本目录、快照和 Markdown 下载。
        if not self.valid_host():
            return self.json_reply(403, {"error": "仅允许本机访问"})
        parsed = urlparse(self.path)
        assets = {"/": ("index.html", "text/html"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                  "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if parsed.path in assets:
            name, mime = assets[parsed.path]
            return self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
        if parsed.path == "/api/job":
            return self.json_reply(200, self.server.job)
        conn = connect(self.server.report_db)
        try:
            if parsed.path == "/api/reports":
                return self.json_reply(200, list_reports(conn))
            if parsed.path in ("/api/report", "/report.md"):
                value = parse_qs(parsed.query).get("id", [None])[0]
                report = get_report(conn, int(value) if value is not None else None)
                if not report:
                    return self.json_reply(404, {"error": "尚无日报，请先生成"})
                if parsed.path == "/report.md":
                    return self.reply(200, report["markdown"], "text/markdown; charset=utf-8")
                return self.json_reply(200, report)
            self.json_reply(404, {"error": "页面不存在"})
        except ValueError:
            self.json_reply(400, {"error": "报告编号格式错误"})
        finally:
            conn.close()

    def do_POST(self):
        # 同源按钮显式触发更新，不允许跨站调用或并行采集。
        origin = self.headers.get("Origin")
        if not self.valid_host() or origin != "http://" + self.headers.get("Host", ""):
            return self.json_reply(403, {"error": "只接受本网站发起的更新"})
        if self.path != "/api/refresh":
            return self.json_reply(404, {"error": "接口不存在"})
        if not self.server.refresh_lock.acquire(blocking=False):
            return self.json_reply(409, {"error": "已有更新任务运行中"})
        self.server.job = {"running": True, "message": "正在启动采集…"}
        threading.Thread(target=self.server.refresh, daemon=True).start()
        self.json_reply(202, self.server.job)


def main():
    # 启动仅本机可用的轻量 HTTP 服务。
    import argparse

    parser = argparse.ArgumentParser(description="本地投资日报网站")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--source-db", type=Path, default=SOURCE_DB)
    parser.add_argument("--report-db", type=Path, default=REPORT_DB)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    server = ReportServer(("127.0.0.1", args.port), args.source_db, args.report_db, args.config)
    print(f"日报网站：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
