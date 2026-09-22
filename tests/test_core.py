"""合规判定核心业务流测试。"""
import copy
import threading
import unittest

from service.core import ComplianceService, DomainError
from service.seed import RULES_V1, seed_if_empty
from service.store import Store
from service.worker import QueueWorker

FUTURE = "2099-01-01T00:00:00+00:00"
T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-06-01T00:00:00+00:00"
T2 = "2027-01-01T00:00:00+00:00"

PROFILE = {
    "model_version": "m-1.0.0",
    "owner_org": "OrgA",
    "data_categories": ["personal"],
    "purposes": ["chat"],
    "data_origin_country": "CN",
    "deployment_country": "SG",
    "vendor_chain": [{"org": "VendorCloud", "role": "hosting"}],
    "evidence": [
        {"id": "ev.cn.security_assessment_report"},
        {"id": "ev.sg.consent_records"},
        {"id": "ev.sg.transfer_agreement"},
    ],
}


def make_service(now=T0):
    """内存存储 + 基线规则；clock[0] 可变以模拟时间推进。"""
    clock = [now]
    store = Store(":memory:")
    seed_if_empty(store)
    service = ComplianceService(store, now_fn=lambda: clock[0])
    return service, QueueWorker(service), clock


def put_and_evaluate(service, worker, profile=None):
    service.put_profile("p1", copy.deepcopy(profile or PROFILE))
    eval_id = service.request_evaluation("p1")
    worker.run_until_idle()
    return service.get_evaluation(eval_id)


def register_reviewers(service):
    service.register_reviewer("rev-dpo", "ReviewOrg", ["dpo"])
    service.register_reviewer("rev-legal", "ReviewOrg", ["legal"])
    service.register_reviewer("rev-sec", "ReviewOrg", ["security"])
    service.register_reviewer("rev-mo", "ReviewOrg", ["model_owner"])


class EvaluationFlowTest(unittest.TestCase):
    def test_evaluation_returns_obligations_gaps_trace(self):
        service, worker, _ = make_service()
        record = put_and_evaluate(service, worker)
        result = record["result"]
        # 缺 CAC 安全评估证明 → 阻断级缺口
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertEqual([g["id"] for g in result["uncovered_gaps"]],
                         ["cert.cac_security_assessment"])
        self.assertEqual(result["required_roles"], ["dpo", "legal"])
        self.assertTrue(result["obligations"])
        self.assertTrue(result["trace"])
        self.assertEqual(result["profile_version"], 1)

    def test_full_release_flow_active(self):
        service, worker, _ = make_service()
        service.put_profile("p1", copy.deepcopy(PROFILE))
        service.register_certificate("p1", "cert.cac_security_assessment",
                                     "CAC", FUTURE)
        eval_id = service.request_evaluation("p1")
        worker.run_until_idle()
        self.assertEqual(service.get_evaluation(eval_id)["result"]["decision"],
                         "APPROVED")
        register_reviewers(service)
        service.sign(eval_id, "rev-dpo", "dpo")
        service.sign(eval_id, "rev-legal", "legal")
        decision = service.request_release("p1")
        self.assertEqual(decision["status"], "ACTIVE")
        self.assertEqual(decision["deny_reasons"], [])
        self.assertTrue(decision["obligations"])
        self.assertTrue(decision["trace"])
        self.assertEqual(decision["signatures"]["missing"], [])
        # 新流量放行
        self.assertTrue(service.authorize_traffic(decision["release_id"])["allowed"])

    def test_release_waits_for_evaluation(self):
        service, worker, _ = make_service()
        service.put_profile("p1", copy.deepcopy(PROFILE))
        pending = service.request_release("p1")
        self.assertEqual(pending["status"], "EVALUATION_PENDING")
        worker.run_until_idle()


class SignatureTest(unittest.TestCase):
    def setUp(self):
        self.service, self.worker, _ = make_service()
        self.service.put_profile("p1", copy.deepcopy(PROFILE))
        self.service.register_certificate(
            "p1", "cert.cac_security_assessment", "CAC", FUTURE)
        self.eval_id = self.service.request_evaluation("p1")
        self.worker.run_until_idle()
        register_reviewers(self.service)

    def test_concurrent_signatures_all_roles_required(self):
        # 并发签署：不同角色可同时到达，缺一不可
        threads = [
            threading.Thread(target=self.service.sign,
                             args=(self.eval_id, "rev-dpo", "dpo")),
            threading.Thread(target=self.service.sign,
                             args=(self.eval_id, "rev-legal", "legal")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        signatures = self.service.store.list_signatures(self.eval_id)
        self.assertEqual({s["role"] for s in signatures}, {"dpo", "legal"})

    def test_missing_role_blocks_release(self):
        self.service.sign(self.eval_id, "rev-dpo", "dpo")
        decision = self.service.request_release("p1")
        self.assertEqual(decision["status"], "DENIED")
        self.assertEqual(decision["signatures"]["missing"], ["legal"])

    def test_duplicate_role_rejected(self):
        self.service.sign(self.eval_id, "rev-dpo", "dpo")
        with self.assertRaises(DomainError):
            self.service.sign(self.eval_id, "rev-dpo", "dpo")

    def test_role_not_held_rejected(self):
        with self.assertRaises(DomainError):
            self.service.sign(self.eval_id, "rev-dpo", "legal")

    def test_conflict_of_interest_isolated(self):
        # 供应商链成员、档案归属方、申报冲突机构均不得签署
        self.service.register_reviewer("rev-vendor", "VendorCloud", ["dpo"])
        self.service.register_reviewer("rev-owner", "OrgA", ["dpo"])
        self.service.register_reviewer("rev-declared", "OtherOrg", ["dpo"],
                                       conflicts=["VendorCloud"])
        for reviewer in ("rev-vendor", "rev-owner", "rev-declared"):
            with self.assertRaises(DomainError, msg=reviewer):
                self.service.sign(self.eval_id, reviewer, "dpo")
        self.assertEqual(self.service.store.list_signatures(self.eval_id), [])


class ExemptionTest(unittest.TestCase):
    def test_scoped_exemption_allows_release_until_expiry(self):
        service, worker, clock = make_service(now=T0)
        profile = copy.deepcopy(PROFILE)
        # 移除安全评估报告，制造证据缺口
        profile["evidence"] = [e for e in profile["evidence"]
                               if e["id"] != "ev.cn.security_assessment_report"]
        service.put_profile("p1", profile)
        service.register_certificate("p1", "cert.cac_security_assessment",
                                     "CAC", FUTURE)
        service.grant_exemption("p1", T1, "整改期临时豁免", "ciso",
                                evidence_ids=["ev.cn.security_assessment_report"])
        eval_id = service.request_evaluation("p1")
        worker.run_until_idle()
        gaps = service.get_evaluation(eval_id)["result"]["evidence_gaps"]
        covered = [g for g in gaps if g["id"] == "ev.cn.security_assessment_report"]
        self.assertTrue(covered[0]["covered_by"])
        register_reviewers(service)
        service.sign(eval_id, "rev-dpo", "dpo")
        service.sign(eval_id, "rev-legal", "legal")
        self.assertEqual(service.request_release("p1")["status"], "ACTIVE")
        # 豁免到期后：新发布被阻断（发布时点重新核算豁免有效性）
        clock[0] = T2
        decision = service.request_release("p1")
        self.assertEqual(decision["status"], "DENIED")
        self.assertTrue(decision["uncovered_gaps"])

    def test_exemption_requires_scope_and_future_expiry(self):
        service, _, _ = make_service()
        service.put_profile("p1", copy.deepcopy(PROFILE))
        with self.assertRaises(DomainError):
            service.grant_exemption("p1", FUTURE, "r", "ciso")  # 无范围
        with self.assertRaises(DomainError):
            service.grant_exemption("p1", "2020-01-01T00:00:00+00:00", "r",
                                    "ciso", rule_ids=["CN-XFER-PI"])  # 已过期


class CertificateTrafficTest(unittest.TestCase):
    def test_expired_certificate_blocks_new_traffic(self):
        service, worker, clock = make_service(now=T0)
        service.put_profile("p1", copy.deepcopy(PROFILE))
        service.register_certificate("p1", "cert.cac_security_assessment",
                                     "CAC", T1)
        eval_id = service.request_evaluation("p1")
        worker.run_until_idle()
        register_reviewers(service)
        service.sign(eval_id, "rev-dpo", "dpo")
        service.sign(eval_id, "rev-legal", "legal")
        decision = service.request_release("p1")
        self.assertEqual(decision["status"], "ACTIVE")
        self.assertTrue(service.authorize_traffic(decision["release_id"])["allowed"])
        # 证明到期 → 阻断新流量，且新发布被拒绝
        clock[0] = T2
        with self.assertRaises(DomainError) as ctx:
            service.authorize_traffic(decision["release_id"])
        self.assertEqual(ctx.exception.status, 409)
        followup = service.request_release("p1")
        self.assertEqual(followup["status"], "DENIED")
        self.assertTrue(any("过期" in r or "缺失" in r
                            for r in followup["deny_reasons"]))


class RulesetUpdateTest(unittest.TestCase):
    def _active_release(self, service, worker):
        service.put_profile("p1", copy.deepcopy(PROFILE))
        service.register_certificate("p1", "cert.cac_security_assessment",
                                     "CAC", FUTURE)
        eval_id = service.request_evaluation("p1")
        worker.run_until_idle()
        register_reviewers(service)
        service.sign(eval_id, "rev-dpo", "dpo")
        service.sign(eval_id, "rev-legal", "legal")
        decision = service.request_release("p1")
        self.assertEqual(decision["status"], "ACTIVE")
        return eval_id, decision["release_id"]

    def test_update_locates_releases_without_rewriting_approvals(self):
        service, worker, _ = make_service()
        old_eval_id, release_id = self._active_release(service, worker)
        old_eval = service.get_evaluation(old_eval_id)
        old_release = service.get_release(release_id)
        old_signatures = service.store.list_signatures(old_eval_id)

        # 规则更新：SG 同意规则追加新的证据要求（阻断级新规则）
        rules_v2 = copy.deepcopy(RULES_V1)
        rules_v2.append({
            "id": "SG-HIGHRISK-AUDIT", "title": "高风险决策年度审计",
            "jurisdiction": "SG", "theme": "high_risk_decision",
            "severity": "block",
            "when": {"data_categories_any": ["personal"]},
            "obligations": ["ob.sg.annual_audit"],
            "required_evidence": ["ev.sg.annual_audit_report"],
            "required_roles": ["legal"],
        })
        outcome = service.register_ruleset("asean-cn-baseline", rules_v2)
        self.assertEqual(outcome["version"], 2)
        self.assertIn("SG-HIGHRISK-AUDIT", outcome["changed_rules"])
        self.assertEqual([a["release_id"] for a in outcome["affected_releases"]],
                         [release_id])
        # 发布被标记待复评，复评完成后因阻断级缺口被暂停
        self.assertEqual(service.store.release_status(release_id),
                         "REASSESSMENT_PENDING")
        worker.run_until_idle()
        self.assertEqual(service.store.release_status(release_id), "SUSPENDED")
        with self.assertRaises(DomainError):
            service.authorize_traffic(release_id)

        # 旧批准不被追溯篡改：旧评估、旧签署、旧发布记录保持原样
        self.assertEqual(service.get_evaluation(old_eval_id), old_eval)
        self.assertEqual(service.store.list_signatures(old_eval_id), old_signatures)
        current = service.get_release(release_id)
        self.assertEqual(current["decision"], old_release["decision"])
        statuses = [e["status"] for e in current["events"]]
        self.assertEqual(statuses, ["ACTIVE", "REASSESSMENT_PENDING", "SUSPENDED"])

    def test_unaffected_release_not_flagged(self):
        service, worker, _ = make_service()
        _, release_id = self._active_release(service, worker)
        rules_v2 = copy.deepcopy(RULES_V1)
        # 仅修改与档案不匹配的 TH 规则
        for rule in rules_v2:
            if rule["id"] == "TH-MINOR-CONSENT":
                rule["required_evidence"] = ["ev.th.guardian_consent_records",
                                             "ev.th.extra"]
        outcome = service.register_ruleset("asean-cn-baseline", rules_v2)
        self.assertEqual(outcome["affected_releases"], [])
        self.assertEqual(service.store.release_status(release_id), "ACTIVE")


class QueueRecoveryTest(unittest.TestCase):
    def test_queue_survives_restart(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/compliance.db"
            store = Store(path)
            seed_if_empty(store)
            service = ComplianceService(store)
            service.put_profile("p1", copy.deepcopy(PROFILE))
            eval_id = service.request_evaluation("p1")
            # 模拟崩溃：一个任务被置为 RUNNING 后进程退出
            job = store.claim_job()
            self.assertIsNotNone(job)
            store.close()

            # 重启：恢复队列并继续处理
            store2 = Store(path)
            self.assertEqual(store2.recover_queue(), 1)
            service2 = ComplianceService(store2)
            worker = QueueWorker(service2)
            self.assertEqual(worker.run_until_idle(), 1)
            record = service2.get_evaluation(eval_id)
            self.assertEqual(record["status"], "COMPLETED")
            self.assertEqual(store2.queue_counts()["DONE"], 1)
            store2.close()

    def test_audit_trail_is_append_only(self):
        service, worker, _ = make_service()
        service.put_profile("p1", copy.deepcopy(PROFILE))
        eval_id = service.request_evaluation("p1")
        worker.run_until_idle()
        entries = service.store.list_audit()
        actions = [e["action"] for e in entries]
        self.assertIn("profile.version_created", actions)
        self.assertIn("evaluation.requested", actions)
        self.assertIn("evaluation.completed", actions)
        for entry in entries:
            self.assertTrue(entry["recorded_at"])
            self.assertIn("occurred_at", entry)


if __name__ == "__main__":
    unittest.main()
