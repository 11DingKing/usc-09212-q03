"""业务编排层：档案、规则、评估、签署、豁免、证书、发布与流量门禁。"""
from __future__ import annotations

from . import engine
from .store import Store, new_id, utcnow

DEFAULT_RULESET = "asean-cn-baseline"

# 发布状态：ACTIVE 放行；DENIED 拒绝；REASSESSMENT_PENDING 规则更新待复评
# （旧批准仍然有效，不被篡改）；SUSPENDED 复评未通过，暂停。
RELEASABLE_STATUSES = ("ACTIVE", "REASSESSMENT_PENDING")


class DomainError(Exception):
    """业务错误，code 用于 HTTP 映射。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def not_found(msg):
    return DomainError("not_found", msg, 404)


def conflict(msg):
    return DomainError("conflict", msg, 409)


def validation(msg):
    return DomainError("validation", msg, 400)


PROFILE_REQUIRED_FIELDS = (
    "model_version", "owner_org", "data_categories", "purposes",
    "data_origin_country", "deployment_country", "vendor_chain",
)


class ComplianceService:
    def __init__(self, store: Store, now_fn=utcnow):
        self.store = store
        self.now_fn = now_fn

    # ---- 档案 ----------------------------------------------------------

    def put_profile(self, profile_id: str, payload: dict, actor=None) -> dict:
        missing = [f for f in PROFILE_REQUIRED_FIELDS if f not in payload]
        if missing:
            raise validation(f"档案缺少必填字段: {missing}")
        version = self.store.append_profile(profile_id, payload)
        self.store.audit(actor, "profile.version_created", "profile", profile_id,
                         {"version": version, "payload": payload})
        return {"profile_id": profile_id, "version": version}

    def get_profile(self, profile_id: str, version: int | None = None) -> dict:
        profile = self.store.get_profile(profile_id, version)
        if profile is None:
            raise not_found(f"档案不存在: {profile_id} v{version or 'latest'}")
        return profile

    # ---- 规则集与影响分析 ----------------------------------------------

    def register_ruleset(self, ruleset_id: str, rules: list, actor=None) -> dict:
        if not isinstance(rules, list) or not rules:
            raise validation("规则集不能为空")
        ids = [r.get("id") for r in rules]
        if any(not i for i in ids) or len(set(ids)) != len(ids):
            raise validation("规则 id 缺失或重复")
        previous = self.store.get_ruleset(ruleset_id)
        version = self.store.append_ruleset(ruleset_id, rules)
        self.store.audit(actor, "ruleset.version_created", "ruleset", ruleset_id,
                         {"version": version, "rule_count": len(rules)})
        impact = self._impact_analysis(ruleset_id, previous, rules, actor)
        return {"ruleset_id": ruleset_id, "version": version, **impact}

    def _impact_analysis(self, ruleset_id, previous, new_rules, actor) -> dict:
        """定位受规则变更影响的发布：追加待复评事件并入队评估，
        不修改任何既有批准记录。"""
        old_by_id = {r["id"]: r for r in (previous["rules"] if previous else [])}
        new_by_id = {r["id"]: r for r in new_rules}
        changed = {rid for rid in new_by_id
                   if rid not in old_by_id or old_by_id[rid] != new_by_id[rid]}
        changed |= set(old_by_id) - set(new_by_id)

        affected = []
        if not changed:
            return {"changed_rules": [], "affected_releases": []}
        for release in self.store.list_releases():
            if release["status"] not in RELEASABLE_STATUSES:
                continue
            profile = self.store.get_profile(
                release["profile_id"], release["profile_version"])["payload"]
            hit = self._matched_rule_ids(profile, new_by_id, changed)
            hit |= self._matched_rule_ids(profile, old_by_id, changed)
            if not hit:
                continue
            eval_id = self.request_evaluation(
                release["profile_id"], release["profile_version"],
                ruleset_id=ruleset_id, release_id=release["release_id"], actor=actor)
            self.store.add_release_event(
                release["release_id"], "REASSESSMENT_PENDING",
                {"changed_rules": sorted(hit), "eval_id": eval_id})
            self.store.audit(actor, "release.reassessment_pending", "release",
                             release["release_id"],
                             {"changed_rules": sorted(hit), "eval_id": eval_id})
            affected.append({"release_id": release["release_id"],
                             "eval_id": eval_id, "matched_rules": sorted(hit)})
        return {"changed_rules": sorted(changed), "affected_releases": affected}

    @staticmethod
    def _matched_rule_ids(profile, rules_by_id, candidate_ids) -> set:
        hit = set()
        for rid in candidate_ids:
            rule = rules_by_id.get(rid)
            if rule is None or not engine.rule_applicable(rule, profile):
                continue
            matched, _ = engine.match_conditions(rule.get("when", {}), profile)
            if matched:
                hit.add(rid)
        return hit

    # ---- 评估（经持久队列异步执行） -------------------------------------

    def request_evaluation(self, profile_id, profile_version=None, ruleset_id=None,
                           release_id=None, actor=None) -> str:
        profile = self.get_profile(profile_id, profile_version)
        ruleset_id = ruleset_id or DEFAULT_RULESET
        ruleset = self.store.get_ruleset(ruleset_id)
        if ruleset is None:
            raise not_found(f"规则集不存在: {ruleset_id}")
        eval_id = new_id("eval")
        self.store.create_evaluation(eval_id, profile_id, profile["version"],
                                     ruleset_id, ruleset["version"], release_id)
        self.store.enqueue(new_id("job"), "evaluation", {"eval_id": eval_id})
        self.store.audit(actor, "evaluation.requested", "evaluation", eval_id,
                         {"profile_id": profile_id,
                          "profile_version": profile["version"],
                          "ruleset_version": ruleset["version"],
                          "release_id": release_id})
        return eval_id

    def run_evaluation(self, eval_id: str):
        """由队列 worker 调用：执行判定并落库；关联发布追加复评结果事件。"""
        record = self.store.get_evaluation(eval_id)
        if record is None:
            raise not_found(f"评估不存在: {eval_id}")
        if record["status"] == "COMPLETED":
            return record
        profile = self.get_profile(record["profile_id"], record["profile_version"])
        ruleset = self.store.get_ruleset(record["ruleset_id"], record["ruleset_version"])
        now = self.now_fn()
        result = engine.evaluate(
            profile["payload"], ruleset,
            self.store.list_exemptions(record["profile_id"]),
            self.store.list_certificates(record["profile_id"]), now)
        result.update({
            "profile_id": record["profile_id"],
            "profile_version": record["profile_version"],
            "ruleset_id": record["ruleset_id"],
            "ruleset_version": record["ruleset_version"],
            "evaluated_at": now,
        })
        self.store.complete_evaluation(eval_id, result)
        self.store.audit(None, "evaluation.completed", "evaluation", eval_id,
                         {"decision": result["decision"]})
        release_id = record["release_id"]
        if release_id:
            blocked = result["decision"] == "BLOCKED" and result["uncovered_gaps"]
            status = "SUSPENDED" if blocked else "ACTIVE"
            self.store.add_release_event(release_id, status,
                                         {"eval_id": eval_id,
                                          "decision": result["decision"]})
            self.store.audit(None, f"release.{status.lower()}", "release", release_id,
                             {"eval_id": eval_id, "decision": result["decision"]})
        return self.store.get_evaluation(eval_id)

    def get_evaluation(self, eval_id: str) -> dict:
        record = self.store.get_evaluation(eval_id)
        if record is None:
            raise not_found(f"评估不存在: {eval_id}")
        return record

    # ---- 审查者与签署 ---------------------------------------------------

    def register_reviewer(self, reviewer_id, org, roles, conflicts=None, actor=None):
        if not reviewer_id or not org or not roles:
            raise validation("审查者需提供 reviewer_id / org / roles")
        self.store.put_reviewer(reviewer_id, org, roles, conflicts or [])
        self.store.audit(actor, "reviewer.registered", "reviewer", reviewer_id,
                         {"org": org, "roles": roles})
        return {"reviewer_id": reviewer_id}

    def sign(self, eval_id, reviewer_id, role, actor=None) -> dict:
        record = self.get_evaluation(eval_id)
        reviewer = self.store.get_reviewer(reviewer_id)
        if reviewer is None:
            raise not_found(f"审查者未注册: {reviewer_id}")
        if role not in reviewer["roles"]:
            raise conflict(f"审查者 {reviewer_id} 不持有角色 {role}")
        profile = self.get_profile(record["profile_id"], record["profile_version"])
        self._check_conflicts(reviewer, profile["payload"])
        if not self.store.add_signature(eval_id, role, reviewer_id, reviewer["org"]):
            raise conflict(f"角色 {role} 已签署，不可重复")
        self.store.audit(actor or reviewer_id, "evaluation.signed",
                         "evaluation", eval_id,
                         {"role": role, "reviewer_id": reviewer_id})
        return {"eval_id": eval_id, "role": role, "reviewer_id": reviewer_id}

    @staticmethod
    def _check_conflicts(reviewer, profile):
        """利益冲突隔离：审查者所属机构或其申报的冲突机构出现在
        档案归属方或供应商链中时，禁止签署。"""
        related = {profile.get("owner_org")}
        related |= {v.get("org") for v in profile.get("vendor_chain", [])}
        related.discard(None)
        clashes = sorted(({reviewer["org"]} | set(reviewer["conflicts"])) & related)
        if clashes:
            raise conflict(f"审查者存在利益冲突，禁止签署: {clashes}")

    # ---- 豁免与证书 -----------------------------------------------------

    def grant_exemption(self, profile_id, expires_at, reason, granted_by,
                        rule_ids=None, evidence_ids=None, obligation_ids=None,
                        actor=None) -> dict:
        if not expires_at or not reason or not granted_by:
            raise validation("豁免必须包含 expires_at / reason / granted_by")
        if expires_at <= self.now_fn():
            raise validation("豁免期限必须晚于当前时间（临时豁免须有期限）")
        if not (rule_ids or evidence_ids or obligation_ids):
            raise validation("豁免必须限定范围：规则、证据或义务至少一项")
        self.get_profile(profile_id)
        exemption_id = new_id("exm")
        self.store.add_exemption(exemption_id, profile_id, rule_ids or [],
                                 evidence_ids or [], obligation_ids or [],
                                 expires_at, reason, granted_by)
        self.store.audit(actor or granted_by, "exemption.granted", "exemption",
                         exemption_id,
                         {"profile_id": profile_id, "expires_at": expires_at})
        return {"exemption_id": exemption_id}

    def register_certificate(self, profile_id, cert_type, issuer, expires_at,
                             actor=None) -> dict:
        if not cert_type or not issuer or not expires_at:
            raise validation("证书必须包含 cert_type / issuer / expires_at")
        self.get_profile(profile_id)
        cert_id = new_id("cert")
        self.store.add_certificate(cert_id, profile_id, cert_type, issuer, expires_at)
        self.store.audit(actor, "certificate.registered", "certificate", cert_id,
                         {"profile_id": profile_id, "cert_type": cert_type,
                          "expires_at": expires_at})
        return {"cert_id": cert_id}

    # ---- 发布门禁 -------------------------------------------------------

    def request_release(self, profile_id, profile_version=None, actor=None) -> dict:
        profile = self.get_profile(profile_id, profile_version)
        ruleset = self.store.get_ruleset(DEFAULT_RULESET)
        if ruleset is None:
            raise not_found("规则集未初始化")
        evaluation = self.store.latest_completed_evaluation(
            profile_id, profile["version"], ruleset["ruleset_id"], ruleset["version"])
        if evaluation is None:
            eval_id = self.request_evaluation(
                profile_id, profile["version"], actor=actor)
            return {"status": "EVALUATION_PENDING", "eval_id": eval_id,
                    "detail": "当前规则集版本下尚无完成的评估，已入队，请稍后重试"}

        now = self.now_fn()
        result = evaluation["result"]
        # 豁免与证书具有时效性，发布时点重新核算，不复用评估时的结论。
        gaps = engine.apply_exemptions(
            [{k: g[k] for k in ("kind", "id", "rule_id", "severity")}
             for g in result["evidence_gaps"]],
            self.store.list_exemptions(profile_id), now)
        uncovered = [g for g in gaps if not g["covered_by"]]
        cert_blockers = self._cert_blockers(profile_id, result["required_certs"], now)

        signatures = self.store.list_signatures(evaluation["eval_id"])
        signed_roles = {s["role"] for s in signatures}
        required_roles = set(result["required_roles"])
        missing_roles = sorted(required_roles - signed_roles)

        deny_reasons = []
        if result["decision"] == "BLOCKED" and uncovered:
            deny_reasons.append("存在未豁免的阻断级缺口")
        if uncovered:
            deny_reasons.append(f"未覆盖的证据缺口: {[g['id'] for g in uncovered]}")
        if missing_roles:
            deny_reasons.append(f"缺少必要角色签署: {missing_roles}")
        if cert_blockers:
            deny_reasons.append(f"外部证明缺失或已过期: {cert_blockers}")

        release_id = new_id("rel")
        decision = {
            "release_id": release_id,
            "profile_id": profile_id,
            "profile_version": profile["version"],
            "eval_id": evaluation["eval_id"],
            "ruleset_id": ruleset["ruleset_id"],
            "ruleset_version": ruleset["version"],
            "status": "DENIED" if deny_reasons else "ACTIVE",
            "deny_reasons": deny_reasons,
            "decision": result["decision"],
            "obligations": result["obligations"],
            "evidence_gaps": gaps,
            "uncovered_gaps": uncovered,
            "signatures": {
                "required": sorted(required_roles),
                "present": sorted(signed_roles),
                "missing": missing_roles,
            },
            "certificates": {
                "required": result["required_certs"],
                "invalid": cert_blockers,
            },
            "trace": result["trace"],
        }
        self.store.create_release(release_id, profile_id, profile["version"],
                                  evaluation["eval_id"], decision)
        self.store.add_release_event(release_id, decision["status"],
                                     {"deny_reasons": deny_reasons})
        self.store.audit(actor, f"release.{decision['status'].lower()}",
                         "release", release_id, decision)
        return decision

    def _cert_blockers(self, profile_id, required_certs, now) -> list:
        held = self.store.list_certificates(profile_id)
        blockers = []
        for cert_type in required_certs:
            candidates = [c for c in held if c["cert_type"] == cert_type]
            if not candidates:
                blockers.append(f"{cert_type}: 缺失")
            elif not any(c["expires_at"] > now for c in candidates):
                blockers.append(f"{cert_type}: 已过期")
        return blockers

    def get_release(self, release_id) -> dict:
        release = self.store.get_release(release_id)
        if release is None:
            raise not_found(f"发布不存在: {release_id}")
        release["events"] = self.store.release_events(release_id)
        return release

    # ---- 流量门禁 -------------------------------------------------------

    def authorize_traffic(self, release_id) -> dict:
        """新流量准入：发布须处于可放行状态，且所需外部证明未过期。"""
        release = self.store.get_release(release_id)
        if release is None:
            raise not_found(f"发布不存在: {release_id}")
        reasons = []
        if release["status"] not in RELEASABLE_STATUSES:
            reasons.append(f"发布状态为 {release['status']}，不可放行")
        evaluation = self.store.get_evaluation(release["eval_id"])
        blockers = self._cert_blockers(release["profile_id"],
                                       evaluation["result"]["required_certs"],
                                       self.now_fn())
        if blockers:
            reasons.append(f"外部证明不可用: {blockers}")
        allowed = not reasons
        self.store.audit(None, "traffic.authorized" if allowed else "traffic.blocked",
                         "release", release_id, {"reasons": reasons})
        if not allowed:
            raise DomainError("traffic_blocked", "；".join(reasons), 409)
        return {"release_id": release_id, "allowed": True}
