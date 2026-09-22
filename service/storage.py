"""SQLite 持久化层。

所有业务表只追加或原地更新非审计状态；判定记录（assessments）构成
哈希链，任何旧记录都不可被追溯改写。
"""
import hashlib
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    latest_revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS release_revisions (
    release_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    data_categories TEXT NOT NULL,
    purpose TEXT NOT NULL,
    deployment_jurisdiction TEXT NOT NULL,
    vendor_chain TEXT NOT NULL,
    data_subject_jurisdictions TEXT NOT NULL DEFAULT '[]',
    involves_minors INTEGER NOT NULL DEFAULT 0,
    high_risk_decision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (release_id, revision),
    FOREIGN KEY (release_id) REFERENCES releases(id)
);

CREATE TABLE IF NOT EXISTS rule_packages (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    published_at TEXT,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    triggered_obligations TEXT NOT NULL,
    evidence_gaps TEXT NOT NULL,
    missing_roles TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    cert_snapshots TEXT NOT NULL,
    input_refs TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    valid_until TEXT
);

CREATE TABLE IF NOT EXISTS certificates (
    id TEXT PRIMARY KEY,
    release_id TEXT,
    jurisdiction TEXT,
    created_at TEXT NOT NULL,
    issuer TEXT NOT NULL,
    scope TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    assessment_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    signed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY,
    affiliations TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waivers (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    obligation_id TEXT NOT NULL,
    scope_note TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS eval_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    status TEXT NOT NULL,
    leased_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    assessment_id INTEGER
);
"""


class Store:
    """线程安全的 SQLite 封装，每个连接带线程锁与行工厂。"""

    def __init__(self, path=":memory:"):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- 基础工具 -------------------------------------------------------

    @property
    def lock(self):
        return self._lock

    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self):
        with self._lock:
            self._conn.close()

    # -- 判定记录哈希链 -------------------------------------------------

    def last_assessment_hash(self):
        row = self.query_one(
            "SELECT entry_hash FROM assessments ORDER BY id DESC LIMIT 1")
        return row["entry_hash"] if row else ""

    @staticmethod
    def hash_entry(prev_hash, payload):
        """对前一哈希与规范化 JSON 载荷做 SHA-256。"""
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256()
        digest.update(prev_hash.encode())
        digest.update(b"\n")
        digest.update(body.encode())
        return digest.hexdigest()

    def append_assessment(self, record):
        """原子追加一条哈希链判定记录，返回自增 id。"""
        payload = {
            "release_id": record.release_id,
            "revision": record.revision,
            "rule_version": record.rule_version,
            "evaluated_at": record.evaluated_at,
            "triggered_obligations": record.triggered_obligations,
            "evidence_gaps": record.evidence_gaps,
            "missing_roles": record.missing_roles,
            "decision": record.decision,
            "rationale": record.rationale,
            "evidence_ids": record.evidence_ids,
            "cert_snapshots": record.cert_snapshots,
            "input_refs": record.input_refs,
        }
        with self._lock:
            prev_hash = self.last_assessment_hash()
            entry_hash = self.hash_entry(prev_hash, payload)
            cur = self._conn.execute(
                """INSERT INTO assessments
                   (release_id, revision, rule_version, evaluated_at,
                    triggered_obligations, evidence_gaps, missing_roles,
                    decision, rationale, evidence_ids, cert_snapshots,
                    input_refs, prev_hash, entry_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record.release_id, record.revision, record.rule_version,
                 record.evaluated_at,
                 json.dumps(record.triggered_obligations, ensure_ascii=False),
                 json.dumps(record.evidence_gaps, ensure_ascii=False),
                 json.dumps(record.missing_roles, ensure_ascii=False),
                 record.decision,
                 json.dumps(record.rationale, ensure_ascii=False),
                 json.dumps(record.evidence_ids),
                 json.dumps(record.cert_snapshots, ensure_ascii=False),
                 json.dumps(record.input_refs, ensure_ascii=False),
                 prev_hash, entry_hash))
            self._conn.commit()
            return cur.lastrowid

    def verify_chain(self):
        """重算整条哈希链，供审计端点与自检使用。返回(条数, 是否一致)。"""
        rows = self.query(
            "SELECT * FROM assessments ORDER BY id ASC")
        prev = ""
        for row in rows:
            payload = {
                "release_id": row["release_id"],
                "revision": row["revision"],
                "rule_version": row["rule_version"],
                "evaluated_at": row["evaluated_at"],
                "triggered_obligations": json.loads(row["triggered_obligations"]),
                "evidence_gaps": json.loads(row["evidence_gaps"]),
                "missing_roles": json.loads(row["missing_roles"]),
                "decision": row["decision"],
                "rationale": json.loads(row["rationale"]),
                "evidence_ids": json.loads(row["evidence_ids"]),
                "cert_snapshots": json.loads(row["cert_snapshots"]),
                "input_refs": json.loads(row["input_refs"]),
            }
            expected = self.hash_entry(prev, payload)
            if expected != row["entry_hash"] or row["prev_hash"] != prev:
                return len(rows), False
            prev = row["entry_hash"]
        return len(rows), True
