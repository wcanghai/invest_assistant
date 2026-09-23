"""研究数据库：外部证据、分析运行和可追溯报告。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_document (
 document_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, source_tier TEXT NOT NULL,
 document_type TEXT NOT NULL, title TEXT, published_at TEXT, effective_at TEXT,
 collected_at TEXT NOT NULL, content_hash TEXT, raw_path TEXT, parser_version TEXT,
 status TEXT NOT NULL DEFAULT 'available', metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS document_entity (
 document_id TEXT NOT NULL, entity_type TEXT NOT NULL, entity_code TEXT NOT NULL,
 entity_name TEXT, PRIMARY KEY(document_id, entity_type, entity_code),
 FOREIGN KEY(document_id) REFERENCES external_document(document_id)
);
CREATE TABLE IF NOT EXISTS research_event (
 event_id TEXT PRIMARY KEY, document_id TEXT, entity_type TEXT NOT NULL,
 entity_code TEXT NOT NULL, event_type TEXT NOT NULL, direction TEXT NOT NULL,
 horizon TEXT NOT NULL, importance TEXT NOT NULL, confidence TEXT NOT NULL,
 summary TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_relation (
 relation_id TEXT PRIMARY KEY, left_type TEXT NOT NULL, left_code TEXT NOT NULL,
 right_type TEXT NOT NULL, right_code TEXT NOT NULL, relation_type TEXT NOT NULL,
 valid_from TEXT, valid_to TEXT, published_at TEXT, source_url TEXT,
 source_tier TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS index_constituent (
 index_code TEXT NOT NULL, security_code TEXT NOT NULL, weight REAL,
 valid_from TEXT, valid_to TEXT, published_at TEXT, source_url TEXT NOT NULL,
 source_tier TEXT NOT NULL, content_hash TEXT, PRIMARY KEY(index_code, security_code, valid_from)
);
CREATE TABLE IF NOT EXISTS fund_profile (
 etf_code TEXT PRIMARY KEY, manager TEXT, manager_name TEXT, fee_rate REAL,
 custodian_fee REAL, inception_date TEXT, profile_json TEXT NOT NULL DEFAULT '{}',
 published_at TEXT, source_url TEXT, source_tier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS etf_basket (
 etf_code TEXT NOT NULL, basket_date TEXT NOT NULL, security_code TEXT NOT NULL,
 weight REAL, cash_substitute TEXT, source_url TEXT NOT NULL, source_tier TEXT NOT NULL,
 PRIMARY KEY(etf_code, basket_date, security_code)
);
CREATE TABLE IF NOT EXISTS source_fetch_run (
 run_id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, pages INTEGER DEFAULT 0,
 records INTEGER DEFAULT 0, error TEXT, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS analysis_run (
 run_id TEXT PRIMARY KEY, input_type TEXT NOT NULL, input_json TEXT NOT NULL,
 parsed_json TEXT NOT NULL DEFAULT '{}', local_as_of TEXT, external_as_of TEXT,
 status TEXT NOT NULL, model_name TEXT, prompt_version TEXT, error TEXT,
 started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS analysis_evidence (
 evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, evidence_type TEXT NOT NULL,
 source_table TEXT, source_key TEXT, value_json TEXT NOT NULL,
 source_url TEXT, published_at TEXT, collected_at TEXT, source_tier TEXT,
 FOREIGN KEY(run_id) REFERENCES analysis_run(run_id)
);
CREATE TABLE IF NOT EXISTS analysis_result (
 run_id TEXT PRIMARY KEY, quant_score REAL, ai_score REAL, confidence TEXT,
 result_json TEXT NOT NULL, warnings_json TEXT NOT NULL DEFAULT '[]',
 FOREIGN KEY(run_id) REFERENCES analysis_run(run_id)
);
CREATE INDEX IF NOT EXISTS idx_event_entity ON research_event(entity_type, entity_code, created_at);
CREATE INDEX IF NOT EXISTS idx_document_published ON external_document(published_at);
"""

def connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_run(input_type: str, payload: dict, local_as_of: str | None = None, path=None) -> str:
    run_id = uuid4().hex
    with connect(path) as conn:
        conn.execute("INSERT INTO analysis_run(run_id,input_type,input_json,local_as_of,status,started_at) VALUES(?,?,?,?,?,?)",
                     (run_id, input_type, json.dumps(payload, ensure_ascii=False), local_as_of, "running", now()))
    return run_id

def finish_run(run_id: str, result: dict, quant_score: float | None, ai_score: float | None,
               confidence: str, warnings: list[str], path=None, error: str | None = None):
    with connect(path) as conn:
        conn.execute("UPDATE analysis_run SET status=?,finished_at=?,error=? WHERE run_id=?",
                     ("failed" if error else "completed", now(), error, run_id))
        conn.execute("INSERT OR REPLACE INTO analysis_result VALUES(?,?,?,?,?,?)",
                     (run_id, quant_score, ai_score, confidence, json.dumps(result, ensure_ascii=False),
                      json.dumps(warnings, ensure_ascii=False)))
        for item in result.get("evidence", []):
            conn.execute("INSERT OR REPLACE INTO analysis_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (item["id"], run_id, item.get("type", "fact"), item.get("table"),
                          item.get("key"), json.dumps(item.get("value"), ensure_ascii=False),
                          item.get("url"), item.get("published_at"), item.get("collected_at"), item.get("tier")))
