"""仅监听本机的轻量日报网站，无需 Node 构建或 Web 框架。"""

import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from invest.reporting.common import CONFIG
from invest.reporting.common import REPORT_DB
from invest.reporting.common import SOURCE_DB
from invest.reporting.common import now_iso
from invest.storage.reports import connect
from invest.storage.reports import get_report
from invest.storage.reports import list_reports

STATIC = Path(__file__).parent / "static"
from invest.reporting.securities import bars as security_bars
from invest.reporting.securities import connect_readonly
from invest.reporting.securities import detail as security_detail
from invest.reporting.securities import search as search_securities
from invest.reporting.rankings import connect_readonly as connect_rankings
from invest.reporting.rankings import meta as rankings_meta
from invest.reporting.rankings import query as rankings_query


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
        self.recommendation_lock = threading.Lock()
        self.recommendation_job = {
            "running": False,
            "message": "尚未计算股票推荐",
        }
        self.recommendation_results = None
        self.etf_recommendation_lock = threading.Lock()
        self.etf_recommendation_job = {
            "running": False,
            "message": "尚未计算 ETF 推荐",
        }
        self.etf_recommendation_results = None

    def refresh(self):
        # 后台运行完整采集与生成，页面轮询只读取任务状态。
        from invest.providers.web import collect
        from invest.reporting.service import generate

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

    def run_recommendations(self):
        # 后台计算三种推荐类型，只在内存中保留当前结果。
        from invest.reporting.recommendations import generate

        def progress(message):
            # 更新可供页面轮询的计算阶段。
            self.recommendation_job = {"running": True, "message": message}

        try:
            progress("正在准备推荐计算…")
            result = generate(self.source_db, progress)
            self.recommendation_results = result
            self.recommendation_job = {
                "running": False,
                "message": "三种类型的候选已计算完成",
                "generated_at": result["generated_at"],
            }
        except Exception as exc:
            self.recommendation_job = {
                "running": False,
                "message": "推荐计算失败",
                "error": str(exc),
            }
        finally:
            self.recommendation_job["finished_at"] = now_iso()
            self.recommendation_lock.release()

    def run_etf_recommendations(self):
        # 后台计算全市场探索性 ETF 候选，只保留当前内存结果。
        from invest.reporting.etf_recommendations import generate

        def progress(message):
            # 向页面轮询接口写入当前计算阶段。
            self.etf_recommendation_job = {"running": True, "message": message}

        try:
            progress("正在准备 ETF 推荐计算…")
            result = generate(self.source_db, progress)
            self.etf_recommendation_results = result
            self.etf_recommendation_job = {
                "running": False,
                "message": "三种类型的 ETF 候选已计算完成",
                "generated_at": result["generated_at"],
            }
        except Exception as exc:
            self.etf_recommendation_job = {
                "running": False,
                "message": "ETF 推荐计算失败",
                "error": str(exc),
            }
        finally:
            self.etf_recommendation_job["finished_at"] = now_iso()
            self.etf_recommendation_lock.release()


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
                  "/securities": ("securities.html", "text/html"),
                  "/securities.html": ("securities.html", "text/html"),
                  "/securities.js": ("securities.js", "text/javascript"),
                  "/securities.css": ("securities.css", "text/css"),
                  "/rankings": ("rankings.html", "text/html"),
                  "/rankings.html": ("rankings.html", "text/html"),
                  "/rankings.js": ("rankings.js", "text/javascript"),
                  "/rankings.css": ("rankings.css", "text/css"),
                  "/recommendations": ("recommendations.html", "text/html"),
                  "/recommendations.html": ("recommendations.html", "text/html"),
                  "/recommendations.js": ("recommendations.js", "text/javascript"),
                  "/recommendations.css": ("recommendations.css", "text/css"),
                  "/etf-recommendations": ("etf_recommendations.html", "text/html"),
                  "/etf-recommendations.html": ("etf_recommendations.html", "text/html"),
                  "/etf-recommendations.js": ("etf_recommendations.js", "text/javascript"),
                  "/etf-recommendations.css": ("etf_recommendations.css", "text/css"),
                  "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                  "/app.js": ("app.js", "text/javascript"),
                  "/style.css": ("style.css", "text/css")}
        if parsed.path in assets:
            name, mime = assets[parsed.path]
            return self.reply(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")
        if parsed.path == "/api/job":
            return self.json_reply(200, self.server.job)
        if parsed.path == "/api/recommendations/job":
            return self.json_reply(200, self.server.recommendation_job)
        if parsed.path == "/api/etf-recommendations/job":
            return self.json_reply(200, self.server.etf_recommendation_job)
        if parsed.path in ("/api/recommendations", "/api/recommendations/detail"):
            return self.recommendation_api(parsed)
        if parsed.path in ("/api/etf-recommendations", "/api/etf-recommendations/detail"):
            return self.etf_recommendation_api(parsed)
        if parsed.path in ("/api/securities/search", "/api/security", "/api/security/bars"):
            return self.security_api(parsed)
        if parsed.path in ("/api/rankings/meta", "/api/rankings"):
            return self.rankings_api(parsed)
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

    def security_api(self, parsed):
        # 返回证券搜索、详情和历史行情。
        query = parse_qs(parsed.query)
        try:
            conn = connect_readonly(self.server.source_db)
            try:
                if parsed.path == "/api/securities/search":
                    items = search_securities(conn, query.get("q", [""])[0])
                    return self.json_reply(200, {"items": items})
                code = query.get("code", [""])[0].strip()
                if not code:
                    return self.json_reply(400, {"error": "缺少证券代码"})
                if parsed.path == "/api/security":
                    value = security_detail(conn, code)
                    if value:
                        return self.json_reply(200, value)
                    return self.json_reply(404, {"error": "证券不存在"})
                values = security_bars(
                    conn,
                    code,
                    query.get("start", [None])[0],
                    query.get("end", [None])[0],
                    query.get("period", ["day"])[0],
                )
                return self.json_reply(200, values)
            finally:
                conn.close()
        except (ValueError, FileNotFoundError) as exc:
            return self.json_reply(400, {"error": str(exc)})
        except Exception:
            return self.json_reply(500, {"error": "查询失败，请稍后重试"})

    def rankings_api(self, parsed):
        # 返回股票排行榜的只读数据与筛选元数据。
        query = parse_qs(parsed.query)
        try:
            conn = connect_rankings(self.server.source_db)
            try:
                if parsed.path.endswith("/meta"):
                    return self.json_reply(200, rankings_meta(conn))
                metric = query.get("metric", ["day_return"])[0]
                filters = {key: value[0] for key, value in query.items() if value}
                return self.json_reply(200, rankings_query(conn, metric, filters))
            finally:
                conn.close()
        except (ValueError, FileNotFoundError) as exc:
            return self.json_reply(400, {"error": str(exc)})
        except Exception:
            return self.json_reply(500, {"error": "排行榜查询失败，请稍后重试"})

    def recommendation_api(self, parsed):
        # 返回当前内存中的推荐列表或股票详情。
        result = self.server.recommendation_results
        if result is None:
            return self.json_reply(404, {"error": "尚无推荐结果，请先点击重新计算"})
        query = parse_qs(parsed.query)
        profile = query.get("profile", ["balanced"])[0]
        if profile not in result["profiles"]:
            return self.json_reply(400, {"error": "不支持的推荐类型"})
        profile_data = result["profiles"][profile]
        if parsed.path.endswith("/detail"):
            code = query.get("code", [""])[0].strip()
            item = next((row for row in profile_data["items"] if row["code"] == code), None)
            if item is None:
                return self.json_reply(404, {"error": "该股票不在当前候选中"})
            return self.json_reply(200, {
                "generated_at": result["generated_at"],
                "summary": result["summary"],
                "profile": profile_data["profile"],
                "profile_name": profile_data["profile_name"],
                "item": item,
            })
        return self.json_reply(200, {
            "generated_at": result["generated_at"],
            "summary": result["summary"],
            **profile_data,
        })

    def etf_recommendation_api(self, parsed):
        # 返回当前内存中的 ETF 推荐列表或详情。
        result = self.server.etf_recommendation_results
        if result is None:
            return self.json_reply(404, {"error": "尚无 ETF 推荐结果，请先点击重新计算"})
        query = parse_qs(parsed.query)
        profile = query.get("profile", ["balanced"])[0]
        if profile not in result["profiles"]:
            return self.json_reply(400, {"error": "不支持的推荐类型"})
        profile_data = result["profiles"][profile]
        if parsed.path.endswith("/detail"):
            code = query.get("code", [""])[0].strip()
            item = next((row for row in profile_data["items"] if row["code"] == code), None)
            if item is None:
                return self.json_reply(404, {"error": "该 ETF 不在当前候选中"})
            return self.json_reply(200, {
                "generated_at": result["generated_at"],
                "summary": result["summary"],
                "profile": profile_data["profile"],
                "profile_name": profile_data["profile_name"],
                "item": item,
            })
        return self.json_reply(200, {
            "generated_at": result["generated_at"],
            "summary": result["summary"],
            **profile_data,
        })

    def do_POST(self):
        # 同源按钮显式触发更新，不允许跨站调用或并行采集。
        origin = self.headers.get("Origin")
        if not self.valid_host() or origin != "http://" + self.headers.get("Host", ""):
            return self.json_reply(403, {"error": "只接受本网站发起的更新"})
        if self.path == "/api/recommendations/run":
            if not self.server.recommendation_lock.acquire(blocking=False):
                return self.json_reply(409, {"error": "推荐计算正在运行"})
            self.server.recommendation_job = {
                "running": True,
                "message": "正在启动推荐计算…",
            }
            threading.Thread(
                target=self.server.run_recommendations,
                daemon=True,
            ).start()
            return self.json_reply(202, self.server.recommendation_job)
        if self.path == "/api/etf-recommendations/run":
            if not self.server.etf_recommendation_lock.acquire(blocking=False):
                return self.json_reply(409, {"error": "ETF 推荐计算正在运行"})
            self.server.etf_recommendation_job = {
                "running": True,
                "message": "正在启动 ETF 推荐计算…",
            }
            threading.Thread(
                target=self.server.run_etf_recommendations,
                daemon=True,
            ).start()
            return self.json_reply(202, self.server.etf_recommendation_job)
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
    from invest.core.settings import load_settings

    parser = argparse.ArgumentParser(description="本地投资日报网站")
    runtime = load_settings().config["runtime"]
    parser.add_argument("--port", type=int, default=runtime["report_port"])
    parser.add_argument("--host", default=runtime["report_host"])
    parser.add_argument("--source-db", type=Path, default=SOURCE_DB)
    parser.add_argument("--report-db", type=Path, default=REPORT_DB)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    server = ReportServer((args.host, args.port), args.source_db, args.report_db, args.config)
    print(f"日报网站：http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
