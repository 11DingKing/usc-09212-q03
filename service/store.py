"""持久化层。

使用 SQLite 保存业务数据。审计表（audit）与发布事件表（release_events）
只追加、不更新，满足“审计记录不得以覆盖方式修改”的领域约定；
评估队列（queue）持久化，系统重启后可恢复继续处理。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone


def utcnow() -> str:
    """服务端接收时间（ISO8601 UTC）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    profile_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, version)
);
CREATE TABLE IF NOT EXISTS rulesets (
    ruleset_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (ruleset_id, version)
);
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    ruleset_id TEXT NOT NULL,
    ruleset_version INTEGER NOT NULL,
    release_id TEXT,
    status TEXT NOT NULL,
    result TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviewers (
    reviewer_id TEXT PRIMARY KEY,
    org TEXT NOT NULL,
    roles TEXT NOT NULL,
    conflicts TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    eval_id TEXT NOT NULL,
    role TEXT NOT NULL,
    reviewer_id TEXT NOT NULL,
    org TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (eval_id, role)
);
CREATE TABLE IF NOT EXISTS exemptions (
    exemption_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    rule_ids TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    obligation_ids TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    granted_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS certificates (
    cert_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    cert_type TEXT NOT NULL,
    issuer TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    eval_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload TEXT,
    occurred_at TEXT,
    recorded_at TEXT NOT NULL
);
"""


class Store:
    """线程安全的 SQLite 存储。所有写操作串行化并立即提交。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- 内部工具 -------------------------------------------------------

    def _execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql, params=()):
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ---- 档案（只追加版本） --------------------------------------------

    def append_profile(self, profile_id: str, payload: dict) -> int:
        row = self._query_one(
            "SELECT MAX(version) AS v FROM profiles WHERE profile_id=?", (profile_id,)
        )
        version = (row["v"] or 0) + 1
        self._execute(
            "INSERT INTO profiles VALUES (?,?,?,?)",
            (profile_id, version, json.dumps(payload, ensure_ascii=False), utcnow()),
        )
        return version

    def get_profile(self, profile_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self._query_one(
                "SELECT * FROM profiles WHERE profile_id=? ORDER BY version DESC LIMIT 1",
                (profile_id,),
            )
        else:
            row = self._query_one(
                "SELECT * FROM profiles WHERE profile_id=? AND version=?",
                (profile_id, version),
            )
        if row is None:
            return None
        return {
            "profile_id": row["profile_id"],
            "version": row["version"],
            "payload": json.loads(row["payload"]),
            "recorded_at": row["recorded_at"],
        }

    # ---- 规则集（只追加版本） ------------------------------------------

    def append_ruleset(self, ruleset_id: str, rules: list) -> int:
        row = self._query_one(
            "SELECT MAX(version) AS v FROM rulesets WHERE ruleset_id=?", (ruleset_id,)
        )
        version = (row["v"] or 0) + 1
        self._execute(
            "INSERT INTO rulesets VALUES (?,?,?,?)",
            (ruleset_id, version, json.dumps(rules, ensure_ascii=False), utcnow()),
        )
        return version

    def get_ruleset(self, ruleset_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self._query_one(
                "SELECT * FROM rulesets WHERE ruleset_id=? ORDER BY version DESC LIMIT 1",
                (ruleset_id,),
            )
        else:
            row = self._query_one(
                "SELECT * FROM rulesets WHERE ruleset_id=? AND version=?",
                (ruleset_id, version),
            )
        if row is None:
            return None
        return {
            "ruleset_id": row["ruleset_id"],
            "version": row["version"],
            "rules": json.loads(row["payload"]),
            "recorded_at": row["recorded_at"],
        }

    # ---- 评估 ----------------------------------------------------------

    def create_evaluation(self, eval_id, profile_id, profile_version,
                          ruleset_id, ruleset_version, release_id=None):
        self._execute(
            "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?,?)",
            (eval_id, profile_id, profile_version, ruleset_id, ruleset_version,
             release_id, "PENDING", None, utcnow()),
        )

    def complete_evaluation(self, eval_id: str, result: dict, status: str = "COMPLETED"):
        self._execute(
            "UPDATE evaluations SET status=?, result=? WHERE eval_id=?",
            (status, json.dumps(result, ensure_ascii=False), eval_id),
        )

    def get_evaluation(self, eval_id: str) -> dict | None:
        row = self._query_one("SELECT * FROM evaluations WHERE eval_id=?", (eval_id,))
        return self._eval_from_row(row) if row else None

    def latest_completed_evaluation(self, profile_id, profile_version,
                                    ruleset_id, ruleset_version) -> dict | None:
        row = self._query_one(
            "SELECT * FROM evaluations WHERE profile_id=? AND profile_version=?"
            " AND ruleset_id=? AND ruleset_version=? AND status='COMPLETED'"
            " ORDER BY recorded_at DESC LIMIT 1",
            (profile_id, profile_version, ruleset_id, ruleset_version),
        )
        return self._eval_from_row(row) if row else None

    @staticmethod
    def _eval_from_row(row):
        return {
            "eval_id": row["eval_id"],
            "profile_id": row["profile_id"],
            "profile_version": row["profile_version"],
            "ruleset_id": row["ruleset_id"],
            "ruleset_version": row["ruleset_version"],
            "release_id": row["release_id"],
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "recorded_at": row["recorded_at"],
        }

    # ---- 审查者与签署 ---------------------------------------------------

    def put_reviewer(self, reviewer_id, org, roles, conflicts):
        self._execute(
            "INSERT OR REPLACE INTO reviewers VALUES (?,?,?,?,?)",
            (reviewer_id, org, json.dumps(roles), json.dumps(conflicts), utcnow()),
        )

    def get_reviewer(self, reviewer_id) -> dict | None:
        row = self._query_one("SELECT * FROM reviewers WHERE reviewer_id=?", (reviewer_id,))
        if row is None:
            return None
        return {
            "reviewer_id": row["reviewer_id"],
            "org": row["org"],
            "roles": json.loads(row["roles"]),
            "conflicts": json.loads(row["conflicts"]),
        }

    def add_signature(self, eval_id, role, reviewer_id, org) -> bool:
        """按 (eval_id, role) 唯一约束写入；并发下同角色仅一人成功。"""
        try:
            self._execute(
                "INSERT INTO signatures VALUES (?,?,?,?,?)",
                (eval_id, role, reviewer_id, org, utcnow()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def list_signatures(self, eval_id) -> list:
        rows = self._query(
            "SELECT * FROM signatures WHERE eval_id=? ORDER BY recorded_at", (eval_id,)
        )
        return [
            {"eval_id": r["eval_id"], "role": r["role"],
             "reviewer_id": r["reviewer_id"], "org": r["org"],
             "recorded_at": r["recorded_at"]}
            for r in rows
        ]

    # ---- 豁免与证书 -----------------------------------------------------

    def add_exemption(self, exemption_id, profile_id, rule_ids, evidence_ids,
                      obligation_ids, expires_at, reason, granted_by):
        self._execute(
            "INSERT INTO exemptions VALUES (?,?,?,?,?,?,?,?,?)",
            (exemption_id, profile_id, json.dumps(rule_ids), json.dumps(evidence_ids),
             json.dumps(obligation_ids), expires_at, reason, granted_by, utcnow()),
        )

    def list_exemptions(self, profile_id) -> list:
        rows = self._query(
            "SELECT * FROM exemptions WHERE profile_id=? ORDER BY recorded_at",
            (profile_id,),
        )
        return [
            {"exemption_id": r["exemption_id"], "profile_id": r["profile_id"],
             "rule_ids": json.loads(r["rule_ids"]),
             "evidence_ids": json.loads(r["evidence_ids"]),
             "obligation_ids": json.loads(r["obligation_ids"]),
             "expires_at": r["expires_at"], "reason": r["reason"],
             "granted_by": r["granted_by"], "recorded_at": r["recorded_at"]}
            for r in rows
        ]

    def add_certificate(self, cert_id, profile_id, cert_type, issuer, expires_at):
        self._execute(
            "INSERT INTO certificates VALUES (?,?,?,?,?,?)",
            (cert_id, profile_id, cert_type, issuer, expires_at, utcnow()),
        )

    def list_certificates(self, profile_id) -> list:
        rows = self._query(
            "SELECT * FROM certificates WHERE profile_id=? ORDER BY recorded_at",
            (profile_id,),
        )
        return [
            {"cert_id": r["cert_id"], "profile_id": r["profile_id"],
             "cert_type": r["cert_type"], "issuer": r["issuer"],
             "expires_at": r["expires_at"], "recorded_at": r["recorded_at"]}
            for r in rows
        ]

    # ---- 发布（记录不可变，状态经事件流派生） ---------------------------

    def create_release(self, release_id, profile_id, profile_version, eval_id, decision):
        self._execute(
            "INSERT INTO releases VALUES (?,?,?,?,?,?)",
            (release_id, profile_id, profile_version, eval_id,
             json.dumps(decision, ensure_ascii=False), utcnow()),
        )

    def add_release_event(self, release_id, status, detail=None):
        self._execute(
            "INSERT INTO release_events (release_id, status, detail, recorded_at)"
            " VALUES (?,?,?,?)",
            (release_id, status,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None,
             utcnow()),
        )

    def get_release(self, release_id) -> dict | None:
        row = self._query_one("SELECT * FROM releases WHERE release_id=?", (release_id,))
        if row is None:
            return None
        return self._release_from_row(row)

    def list_releases(self) -> list:
        rows = self._query("SELECT * FROM releases ORDER BY recorded_at")
        return [self._release_from_row(r) for r in rows]

    def release_status(self, release_id) -> str | None:
        row = self._query_one(
            "SELECT status FROM release_events WHERE release_id=?"
            " ORDER BY seq DESC LIMIT 1",
            (release_id,),
        )
        return row["status"] if row else None

    def release_events(self, release_id) -> list:
        rows = self._query(
            "SELECT * FROM release_events WHERE release_id=? ORDER BY seq", (release_id,)
        )
        return [
            {"status": r["status"],
             "detail": json.loads(r["detail"]) if r["detail"] else None,
             "recorded_at": r["recorded_at"]}
            for r in rows
        ]

    def _release_from_row(self, row):
        return {
            "release_id": row["release_id"],
            "profile_id": row["profile_id"],
            "profile_version": row["profile_version"],
            "eval_id": row["eval_id"],
            "decision": json.loads(row["decision"]),
            "recorded_at": row["recorded_at"],
            "status": self.release_status(row["release_id"]),
        }

    # ---- 评估队列（持久化，支持重启恢复） -------------------------------

    def enqueue(self, job_id, kind, payload):
        now = utcnow()
        self._execute(
            "INSERT INTO queue VALUES (?,?,?,?,0,NULL,?,?)",
            (job_id, kind, json.dumps(payload, ensure_ascii=False), "PENDING", now, now),
        )

    def claim_job(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE status='PENDING' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE queue SET status='RUNNING', attempts=attempts+1, updated_at=?"
                " WHERE job_id=?",
                (utcnow(), row["job_id"]),
            )
            self._conn.commit()
            return {"job_id": row["job_id"], "kind": row["kind"],
                    "payload": json.loads(row["payload"]), "attempts": row["attempts"] + 1}

    def complete_job(self, job_id):
        self._execute(
            "UPDATE queue SET status='DONE', updated_at=? WHERE job_id=?",
            (utcnow(), job_id),
        )

    def fail_job(self, job_id, error, retry=True):
        status = "PENDING" if retry else "FAILED"
        self._execute(
            "UPDATE queue SET status=?, error=?, updated_at=? WHERE job_id=?",
            (status, error, utcnow(), job_id),
        )

    def recover_queue(self) -> int:
        """重启恢复：把中断的 RUNNING 任务退回 PENDING。返回恢复条数。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status='PENDING', updated_at=? WHERE status='RUNNING'",
                (utcnow(),),
            )
            self._conn.commit()
            return cur.rowcount

    def queue_counts(self) -> dict:
        rows = self._query("SELECT status, COUNT(*) AS n FROM queue GROUP BY status")
        counts = {"PENDING": 0, "RUNNING": 0, "DONE": 0, "FAILED": 0}
        for r in rows:
            counts[r["status"]] = r["n"]
        return counts

    # ---- 审计（只追加） -------------------------------------------------

    def audit(self, actor, action, entity, entity_id, payload=None, occurred_at=None):
        """occurred_at 为调用方事件时间，recorded_at 为服务端接收时间。"""
        self._execute(
            "INSERT INTO audit (actor, action, entity, entity_id, payload,"
            " occurred_at, recorded_at) VALUES (?,?,?,?,?,?,?)",
            (actor, action, entity, entity_id,
             json.dumps(payload, ensure_ascii=False) if payload is not None else None,
             occurred_at, utcnow()),
        )

    def list_audit(self, entity=None, entity_id=None) -> list:
        sql = "SELECT * FROM audit"
        params = ()
        if entity is not None:
            sql += " WHERE entity=?"
            params = (entity,)
            if entity_id is not None:
                sql += " AND entity_id=?"
                params = (entity, entity_id)
        sql += " ORDER BY seq"
        rows = self._query(sql, params)
        return [
            {"seq": r["seq"], "actor": r["actor"], "action": r["action"],
             "entity": r["entity"], "entity_id": r["entity_id"],
             "payload": json.loads(r["payload"]) if r["payload"] else None,
             "occurred_at": r["occurred_at"], "recorded_at": r["recorded_at"]}
            for r in rows
        ]
