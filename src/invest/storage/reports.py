"""独立日报库，保存每次采集证据和不可变报告版本。"""

import hashlib
import json
import sqlite3
from pathlib import Path

from invest.reporting.common import REPORT_DB
from invest.reporting.common import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, category TEXT NOT NULL, code TEXT NOT NULL,
 observed_at TEXT NOT NULL, source_date TEXT, status TEXT NOT NULL,
 payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observation_lookup
 ON observations(category, code, observed_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY, report_date TEXT NOT NULL, version INTEGER NOT NULL,
 generated_at TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT NOT NULL,
 payload TEXT NOT NULL, markdown TEXT NOT NULL,
 UNIQUE(report_date, version), UNIQUE(report_date, fingerprint)
);
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, message TEXT
);
"""


def connect(path=REPORT_DB):
    # 初始化独立结果库，不对行情主库执行迁移。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def save_observation(conn, row):
    # 每次网页采集保留成功或失败证据。
    conn.execute(
        "INSERT INTO observations(category,code,observed_at,source_date,status,payload) "
        "VALUES (?,?,?,?,?,?)",
        (row["category"], row["code"], row["observed_at"], row.get("source_date"),
         row["status"], json.dumps(row, ensure_ascii=False, allow_nan=False)),
    )
    conn.commit()


def latest_observations(conn, config, cutoff):
    # 历史报告仅使用截止时刻前采集的记录，失败时保留最近成功值及失败原因。
    from invest.reporting.common import EXTERNAL

    result = {}
    for category in EXTERNAL:
        result[category] = []
        for code, name in config[category].items():
            latest_row = conn.execute(
                "SELECT payload FROM observations WHERE category=? AND code=? "
                "AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1",
                (category, code, cutoff),
            ).fetchone()
            success_row = conn.execute(
                "SELECT payload FROM observations WHERE category=? AND code=? "
                "AND observed_at<=? AND status='ok' ORDER BY observed_at DESC,id DESC LIMIT 1",
                (category, code, cutoff),
            ).fetchone()
            latest = json.loads(latest_row[0]) if latest_row else None
            successful = json.loads(success_row[0]) if success_row else None
            row = dict(successful or latest or {})
            row.update(category=category, code=code, name=name)
            if not latest:
                row.update(status="missing", error="尚无网页采集记录")
            elif latest["status"] != "ok":
                row.update(status="stale" if successful else "error")
                row["error"] = latest.get("error", "最近一次采集失败")
            result[category].append(row)
    return result


def save_report(conn, payload, markdown):
    # 在事务内分配版本，同输入重复运行返回原版本。
    content = {k: v for k, v in payload.items() if k != "generated_at"}
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT id FROM reports WHERE report_date=? AND fingerprint=?",
            (payload["report_date"], fingerprint),
        ).fetchone()
        if existing:
            conn.commit()
            return existing[0]
        version = conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM reports WHERE report_date=?",
            (payload["report_date"],),
        ).fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO reports(report_date,version,generated_at,status,fingerprint,"
            "payload,markdown) VALUES (?,?,?,?,?,?,?)",
            (payload["report_date"], version, payload["generated_at"], payload["status"],
             fingerprint, json.dumps(payload, ensure_ascii=False, allow_nan=False), markdown),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise


def list_reports(conn):
    # 返回不包含正文的版本目录。
    return [dict(r) for r in conn.execute(
        "SELECT id,report_date,version,generated_at,status FROM reports "
        "ORDER BY report_date DESC,version DESC LIMIT 500"
    )]


def get_report(conn, report_id=None):
    # 默认显示最新日期的完整版本，若不存在则显示该日最新部分版本。
    if report_id is None:
        row = conn.execute(
            "SELECT * FROM reports ORDER BY report_date DESC,"
            "(status='complete') DESC,version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["data"] = json.loads(result.pop("payload"))
    return result
