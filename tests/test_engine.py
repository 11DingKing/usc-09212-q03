"""判定引擎：辖区规则命中、义务/缺口计算、规则更新定位、旧批准不被改写。"""
import json
import unittest

from service.clock import FixedClock
from service.api import build_app
from service.seed_rules import SEED_PACKAGE
from tests.support import CROSS_BORDER_PROFILE
from datetime import datetime, timezone


def fixed_clock():
    return FixedClock(datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc))


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.clock = fixed_clock()
        self.app = build_app(clock=self.clock, seed=True)

    def _drain(self):
        self.app.worker.run_pending()

    def test_obligations_and_evidence_gaps_for_cross_border_minors(self):
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current(
            "rel-1", "test", self.app.rules)
        self._drain()
        result = self.app.gate.decision("rel-1")
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertFalse(result["traffic_allowed"])
        ids = {o["obligation_id"] for o in result["obligations"]}
        # 部署在 SG：高风险决策 + PDPA 同意；CN 因数据主体在境内且跨境命中出境
        self.assertIn("SG-AI-HIGH-RISK-01", ids)
        self.assertIn("SG-PDPA-CONSENT-01", ids)
        self.assertIn("CN-DATA-EXPORT-01", ids)
        # 未成年人规则仅在部署地 CN 触发（SG 种子无未成年人条款）
        self.assertNotIn("CN-MINORS-01", ids)
        gap_items = {g["item"] for g in result["evidence_gaps"]}
        self.assertIn("ai_impact_assessment", gap_items)
        self.assertIn("consent_notice", gap_items)
        self.assertIn("data_export_security_assessment", gap_items)
        # 外部证明缺口给出具体原因
        cert_gaps = [g for g in result["evidence_gaps"]
                     if g["type"] == "certificate"]
        self.assertTrue(cert_gaps)
        self.assertEqual(cert_gaps[0]["reason"], "missing")

    def test_minors_rule_when_deployed_in_cn(self):
        profile = dict(CROSS_BORDER_PROFILE,
                       deployment_jurisdiction="CN",
                       data_subject_jurisdictions=["CN", "SG"],
                       purpose="content_recommendation",
                       high_risk_decision=False)
        self.app.releases.register("rel-cn", profile)
        self.app.queue.enqueue_for_current("rel-cn", "test", self.app.rules)
        self._drain()
        result = self.app.gate.decision("rel-cn")
        ids = {o["obligation_id"] for o in result["obligations"]}
        self.assertIn("CN-MINORS-01", ids)
        self.assertIn("CN-ALGO-REC-01", ids)
        self.assertIn("CN-DATA-EXPORT-01", ids)
        gap_items = {g["item"] for g in result["evidence_gaps"]}
        self.assertIn("guardian_consent", gap_items)
        self.assertIn("minors_processing_policy", gap_items)

    def test_completing_evidence_clears_gaps_and_reaches_signoff(self):
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "test", self.app.rules)
        self._drain()
        blocked = self.app.gate.decision("rel-1")
        # 补齐全部证据与外部证明
        kinds = {g["item"] for g in blocked["evidence_gaps"]
                 if g["type"] == "evidence"}
        for kind in sorted(kinds):
            self.app.evidence.add("rel-1", f"ev-{kind}", kind, f"{kind} 文件")
        self.app.certificates.register(
            "cert-1", "中国国家网信部门", "cn_cross_border_data_transfer",
            "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
            release_id="rel-1", jurisdiction="CN")
        self._drain()
        result = self.app.gate.decision("rel-1")
        self.assertEqual(result["decision"], "PENDING_SIGNOFF")
        self.assertIn("ai_governance_lead", result["missing_roles"])
        self.assertIn("business_owner", result["missing_roles"])
        self.assertIn("privacy_officer", result["missing_roles"])

    def test_rule_update_targets_affected_release_without_touching_old(self):
        profile = dict(CROSS_BORDER_PROFILE, involves_minors=False)
        self.app.releases.register("rel-1", profile)
        self.app.queue.enqueue_for_current("rel-1", "test", self.app.rules)
        self._drain()
        first = self.app.gate.decision("rel-1")
        first_id = first["assessment_id"]
        first_hash = first["assessment_hash"]
        first_obligations = {o["obligation_id"]
                             for o in first["obligations"]}

        # 发布新规则：SG 新增未成年人专项义务
        new_package = json.loads(json.dumps(SEED_PACKAGE))
        new_package["version"] = "2026.12"
        new_package["jurisdictions"]["SG"]["rules"].append({
            "id": "SG-MINORS-01",
            "title": "未成年人保护设计规范",
            "detail": "面向未成年人的 AI 应用须采用年龄适配设计。",
            "when": {"deployment_in": ["SG"], "involves_minors": True},
            "obligation": {"risk_level": "high",
                           "evidence": ["age_appropriate_design"],
                           "required_roles": ["privacy_officer"]},
        })
        package, created = self.app.rules.publish(new_package)
        self.assertTrue(created)
        # rel-1 不涉及未成年人，义务指纹不变 -> 不受影响、不入队
        affected = self.app.enqueue_affected_by_rules(package)
        self.assertEqual(affected, [])
        still = self.app.gate.decision("rel-1")
        self.assertEqual(still["decision"], first["decision"])
        self.assertEqual(still["assessment_id"], first_id)

        # 另一发布涉及未成年人 -> 自动定位入队
        self.app.releases.register("rel-2", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-2", "test", self.app.rules)
        self._drain()
        # 修订 rel-1 改为涉及未成年人，新规则命中 -> 自动重评
        self.app.releases.amend("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "amend", self.app.rules)
        self._drain()
        reevaluated = self.app.gate.decision("rel-1")
        self.assertNotEqual(reevaluated["assessment_id"], first_id)
        # 旧记录与旧哈希保持不变（不追溯篡改旧批准）
        old = self.app.store.query_one(
            "SELECT * FROM assessments WHERE id=?", (first_id,))
        self.assertEqual(old["entry_hash"], first_hash)
        self.assertEqual(
            {o["obligation_id"] for o in
             json.loads(old["triggered_obligations"])}, first_obligations)

    def test_rule_obligation_content_change_triggers_reevaluation(self):
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "test", self.app.rules)
        self._drain()
        first = self.app.gate.decision("rel-1")

        new_package = json.loads(json.dumps(SEED_PACKAGE))
        new_package["version"] = "2026.12"
        # 同一命中集合但义务内容变化：增加必要角色
        for rule in new_package["jurisdictions"]["SG"]["rules"]:
            if rule["id"] == "SG-AI-HIGH-RISK-01":
                rule["obligation"]["required_roles"].append("legal_counsel")
        package, _ = self.app.rules.publish(new_package)
        affected = self.app.enqueue_affected_by_rules(package)
        self.assertIn("rel-1", affected)
        self._drain()
        result = self.app.gate.decision("rel-1")
        self.assertEqual(result["rule_version"], "2026.12")
        sg_rule = [o for o in result["obligations"]
                   if o["obligation_id"] == "SG-AI-HIGH-RISK-01"][0]
        self.assertIn("legal_counsel", sg_rule["required_roles"])

    def test_hash_chain_anchors_every_assessment(self):
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "test", self.app.rules)
        self._drain()
        self.app.evidence.add("rel-1", "ev-1", "consent_notice", "x")
        self._drain()
        count, intact = self.app.store.verify_chain()
        self.assertEqual(count, 2)
        self.assertTrue(intact)
        result = self.app.gate.decision("rel-1")
        anchor = [s for s in result["decision_path"]
                  if s.get("step") == "ledger_anchor"][0]
        self.assertEqual(anchor["entry_hash"], result["assessment_hash"])

    def test_decision_path_is_reviewable(self):
        self.app.releases.register("rel-1", CROSS_BORDER_PROFILE)
        self.app.queue.enqueue_for_current("rel-1", "test", self.app.rules)
        self._drain()
        result = self.app.gate.decision("rel-1")
        steps = [p["step"] for p in result["decision_path"]]
        self.assertEqual(steps[0], "profile_locked")
        self.assertIn("rules_locked", steps)
        self.assertIn("condition_matches", steps)
        self.assertIn("evidence_resolution", steps)
        self.assertIn("signoff_resolution", steps)
        self.assertIn("live_gate_checks", steps)
        matches = [p for p in result["decision_path"]
                   if p.get("step") == "condition_matches"][0]
        sg_rule = [r for r in matches["rules"]
                   if r["rule_id"] == "SG-AI-HIGH-RISK-01"][0]
        self.assertIn("high_risk_decision", sg_rule["matched_conditions"])


if __name__ == "__main__":
    unittest.main()
