"""合规判定核心领域逻辑。

包含发布档案、规则包、证据、外部证明、豁免、签署、评估队列与
流量闸门的协作。所有判定输入都锁定到具体档案修订与规则版本，
判定结果以哈希链只追加保存。
"""
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field

from .clock import now_iso, parse_iso, to_iso
from .errors import ConflictError, NotFoundError, ValidationError
from .rules import (applicable_jurisdictions, build_context,
                    jurisdiction_code, normalize_package, triggered_rules)

# 评估队列状态
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
MAX_ATTEMPTS = 3

# 判定状态
BLOCKED = "BLOCKED"
PENDING_SIGNOFF = "PENDING_SIGNOFF"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
REQUIRES_REASSESSMENT = "REQUIRES_REASSESSMENT"


def canonical_hash(obj):
    body = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class AssessmentRecord:
    release_id: str
    revision: int
    rule_version: str
    evaluated_at: str
    triggered_obligations: list
    evidence_gaps: list
    missing_roles: list
    decision: str
    rationale: list
    evidence_ids: list
    cert_snapshots: list
    input_refs: dict


# ---------------------------------------------------------------------
# 发布档案
# ---------------------------------------------------------------------

class ReleaseService:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    @staticmethod
    def validate_profile(profile):
        if not isinstance(profile, dict):
            raise ValidationError("档案必须是对象")
        model_version = profile.get("model_version")
        if not isinstance(model_version, str) or not model_version.strip():
            raise ValidationError("model_version 必填且为非空字符串")
        cats = profile.get("data_categories")
        if not isinstance(cats, list) or not cats or not all(
                isinstance(x, str) and x.strip() for x in cats):
            raise ValidationError("data_categories 必须为非空字符串列表")
        purpose = profile.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValidationError("purpose 必填且为非空字符串")
        deployment = profile.get("deployment_jurisdiction")
        if not isinstance(deployment, str) or not deployment.strip():
            raise ValidationError("deployment_jurisdiction 必填")
        vendors = profile.get("vendor_chain", [])
        if not isinstance(vendors, list):
            raise ValidationError("vendor_chain 必须为列表")
        norm_vendors = []
        for v in vendors:
            if isinstance(v, str) and ":" in v:
                name, juris = v.rsplit(":", 1)
                v = {"name": name.strip(), "jurisdiction": juris.strip()}
            if not isinstance(v, dict) or not v.get("name") or not v.get(
                    "jurisdiction"):
                raise ValidationError(
                    "vendor_chain 每项需含 name 与 jurisdiction")
            norm_vendors.append({
                "name": str(v["name"]).strip(),
                "jurisdiction": jurisdiction_code(v["jurisdiction"]),
            })
        subjects = profile.get("data_subject_jurisdictions", [])
        if not isinstance(subjects, list) or not all(
                isinstance(x, str) and x.strip() for x in subjects):
            raise ValidationError("data_subject_jurisdictions 必须为字符串列表")
        return {
            "model_version": model_version.strip(),
            "data_categories": [c.strip() for c in cats],
            "purpose": purpose.strip(),
            "deployment_jurisdiction": jurisdiction_code(deployment),
            "vendor_chain": norm_vendors,
            "data_subject_jurisdictions": [jurisdiction_code(x) for x in subjects],
            "involves_minors": bool(profile.get("involves_minors", False)),
            "high_risk_decision": bool(profile.get("high_risk_decision", False)),
        }

    def register(self, release_id, profile):
        if self.store.query_one("SELECT 1 FROM releases WHERE id=?",
                                (release_id,)):
            raise ConflictError(f"发布 {release_id} 已存在，请使用修订接口")
        profile = self.validate_profile(profile)
        ts = now_iso(self.clock)
        with self.store.lock:
            self.store.execute(
                "INSERT INTO releases (id, created_at, latest_revision) "
                "VALUES (?,?,0)", (release_id, ts))
            self._insert_revision(release_id, 1, profile, ts)
            self.store.execute(
                "UPDATE releases SET latest_revision=1 WHERE id=?",
                (release_id,))
        return self.get_release(release_id)

    def amend(self, release_id, profile):
        release = self._require(release_id)
        profile = self.validate_profile(profile)
        new_rev = release["latest_revision"] + 1
        ts = now_iso(self.clock)
        self._insert_revision(release_id, new_rev, profile, ts)
        self.store.execute(
            "UPDATE releases SET latest_revision=? WHERE id=?",
            (new_rev, release_id))
        return self.get_release(release_id)

    def _insert_revision(self, release_id, revision, profile, ts):
        self.store.execute(
            """INSERT INTO release_revisions
               (release_id, revision, created_at, model_version,
                data_categories, purpose, deployment_jurisdiction,
                vendor_chain, data_subject_jurisdictions,
                involves_minors, high_risk_decision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (release_id, revision, ts, profile["model_version"],
             json.dumps(profile["data_categories"]), profile["purpose"],
             profile["deployment_jurisdiction"],
             json.dumps(profile["vendor_chain"], ensure_ascii=False),
             json.dumps(profile["data_subject_jurisdictions"]),
             int(profile["involves_minors"]),
             int(profile["high_risk_decision"])))

    def _require(self, release_id):
        row = self.store.query_one("SELECT * FROM releases WHERE id=?",
                                   (release_id,))
        if not row:
            raise NotFoundError(f"发布 {release_id} 不存在")
        return row

    def get_profile(self, release_id, revision=None):
        self._require(release_id)
        if revision is None:
            revision = self.store.query_one(
                "SELECT latest_revision FROM releases WHERE id=?",
                (release_id,))["latest_revision"]
        row = self.store.query_one(
            "SELECT * FROM release_revisions WHERE release_id=? AND revision=?",
            (release_id, revision))
        if not row:
            raise NotFoundError(f"修订 {revision} 不存在")
        return self._row_to_profile(row), revision

    @staticmethod
    def _row_to_profile(row):
        return {
            "model_version": row["model_version"],
            "data_categories": json.loads(row["data_categories"]),
            "purpose": row["purpose"],
            "deployment_jurisdiction": row["deployment_jurisdiction"],
            "vendor_chain": json.loads(row["vendor_chain"]),
            "data_subject_jurisdictions": json.loads(
                row["data_subject_jurisdictions"]),
            "involves_minors": bool(row["involves_minors"]),
            "high_risk_decision": bool(row["high_risk_decision"]),
        }

    def get_release(self, release_id):
        row = self._require(release_id)
        revisions = self.store.query(
            "SELECT revision, created_at, model_version, purpose, "
            "deployment_jurisdiction FROM release_revisions "
            "WHERE release_id=? ORDER BY revision", (release_id,))
        profile, latest = self.get_profile(release_id)
        return {
            "id": release_id,
            "created_at": row["created_at"],
            "latest_revision": row["latest_revision"],
            "profile": profile,
            "revisions": [dict(r) for r in revisions],
            "profile_hash": canonical_hash(profile),
        }

    def list_releases(self):
        rows = self.store.query("SELECT id FROM releases ORDER BY id")
        return [self.get_release(r["id"]) for r in rows]


# ---------------------------------------------------------------------
# 规则包
# ---------------------------------------------------------------------

class RuleService:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    def publish(self, content):
        norm = normalize_package(content)
        version = norm["version"]
        existing = self.store.query_one(
            "SELECT content_hash FROM rule_packages WHERE version=?",
            (version,))
        digest = canonical_hash(norm)
        if existing:
            if existing["content_hash"] != digest:
                raise ConflictError(
                    f"规则版本 {version} 已存在且内容不同，版本不可变")
            return self.get_package(version), False
        ts = now_iso(self.clock)
        self.store.execute(
            "INSERT INTO rule_packages (version, created_at, published_at, "
            "content, content_hash) VALUES (?,?,?,?,?)",
            (version, ts, ts, json.dumps(norm, ensure_ascii=False), digest))
        return self.get_package(version), True

    def get_package(self, version):
        row = self.store.query_one(
            "SELECT * FROM rule_packages WHERE version=?", (version,))
        if not row:
            raise NotFoundError(f"规则版本 {version} 不存在")
        return {
            "version": row["version"],
            "published_at": row["published_at"],
            "content_hash": row["content_hash"],
            "content": json.loads(row["content"]),
        }

    def active_version(self):
        row = self.store.query_one(
            "SELECT version FROM rule_packages WHERE published_at IS NOT NULL "
            "ORDER BY published_at DESC, rowid DESC LIMIT 1")
        return row["version"] if row else None

    def active_package(self):
        version = self.active_version()
        return self.get_package(version) if version else None

    def list_packages(self):
        rows = self.store.query(
            "SELECT version, published_at, content_hash FROM rule_packages "
            "ORDER BY published_at")
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# 证据 / 外部证明 / 豁免 / 审查者
# ---------------------------------------------------------------------

class EvidenceService:
    def __init__(self, store, clock, releases, rules, queue):
        self.store = store
        self.clock = clock
        self.releases = releases
        self.rules = rules
        self.queue = queue

    def add(self, release_id, evidence_id, kind, description, valid_until=None):
        self.releases._require(release_id)
        if not kind or not str(kind).strip():
            raise ValidationError("evidence kind 必填")
        if self.store.query_one("SELECT 1 FROM evidence WHERE id=?",
                                (evidence_id,)):
            raise ConflictError(f"证据 {evidence_id} 已存在")
        if valid_until is not None:
            parse_iso(valid_until)  # 校验格式
        ts = now_iso(self.clock)
        self.store.execute(
            "INSERT INTO evidence (id, release_id, created_at, kind, "
            "description, valid_until) VALUES (?,?,?,?,?,?)",
            (evidence_id, release_id, ts, kind.strip(), description or "",
             valid_until))
        # 证据补充后自动安排复评
        self.queue.enqueue_for_current(release_id, "evidence_provided",
                                        self.rules)
        return self.get(evidence_id)

    def get(self, evidence_id):
        row = self.store.query_one("SELECT * FROM evidence WHERE id=?",
                                   (evidence_id,))
        if not row:
            raise NotFoundError(f"证据 {evidence_id} 不存在")
        return dict(row)

    def list_for_release(self, release_id):
        return [dict(r) for r in self.store.query(
            "SELECT * FROM evidence WHERE release_id=? ORDER BY id",
            (release_id,))]

    def valid_index(self, release_id, at):
        """按 kind 汇总当前有效证据。"""
        index = {}
        for row in self.store.query(
                "SELECT * FROM evidence WHERE release_id=?", (release_id,)):
            if row["valid_until"] and parse_iso(row["valid_until"]) <= at:
                continue
            index.setdefault(row["kind"], []).append(dict(row))
        return index


class CertificateService:
    def __init__(self, store, clock, releases, rules, queue):
        self.store = store
        self.clock = clock
        self.releases = releases
        self.rules = rules
        self.queue = queue

    def register(self, cert_id, issuer, scope, valid_from, valid_until,
                 release_id=None, jurisdiction=None):
        if not issuer or not scope:
            raise ValidationError("issuer 与 scope 必填")
        vf, vu = parse_iso(valid_from), parse_iso(valid_until)
        if vu <= vf:
            raise ValidationError("valid_until 必须晚于 valid_from")
        if self.store.query_one("SELECT 1 FROM certificates WHERE id=?",
                                (cert_id,)):
            raise ConflictError(f"证明 {cert_id} 已存在")
        ts = now_iso(self.clock)
        self.store.execute(
            "INSERT INTO certificates (id, release_id, jurisdiction, "
            "created_at, issuer, scope, valid_from, valid_until) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cert_id, release_id,
             jurisdiction_code(jurisdiction) if jurisdiction else None,
             ts, issuer, scope, to_iso(vf), to_iso(vu)))
        self._enqueue_all("certificate_registered")
        return self.get(cert_id)

    def _enqueue_all(self, reason):
        """证明状态可能影响任意发布的证书类义务，全部安排复评。"""
        for release in self.releases.list_releases():
            self.queue.enqueue_for_current(
                release["id"], reason, self.rules)

    def get(self, cert_id):
        row = self.store.query_one("SELECT * FROM certificates WHERE id=?",
                                   (cert_id,))
        if not row:
            raise NotFoundError(f"证明 {cert_id} 不存在")
        return self._to_dict(row)

    def list_all(self):
        return [self._to_dict(r) for r in
                self.store.query("SELECT * FROM certificates ORDER BY id")]

    @staticmethod
    def _to_dict(row):
        d = dict(row)
        d["revoked"] = bool(row["revoked"])
        return d

    def revoke(self, cert_id):
        self.get(cert_id)
        self.store.execute(
            "UPDATE certificates SET revoked=1 WHERE id=?", (cert_id,))
        self._enqueue_all("certificate_revoked")
        return self.get(cert_id)

    def find_valid(self, release_id, jurisdiction, scope, at):
        """查找可用于某义务的有效外部证明，返回(证明, 状态)。"""
        rows = self.store.query(
            "SELECT * FROM certificates WHERE scope=? AND revoked=0", (scope,))
        fallback = None
        for row in rows:
            cert = self._to_dict(row)
            if not (parse_iso(cert["valid_from"]) <= at <
                    parse_iso(cert["valid_until"])):
                continue
            if cert["release_id"] == release_id:
                return cert, "valid"
            if cert["release_id"] is None and (
                    not cert["jurisdiction"]
                    or cert["jurisdiction"] == jurisdiction_code(jurisdiction)):
                fallback = cert
        if fallback is not None:
            return fallback, "valid"
        # 给出最近的失效原因
        reason_row = self.store.query_one(
            "SELECT * FROM certificates WHERE scope=? ORDER BY rowid DESC LIMIT 1",
            (scope,))
        return None, self._invalid_reason(reason_row, at)

    @staticmethod
    def _invalid_reason(row, at):
        if row is None:
            return "missing"
        if row["revoked"]:
            return "revoked"
        if parse_iso(row["valid_until"]) <= at:
            return "expired"
        if at < parse_iso(row["valid_from"]):
            return "not_yet_valid"
        return "scope_mismatch"


class WaiverService:
    def __init__(self, store, clock, releases, rules, queue):
        self.store = store
        self.clock = clock
        self.releases = releases
        self.rules = rules
        self.queue = queue

    def grant(self, waiver_id, release_id, jurisdiction, obligation_id,
              scope_note, valid_from, valid_until):
        vf, vu = parse_iso(valid_from), parse_iso(valid_until)
        if vu <= vf:
            raise ValidationError("valid_until 必须晚于 valid_from")
        if not scope_note or not scope_note.strip():
            raise ValidationError("豁免必须写明 scope_note（范围与理由）")
        if self.store.query_one("SELECT 1 FROM waivers WHERE id=?",
                                (waiver_id,)):
            raise ConflictError(f"豁免 {waiver_id} 已存在")
        ts = now_iso(self.clock)
        self.store.execute(
            "INSERT INTO waivers (id, release_id, jurisdiction, obligation_id, "
            "scope_note, granted_at, valid_from, valid_until) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (waiver_id, release_id, jurisdiction_code(jurisdiction),
             obligation_id, scope_note.strip(), ts, to_iso(vf), to_iso(vu)))
        self.queue.enqueue_for_current(release_id, "waiver_granted",
                                       self.rules)
        return self.get(waiver_id)

    def get(self, waiver_id):
        row = self.store.query_one("SELECT * FROM waivers WHERE id=?",
                                   (waiver_id,))
        if not row:
            raise NotFoundError(f"豁免 {waiver_id} 不存在")
        return self._to_dict(row)

    def list_all(self):
        return [self._to_dict(r) for r in
                self.store.query("SELECT * FROM waivers ORDER BY granted_at")]

    @staticmethod
    def _to_dict(row):
        d = dict(row)
        d["revoked"] = bool(row["revoked"])
        return d

    def revoke(self, waiver_id):
        waiver = self.get(waiver_id)
        self.store.execute(
            "UPDATE waivers SET revoked=1 WHERE id=?", (waiver_id,))
        self.queue.enqueue_for_current(
            waiver["release_id"], "waiver_revoked", self.rules)
        return self.get(waiver_id)

    def active_for(self, release_id, jurisdiction, obligation_id, at):
        row = self.store.query_one(
            "SELECT * FROM waivers WHERE release_id=? AND jurisdiction=? "
            "AND obligation_id=? AND revoked=0 ORDER BY rowid DESC LIMIT 1",
            (release_id, jurisdiction_code(jurisdiction), obligation_id))
        if row is None:
            return None
        if parse_iso(row["valid_from"]) <= at < parse_iso(row["valid_until"]):
            return self._to_dict(row)
        return None


class ReviewerService:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    def upsert(self, reviewer_id, affiliations):
        if not isinstance(affiliations, list) or not all(
                isinstance(x, str) and x.strip() for x in affiliations):
            raise ValidationError("affiliations 必须为字符串列表")
        ts = now_iso(self.clock)
        self.store.execute(
            "INSERT INTO reviewers (reviewer_id, affiliations) VALUES (?,?) "
            "ON CONFLICT(reviewer_id) DO UPDATE SET affiliations=excluded.affiliations",
            (reviewer_id, json.dumps([a.strip() for a in affiliations])))
        return self.get(reviewer_id)

    def get(self, reviewer_id):
        row = self.store.query_one("SELECT * FROM reviewers WHERE reviewer_id=?",
                                   (reviewer_id,))
        if not row:
            raise NotFoundError(f"审查者 {reviewer_id} 未登记")
        return {"reviewer_id": reviewer_id,
                "affiliations": json.loads(row["affiliations"])}


# ---------------------------------------------------------------------
# 评估队列与判定引擎
# ---------------------------------------------------------------------

class EvaluationQueue:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock

    def enqueue(self, release_id, revision, rule_version, reason):
        """同一三元组已有待处理任务时不重复入队。"""
        existing = self.store.query_one(
            "SELECT id FROM eval_queue WHERE release_id=? AND revision=? "
            "AND rule_version=? AND status IN (?,?)",
            (release_id, revision, rule_version, PENDING, RUNNING))
        if existing:
            return existing["id"]
        ts = now_iso(self.clock)
        cur = self.store.execute(
            "INSERT INTO eval_queue (release_id, revision, rule_version, "
            "reason, enqueued_at, status) VALUES (?,?,?,?,?,?)",
            (release_id, revision, rule_version, reason, ts, PENDING))
        return cur.lastrowid

    def enqueue_for_current(self, release_id, reason, rules=None):
        """按发布最新修订与当前生效规则入队。"""
        row = self.store.query_one(
            "SELECT latest_revision FROM releases WHERE id=?", (release_id,))
        if not row or rules is None:
            return None
        version = rules.active_version()
        if not version:
            return None
        return self.enqueue(release_id, row["latest_revision"], version, reason)

    def recover_stale(self):
        """重启恢复：崩溃时停留 running 的任务回到 pending。"""
        self.store.execute(
            f"UPDATE eval_queue SET status='{PENDING}', leased_at=NULL, "
            f"last_error='recovered_after_restart' WHERE status='{RUNNING}'")

    def lease(self):
        ts = now_iso(self.clock)
        with self.store.lock:
            row = self.store.query_one(
                "SELECT * FROM eval_queue WHERE status=? ORDER BY id LIMIT 1",
                (PENDING,))
            if row is None:
                return None
            self.store.execute(
                "UPDATE eval_queue SET status=?, leased_at=?, attempts=attempts+1 "
                "WHERE id=?", (RUNNING, ts, row["id"]))
        return self.store.query_one(
            "SELECT * FROM eval_queue WHERE id=?", (row["id"],))

    def complete(self, job_id, assessment_id):
        self.store.execute(
            "UPDATE eval_queue SET status=?, assessment_id=?, last_error=NULL "
            "WHERE id=?", (DONE, assessment_id, job_id))

    def fail(self, job_id, error):
        row = self.store.query_one(
            "SELECT attempts FROM eval_queue WHERE id=?", (job_id,))
        if row and row["attempts"] >= MAX_ATTEMPTS:
            status = FAILED
        else:
            status = PENDING
        self.store.execute(
            "UPDATE eval_queue SET status=?, last_error=? WHERE id=?",
            (status, error, job_id))

    def list_jobs(self):
        return [dict(r) for r in self.store.query(
            "SELECT * FROM eval_queue ORDER BY id")]

    def pending_for(self, release_id, revision, rule_version):
        return self.store.query_one(
            "SELECT id, reason, status FROM eval_queue WHERE release_id=? "
            "AND revision=? AND rule_version=? AND status IN (?,?)",
            (release_id, revision, rule_version, PENDING, RUNNING))


class Evaluator:
    """对锁定的档案修订 + 规则版本执行一次判定并追加哈希链记录。"""

    def __init__(self, store, clock, releases, rules, evidence, certificates,
                 waivers):
        self.store = store
        self.clock = clock
        self.releases = releases
        self.rules = rules
        self.evidence = evidence
        self.certificates = certificates
        self.waivers = waivers

    def evaluate(self, release_id, revision, rule_version):
        at = self.clock.now()
        at_iso = to_iso(at)
        profile, _ = self.releases.get_profile(release_id, revision)
        package = self.rules.get_package(rule_version)["content"]
        hits = triggered_rules(package, profile)
        evidence_index = self.evidence.valid_index(release_id, at)
        ctx = build_context(profile)

        obligations = []
        gaps = []
        cert_snapshots = []
        used_evidence = []
        match_notes = []
        required_roles = set()

        for rule in hits:
            oid = rule["id"]
            juris = rule["jurisdiction"]
            waiver = self.waivers.active_for(release_id, juris, oid, at)
            obligation = {
                "obligation_id": oid,
                "jurisdiction": juris,
                "title": rule["title"],
                "detail": rule["detail"],
                "risk_level": rule["risk_level"],
                "required_roles": rule["required_roles"],
                "required_evidence": rule["evidence"],
                "requires_certificate": rule["requires_certificate"],
            }
            matched = [key for key in rule["when"]
                       if _condition_held(rule["when"][key], ctx, key)]
            match_notes.append({
                "rule_id": oid,
                "jurisdiction": juris,
                "matched_conditions": matched,
            })
            if waiver:
                obligation["status"] = "WAIVED"
                obligation["waiver_id"] = waiver["id"]
                obligation["waiver_valid_until"] = waiver["valid_until"]
            else:
                obligation["status"] = "OUTSTANDING"
                required_roles.update(rule["required_roles"])
                for kind in rule["evidence"]:
                    items = evidence_index.get(kind, [])
                    if not items:
                        gaps.append({
                            "obligation_id": oid,
                            "jurisdiction": juris,
                            "type": "evidence",
                            "item": kind,
                            "reason": "missing",
                        })
                    else:
                        for item in items:
                            if item["id"] not in used_evidence:
                                used_evidence.append(item["id"])
                if rule["requires_certificate"]:
                    scope = rule["requires_certificate"]["scope"]
                    cert, cert_status = self.certificates.find_valid(
                        release_id, juris, scope, at)
                    if cert is None:
                        gaps.append({
                            "obligation_id": oid,
                            "jurisdiction": juris,
                            "type": "certificate",
                            "item": scope,
                            "reason": cert_status,
                        })
                    else:
                        cert_snapshots.append({
                            "certificate_id": cert["id"],
                            "issuer": cert["issuer"],
                            "scope": cert["scope"],
                            "valid_until": cert["valid_until"],
                            "status_at_evaluation": "valid",
                        })
            obligations.append(obligation)

        missing_roles = sorted(required_roles)
        if gaps:
            decision = BLOCKED
        elif missing_roles:
            decision = PENDING_SIGNOFF
        else:
            decision = APPROVED

        profile_hash = canonical_hash(profile)
        package_hash = self.rules.get_package(rule_version)["content_hash"]
        rule_signature = rule_set_signature(hits)
        rationale = [
            {"step": "jurisdiction_scope",
             "applicable_jurisdictions": sorted(applicable_jurisdictions(profile))},
            {"step": "condition_matches", "rules": match_notes},
            {"step": "evidence_resolution",
             "available_kinds": sorted(evidence_index)},
            {"step": "waiver_resolution",
             "waived": [o["obligation_id"] for o in obligations
                        if o["status"] == "WAIVED"]},
        ]
        input_refs = {
            "release_id": release_id,
            "revision": revision,
            "profile_hash": profile_hash,
            "rule_version": rule_version,
            "rule_package_hash": package_hash,
            "rule_signature": rule_signature,
            "evaluated_at": at_iso,
        }
        record = AssessmentRecord(
            release_id=release_id, revision=revision,
            rule_version=rule_version, evaluated_at=at_iso,
            triggered_obligations=obligations, evidence_gaps=gaps,
            missing_roles=missing_roles, decision=decision,
            rationale=rationale, evidence_ids=sorted(set(used_evidence)),
            cert_snapshots=cert_snapshots, input_refs=input_refs)
        assessment_id = self.store.append_assessment(record)
        row = self.store.query_one(
            "SELECT entry_hash FROM assessments WHERE id=?", (assessment_id,))
        return assessment_id, row["entry_hash"]


def rule_set_signature(hits):
    """命中规则的义务指纹：任一条款义务内容变化都会改变指纹。"""
    view = sorted([{
        "id": r["id"],
        "jurisdiction": r["jurisdiction"],
        "risk_level": r["risk_level"],
        "evidence": sorted(r["evidence"]),
        "required_roles": sorted(r["required_roles"]),
        "certificate_scope": r["requires_certificate"]["scope"]
        if r["requires_certificate"] else None,
    } for r in hits], key=lambda x: (x["jurisdiction"], x["id"]))
    return canonical_hash(view)


def _condition_held(value, ctx, key):
    if key == "data_categories_any":
        return bool(ctx["data_categories"] & set(value))
    if key == "purposes_any":
        return bool(ctx["purposes"] & set(value))
    if key == "deployment_in":
        return ctx["deployment"] in {jurisdiction_code(x) for x in value}
    if key == "subject_jurisdictions_any":
        return bool(ctx["subject_jurisdictions"] &
                    {jurisdiction_code(x) for x in value})
    if key == "vendor_jurisdictions_any":
        return bool(ctx["vendor_jurisdictions"] &
                    {jurisdiction_code(x) for x in value})
    return bool(value)


class EvaluationWorker:
    """后台守护线程：持久队列在重启后继续被消费。"""

    def __init__(self, queue, evaluator, interval=0.1):
        self.queue = queue
        self.evaluator = evaluator
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self.queue.recover_stale()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="eval-worker")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.is_set():
            job = self.queue.lease()
            if job is None:
                self._stop.wait(self.interval)
                continue
            try:
                assessment_id, _ = self.evaluator.evaluate(
                    job["release_id"], job["revision"], job["rule_version"])
                self.queue.complete(job["id"], assessment_id)
            except Exception as exc:  # 毒消息不拖垮循环
                self.queue.fail(job["id"], str(exc))
                self._stop.wait(self.interval)

    def run_pending(self, timeout=5.0):
        """供测试与同步调用：阻塞至队列清空。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.queue.lease()
            if job is None:
                return
            try:
                assessment_id, _ = self.evaluator.evaluate(
                    job["release_id"], job["revision"], job["rule_version"])
                self.queue.complete(job["id"], assessment_id)
            except Exception as exc:
                self.queue.fail(job["id"], str(exc))


# ---------------------------------------------------------------------
# 签署：利益冲突隔离、角色不可跳过、并发安全
# ---------------------------------------------------------------------

class SignoffService:
    def __init__(self, store, clock, releases):
        self.store = store
        self.clock = clock
        self.releases = releases

    def sign(self, release_id, assessment_id, role, reviewer_id, decision,
             comment=None):
        if decision not in ("APPROVE", "REJECT"):
            raise ValidationError("decision 必须为 APPROVE 或 REJECT")
        release = self.releases._require(release_id)
        assessment = self.store.query_one(
            "SELECT * FROM assessments WHERE id=? AND release_id=?",
            (assessment_id, release_id))
        if not assessment:
            raise NotFoundError("判定记录不存在或不属于该发布")
        latest = self.store.query_one(
            "SELECT id FROM assessments WHERE release_id=? ORDER BY id DESC LIMIT 1",
            (release_id,))
        if latest["id"] != assessment_id:
            raise ConflictError("档案或规则已更新，只能对最新判定记录签署")
        required = set(json.loads(assessment["missing_roles"]))
        if not required:
            raise ConflictError("该判定无需角色签署")
        if role not in required:
            raise ValidationError(
                f"角色 {role} 不是该判定的必要角色；必要角色: {sorted(required)}")

        profile, _ = self.releases.get_profile(release_id)
        reviewer = self.store.query_one(
            "SELECT * FROM reviewers WHERE reviewer_id=?", (reviewer_id,))
        if not reviewer:
            raise ValidationError(f"审查者 {reviewer_id} 未登记利益关系")
        affiliations = {a.lower() for a in json.loads(reviewer["affiliations"])}
        vendor_names = {v["name"].lower() for v in profile["vendor_chain"]}
        overlap = affiliations & vendor_names
        if overlap:
            raise ConflictError(
                f"审查者与供应商存在利益关联，必须隔离: {sorted(overlap)}")

        ts = now_iso(self.clock)
        with self.store.lock:
            rows = self.store.query(
                "SELECT * FROM signoffs WHERE release_id=? AND assessment_id=?",
                (release_id, assessment_id))
            if any(r["role"] == role for r in rows):
                raise ConflictError(f"角色 {role} 已完成签署，不可重复签署")
            if any(r["reviewer_id"] == reviewer_id for r in rows):
                raise ConflictError(
                    "同一审查者不得兼任该发布的其他必要角色（职责隔离）")
            cur = self.store.execute(
                "INSERT INTO signoffs (release_id, assessment_id, role, "
                "reviewer_id, decision, comment, signed_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (release_id, assessment_id, role, reviewer_id, decision,
                 comment, ts))
            signoff_id = cur.lastrowid
        return self.get(signoff_id)

    def get(self, signoff_id):
        row = self.store.query_one("SELECT * FROM signoffs WHERE id=?",
                                   (signoff_id,))
        if not row:
            raise NotFoundError("签署记录不存在")
        return dict(row)

    def list_for(self, release_id, assessment_id):
        return [dict(r) for r in self.store.query(
            "SELECT * FROM signoffs WHERE release_id=? AND assessment_id=? "
            "ORDER BY id", (release_id, assessment_id))]


# ---------------------------------------------------------------------
# 流量闸门 / 发布判定接口
# ---------------------------------------------------------------------

class GateService:
    def __init__(self, store, clock, releases, rules, queue, evaluator):
        self.store = store
        self.clock = clock
        self.releases = releases
        self.rules = rules
        self.queue = queue
        self.evaluator = evaluator

    def latest_assessment(self, release_id):
        row = self.store.query_one(
            "SELECT * FROM assessments WHERE release_id=? ORDER BY id DESC LIMIT 1",
            (release_id,))
        return row

    def decision(self, release_id):
        release = self.releases._require(release_id)
        at = self.clock.now()
        latest_rev = release["latest_revision"]
        active_version = self.rules.active_version()
        profile, _ = self.releases.get_profile(release_id)
        row = self.latest_assessment(release_id)

        pending_job = None
        if active_version:
            pending_job = self.queue.pending_for(
                release_id, latest_rev, active_version)

        path = [{
            "step": "profile_locked",
            "release_id": release_id,
            "revision": latest_rev,
            "model_version": profile["model_version"],
            "profile_hash": canonical_hash(profile),
        }]

        if row is None:
            if active_version and not pending_job:
                self.queue.enqueue(release_id, latest_rev, active_version,
                                   "initial_evaluation")
            return {
                "release_id": release_id,
                "revision": latest_rev,
                "rule_version": active_version,
                "decision": REQUIRES_REASSESSMENT,
                "traffic_allowed": False,
                "obligations": [],
                "evidence_gaps": [],
                "missing_roles": [],
                "signoffs": [],
                "reevaluation_pending": True,
                "reason": "尚无判定记录，已安排首次评估",
                "decision_path": path,
            }

        assessment = dict(row)
        obligations = json.loads(row["triggered_obligations"])
        gaps = json.loads(row["evidence_gaps"])
        required_roles = json.loads(row["missing_roles"])
        signoffs = [dict(r) for r in self.store.query(
            "SELECT * FROM signoffs WHERE assessment_id=? ORDER BY id",
            (row["id"],))]

        path.append({
            "step": "rules_locked",
            "rule_version": row["rule_version"],
            "rule_package_hash": json.loads(row["input_refs"])["rule_package_hash"],
        })
        for note in json.loads(row["rationale"]):
            path.append(note)
        path.append({
            "step": "ledger_anchor",
            "assessment_id": row["id"],
            "prev_hash": row["prev_hash"],
            "entry_hash": row["entry_hash"],
            "chain_tip": self.store.last_assessment_hash(),
        })

        # 当前档案/规则与已锁定判定是否实质不一致：
        # 修订不同，或适用义务指纹变化（版本号变化但义务不变不算过期）
        current_signature = None
        if active_version:
            current_hits = triggered_rules(
                self.rules.get_package(active_version)["content"], profile)
            current_signature = rule_set_signature(current_hits)
        old_signature = json.loads(row["input_refs"]).get("rule_signature")
        stale = (row["revision"] != latest_rev or
                 old_signature != current_signature)
        drift_gaps = []
        if not stale:
            drift_gaps = self._live_drift(release_id, obligations, at, row,
                                          active_version, gaps)

        # 签署状态
        approved_roles = {s["role"] for s in signoffs if s["decision"] == "APPROVE"}
        rejected = [s for s in signoffs if s["decision"] == "REJECT"]
        missing_now = sorted(set(required_roles) - approved_roles)
        path.append({
            "step": "signoff_resolution",
            "required_roles": required_roles,
            "approved_roles": sorted(approved_roles),
            "missing_roles": missing_now,
            "rejected": [{"role": s["role"], "reviewer_id": s["reviewer_id"]}
                         for s in rejected],
        })
        path.append({
            "step": "live_gate_checks",
            "checked_at": to_iso(at),
            "certificates": json.loads(row["cert_snapshots"]),
            "drift_detected": drift_gaps,
        })

        reevaluation_pending = bool(pending_job) or stale
        if stale and not pending_job and active_version:
            self.queue.enqueue(release_id, latest_rev, active_version,
                               "stale_assessment")
            reevaluation_pending = True
        if reevaluation_pending:
            decision = REQUIRES_REASSESSMENT
            reason = pending_job["reason"] if pending_job else (
                "档案已修订" if row["revision"] != latest_rev
                else "规则已更新")
        elif drift_gaps:
            decision = BLOCKED
            reason = "外部证明或豁免在判定后失效，已阻断新流量并安排复评"
            self.queue.enqueue(
                release_id, row["revision"], row["rule_version"],
                "gate_drift_detected")
        elif rejected:
            decision = REJECTED
            reason = f"审查者 {rejected[0]['reviewer_id']} 以角色 " \
                     f"{rejected[0]['role']} 驳回"
        elif gaps:
            decision = BLOCKED
            reason = "存在证据/证明缺口"
        elif missing_now:
            decision = PENDING_SIGNOFF
            reason = "等待必要角色签署"
        else:
            decision = APPROVED
            reason = "义务全部满足，必要角色全部批准"

        all_gaps = gaps + drift_gaps
        return {
            "release_id": release_id,
            "revision": row["revision"],
            "latest_revision": latest_rev,
            "rule_version": row["rule_version"],
            "active_rule_version": active_version,
            "assessment_id": row["id"],
            "assessment_hash": row["entry_hash"],
            "decision": decision,
            "traffic_allowed": decision == APPROVED,
            "reason": reason,
            "obligations": obligations,
            "evidence_gaps": all_gaps,
            "missing_roles": missing_now,
            "signoffs": signoffs,
            "reevaluation_pending": reevaluation_pending,
            "evaluated_at": row["evaluated_at"],
            "checked_at": to_iso(at),
            "decision_path": path,
        }

    def _live_drift(self, release_id, obligations, at, assessment_row,
                    active_version, original_gaps):
        """实时复核判定时有效的证明与豁免此后是否失效。

        仅检查判定当时已满足的项；判定时即缺失的缺口已在原始缺口中，
        不重复报告。
        """
        drift = []
        already_missing_certs = {
            (g.get("obligation_id"), g.get("item"))
            for g in original_gaps if g.get("type") == "certificate"}
        for ob in obligations:
            oid, juris = ob["obligation_id"], ob["jurisdiction"]
            if ob["status"] == "WAIVED":
                waiver_id = ob["waiver_id"]
                row = self.store.query_one(
                    "SELECT * FROM waivers WHERE id=?", (waiver_id,))
                if row is None or row["revoked"] or not (
                        parse_iso(row["valid_from"]) <= at <
                        parse_iso(row["valid_until"])):
                    drift.append({
                        "obligation_id": oid, "jurisdiction": juris,
                        "type": "waiver", "item": waiver_id,
                        "reason": "expired" if row and not row["revoked"]
                        else "revoked"})
                continue
            cert_req = ob.get("requires_certificate")
            if not cert_req:
                continue
            scope = cert_req["scope"]
            if (oid, scope) in already_missing_certs:
                continue
            cert, status = self.evaluator.certificates.find_valid(
                release_id, juris, scope, at)
            if cert is None:
                drift.append({
                    "obligation_id": oid, "jurisdiction": juris,
                    "type": "certificate", "item": scope,
                    "reason": f"invalid_after_approval:{status}"})
        return drift


# ---------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------

class App:
    def __init__(self, store, clock, worker_interval=0.1):
        self.store = store
        self.clock = clock
        self.releases = ReleaseService(store, clock)
        self.rules = RuleService(store, clock)
        self.queue = EvaluationQueue(store, clock)
        self.evidence = EvidenceService(store, clock, self.releases,
                                        self.rules, self.queue)
        self.certificates = CertificateService(
            store, clock, self.releases, self.rules, self.queue)
        self.waivers = WaiverService(
            store, clock, self.releases, self.rules, self.queue)
        # 启动即恢复：崩溃时停留 running 的任务回到 pending 继续评估
        self.queue.recover_stale()
        self.reviewers = ReviewerService(store, clock)
        self.evaluator = Evaluator(
            store, clock, self.releases, self.rules, self.evidence,
            self.certificates, self.waivers)
        self.signoffs = SignoffService(store, clock, self.releases)
        self.gate = GateService(store, clock, self.releases, self.rules,
                                self.queue, self.evaluator)
        self.worker = EvaluationWorker(self.queue, self.evaluator,
                                       interval=worker_interval)

    def enqueue_affected_by_rules(self, new_package):
        """规则发布后自动定位需要重新评估的发布。

        仅当新规则在发布最新修订上命中的规则集合与最近判定不同才入队。
        """
        affected = []
        for release in self.releases.list_releases():
            rid = release["id"]
            rev = release["latest_revision"]
            profile, _ = self.releases.get_profile(rid, rev)
            new_hits = triggered_rules(new_package["content"], profile)
            new_signature = rule_set_signature(new_hits)
            latest = self.gate.latest_assessment(rid)
            old_signature = None
            if latest:
                old_signature = json.loads(
                    latest["input_refs"]).get("rule_signature")
            if old_signature != new_signature:
                self.queue.enqueue(
                    rid, rev, new_package["version"],
                    f"rule_update:{new_package['version']}")
                affected.append(rid)
        return affected

    def enqueue_after_register(self, release_id, reason):
        return self.queue.enqueue_for_current(release_id, reason, self.rules)

    def start_worker(self):
        self.worker.start()

    def stop_worker(self):
        self.worker.stop()
